# -*- coding: utf-8 -*-
"""小小英雄定位: 等级数字块 + 右侧血条颜色（纯图形学, 不动游戏、不动键鼠）。

## 为什么不用「找血条」那套

旧方案（`recognize/board.py::_find_bars` + `_has_name_text`）是**先找横条、再看条上方有没有白像素**。
这个判据太宽松 —— 好友列表头像的头发/脸部横条、聊天框文字条、底部 UI 都能满足, 实测战斗帧
3 个"检出"里 2 个是假阳性（x=119 那两条是好友列表 108 号头像）。而且整帧要 150~200ms。

新方案（用户 m02435 提出, m02459 实测验证）**反过来锚定**:
    等级数字块（白色连通域） → 它右侧一小段必须是**红/绿血条** → 才是小小英雄

实测 5 帧全对, 10~16ms（旧方案 150~200ms）:
    draft 2-4   8/8（我方1 敌方7）   16.0ms
    draft 3-4   7/7（我方1 敌方6）   12.8ms
    combat 22:21 1/1（发糖是怪兽）   12.2ms
    combat 22:26 1/1（我方）         10.7ms
    combat 22:27 1/1                 11.8ms

## 四个关键门槛（都是实测出来的, 别乱调）

1. **白块尺寸** `7<=w<=24 / 11<=h<=28 / area>=40` —— 等级数字实测 11~12 x 16~17。
2. **暗底** 白块外扩 6px 的暗像素占比 >= 100（0~255 均值）—— 排除亮背景上的白字（聊天文字、
   玩家名牌都在半透明暗底上, 这个门槛主要挡"亮地形上的白点"）。
3. **右侧血条颜色**（用户原话: 「匹配文字图标的右边一点点, 判断它是红色还是绿色」）:
   - 我方 `g > r+50 且 g > b+50 且 g >= 110`
   - 敌方 `r > g+50 且 r > b+40 且 r >= 120`
   色差 + 亮度两个条件缺一不可 —— 只判色差会把**暗背景**判成绿
   （实测假阳性 `rgb(19,55,26)`、`(16,44,18)`、`(34,69,42)` 全是深绿地面/暗 UI）。
4. **等级数字模板**（可选, `recognize/templates/hero_level/<等级>.png`）:
   血量数字也是白块、右边也是橙红, 单靠 1~3 会把它当等级块。模板匹配作**软分**,
   同一英雄有多个候选时优先取分数高的、且优先取**最左**那个（等级数字永远在最左）。

## 坐标口径

全部 1920x1080 基准。`numblk` = 等级数字块 bbox `(x, y, w, h)`; 英雄落点由
`hero_offset()` 从数字块推出（偏移在 `config.DRAFT_HERO_LEVEL_*` 里, 默认按 draft 实测标定）。

用法:
    python -X utf8 -m recognize.draft_hero --img tempdir/labelme_hero/draft_2_4_enter.png
    python -X utf8 -m recognize.draft_hero --img xxx.png --annot tempdir/out.png
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import cv2
import numpy as np

from config.config import default_config as cfg

# 等级数字模板目录（用户 m02460: 「我们缺少 1~10级所有白色字体等级模板」）
LEVEL_TEMPLATE_DIR = cfg.PROJECT_ROOT / "recognize" / "templates" / "hero_level"
LEVEL_TPL_W, LEVEL_TPL_H = 16, 22     # 模板归一化尺寸（保持等级数字 12x17 的比例）
LEVEL_WHITE_LO = 230                  # 等级数字白阈值（比血条白 200 更严: 滤掉等级块外框灰高光）

_LEVEL_TPL: dict[int, np.ndarray] | None = None

# 白块尺寸闸（等级数字实测 11~12 x 16~17; 两位数字如 "10" 会拆成两块）
BLK_W_MIN, BLK_W_MAX = 7, 24
BLK_H_MIN, BLK_H_MAX = 11, 28
BLK_AREA_MIN = 40

# 暗底闸: 白块外扩 6px 的暗像素占比（0~255 均值）
DARK_PAD = 6
DARK_MEAN_MIN = 100

# 右侧取色窗口: 从白块右边界 +4 到 +22, 中心行 ±6
SEG_DX0, SEG_DX1, SEG_DY = 4, 22, 6
SEG_MIN_W = 8

# 我方(绿) / 敌方(红) 判据
MINE_DC, MINE_MIN = 50, 110          # g > r+DC 且 g > b+DC 且 g >= MIN
ENEMY_DC, ENEMY_DB, ENEMY_MIN = 50, 40, 120

# 同英雄去重半径（px）
DEDUP_PX = 40.0


def _dedup_px() -> float:
    return float(getattr(cfg, "DRAFT_HERO_LEVEL_TOL", DEDUP_PX) or DEDUP_PX)


# --------------------------------------------------------------------------- #
# 等级数字模板
# --------------------------------------------------------------------------- #
def level_templates() -> dict[int, np.ndarray]:
    """加载 `recognize/templates/hero_level/<等级>.png`（灰度 0/255, 16x22）。

    目录不存在 / 为空时返回 `{}` —— 此时模块照常工作, 只是少了"等级数字 vs 血量数字"的
    区分能力（`detect_units` 里 `level` 为 None, 去重改为"取最左"）。
    """
    global _LEVEL_TPL
    if _LEVEL_TPL is not None:
        return _LEVEL_TPL
    out: dict[int, np.ndarray] = {}
    d = LEVEL_TEMPLATE_DIR
    if d.is_dir():
        for p in sorted(d.glob("*.png")):
            try:
                lv = int(p.stem)
            except ValueError:
                continue
            arr = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if arr is None:
                continue
            if arr.shape[:2] != (LEVEL_TPL_H, LEVEL_TPL_W):
                arr = cv2.resize(arr, (LEVEL_TPL_W, LEVEL_TPL_H),
                                 interpolation=cv2.INTER_AREA)
            out[lv] = arr
    _LEVEL_TPL = out
    return out


def reset_level_templates() -> None:
    """清模板缓存（测试/用户新截模板后用）。"""
    global _LEVEL_TPL
    _LEVEL_TPL = None


def digit_mask(rgb: np.ndarray, blk: tuple[int, int, int, int]) -> np.ndarray:
    """把等级数字块裁出来, **整体拉伸**成 LEVEL_TPL_W×LEVEL_TPL_H 的白字掩码。

    不做"保持宽高比 + 居中"(那样窄笔画「1」只占中间一条, 与「4」误配 0.80);
    统一拉伸到固定尺寸后, 形状差异才可比 —— 与 build_level_tpl.py / 离线模板同口径。
    """
    x, y, w, h = blk
    crop = rgb[y:y + h, x:x + w]
    m = cv2.inRange(crop, (LEVEL_WHITE_LO, LEVEL_WHITE_LO, LEVEL_WHITE_LO), (255, 255, 255))
    return cv2.resize(m, (LEVEL_TPL_W, LEVEL_TPL_H), interpolation=cv2.INTER_AREA)


def match_level(rgb: np.ndarray, blk: tuple[int, int, int, int]) -> tuple[int | None, float]:
    """等级数字块 -> (等级, 相似度)。没有模板时返回 (None, 0.0)。

    相似度用**余弦相似度**（不做去均值: 等级数字模板空白多, 去均值相关会让窄笔画「1」
    和任何块都虚高, 实测把我方 4 级误判成 1 级）。余弦只奖励笔画重叠, 不奖励"共同的黑背景"。
    """
    tpls = level_templates()
    if not tpls:
        return None, 0.0
    a = digit_mask(rgb, blk).astype(np.float32)
    na = float(np.sqrt((a * a).sum())) + 1e-6
    best_lv, best_sc = None, -9.0
    for lv, t in tpls.items():
        b = t.astype(np.float32)
        nb = float(np.sqrt((b * b).sum())) + 1e-6
        sc = float((a * b).sum()) / (na * nb)
        if sc > best_sc:
            best_lv, best_sc = lv, sc
    return best_lv, best_sc


# --------------------------------------------------------------------------- #
# 英雄落点
# --------------------------------------------------------------------------- #
def hero_offset() -> tuple[float, float]:
    """等级数字块中心 -> 英雄落点 的偏移 (dx, dy)。"""
    return float(cfg.DRAFT_HERO_LEVEL_DX), float(cfg.DRAFT_HERO_LEVEL_DY)


def blk_center(blk: tuple[int, int, int, int]) -> tuple[float, float]:
    x, y, w, h = blk
    return x + w / 2.0, y + h / 2.0


def hero_xy(blk: tuple[int, int, int, int]) -> tuple[float, float]:
    """等级数字块 -> 英雄落点坐标。"""
    cx, cy = blk_center(blk)
    dx, dy = hero_offset()
    return cx + dx, cy + dy


# --------------------------------------------------------------------------- #
# 主检测
# --------------------------------------------------------------------------- #
def _kind_of(rgb: np.ndarray, blk: tuple[int, int, int, int]) -> tuple[str | None, tuple[int, int, int]]:
    """看数字块右侧一小段是红还是绿 -> ("mine"|"enemy"|None, 该段平均 RGB)。"""
    H, W = rgb.shape[:2]
    x, y, w, h = blk
    cy = y + h // 2
    sx1, sx2 = x + w + SEG_DX0, min(W, x + w + SEG_DX1)
    sy1, sy2 = max(0, cy - SEG_DY), min(H, cy + SEG_DY + 1)
    if sx2 - sx1 < SEG_MIN_W:
        return None, (0, 0, 0)
    seg = rgb[sy1:sy2, sx1:sx2].reshape(-1, 3).astype(float)
    r, g, b = seg.mean(axis=0)
    if g > r + MINE_DC and g > b + MINE_DC and g >= MINE_MIN:
        return "mine", (int(r), int(g), int(b))
    if r > g + ENEMY_DC and r > b + ENEMY_DB and r >= ENEMY_MIN:
        return "enemy", (int(r), int(g), int(b))
    return None, (int(r), int(g), int(b))


def _white_blocks(rgb: np.ndarray, *, max_x: int | None = None) -> list[tuple[int, int, int, int]]:
    """白色连通域 -> 尺寸/暗底双筛后的候选块（**不含**血条颜色判据）。"""
    H, W = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    white = cv2.inRange(rgb, (200, 200, 200), (255, 255, 255))
    dark = cv2.inRange(gray, 0, 109)
    lim = W if max_x is None else min(W, int(max_x))
    n, _lbl, stats, _cent = cv2.connectedComponentsWithStats(white, connectivity=8)
    out: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (BLK_W_MIN <= w <= BLK_W_MAX and BLK_H_MIN <= h <= BLK_H_MAX
                and area >= BLK_AREA_MIN):
            continue
        if x >= lim:
            continue
        ex1, ey1 = max(0, x - DARK_PAD), max(0, y - DARK_PAD)
        ey2, ex2 = min(H, y + h + DARK_PAD), min(W, x + w + DARK_PAD)
        if dark[ey1:ey2, ex1:ex2].mean() < DARK_MEAN_MIN:
            continue
        out.append((int(x), int(y), int(w), int(h)))
    return out


def hero_blocks(rgb: np.ndarray, *, max_x: int | None = 1600) -> list[dict]:
    """所有「等级数字块 + 右侧红/绿小血段」候选（**未去重、不含英雄落点偏移**）。

    这是本方法的真正判据: 白数字块 (尺寸 + 暗底双筛) -> 右侧 x+w+4 .. x+w+22 的
    一小段必须过绿/红门槛 (绿=我方, 红=敌方)。棋盘/备战席的血条定位可以直接拿它当
    "这根血条是不是小小英雄"的锚点 —— 纯图色工程里比 OCR 名牌稳得多。

    Returns:
        `[{kind, level, level_score, numblk, rgb, score}]`; `numblk` 是白数字块 bbox
        `(x, y, w, h)` (cv2 口径), `rgb` 是右侧小血段的平均 RGB。
    """
    if max_x is None:
        max_x = getattr(cfg, "DRAFT_HERO_SEG_MAX_X", 1600) or None
    use_lv = bool(getattr(cfg, "DRAFT_HERO_USE_LEVEL_MATCH", True))
    out: list[dict] = []
    for blk in _white_blocks(rgb, max_x=max_x):
        kind, seg_rgb = _kind_of(rgb, blk)
        if kind is None:
            continue
        lv, lv_sc = match_level(rgb, blk) if use_lv else (None, 0.0)
        r, g, b = seg_rgb
        if kind == "mine":
            strength = (g - max(r, b)) / 255.0
        else:
            strength = (r - max(g, b)) / 255.0
        out.append({"kind": kind, "level": lv, "level_score": round(lv_sc, 3),
                    "numblk": blk, "rgb": seg_rgb,
                    "score": round(lv_sc if lv is not None else strength, 3)})
    return out


def detect_units(rgb: np.ndarray, *, max_x: int | None = 1600) -> list[dict]:
    """一帧画面 -> 小小英雄列表。

    参数 `max_x`: 只认 x 小于它的候选（默认 1600, 排除右侧玩家排行榜 UI; 传 None 不限制）。

    返回 `[{x, y, kind, level, level_score, numblk, rgb, score}]`:
        x, y        英雄落点（数字块中心 + 偏移）
        kind        "mine" | "enemy"
        level       等级数字（无模板时 None）
        numblk      等级数字块 bbox
        rgb         右侧血条段平均 RGB
        score       综合分（有等级模板时=模板相似度, 否则=色差强度归一化）
    """
    dedup = _dedup_px()
    cands: list[dict] = []
    for it in hero_blocks(rgb, max_x=max_x):
        blk = it["numblk"]
        hx, hy = hero_xy(blk)
        cands.append({"x": round(hx, 1), "y": round(hy, 1), "kind": it["kind"],
                      "level": it["level"], "level_score": it["level_score"],
                      "numblk": blk, "rgb": it["rgb"], "score": it["score"],
                      "_cx": blk[0] + blk[2] / 2.0, "_cy": blk[1] + blk[3] / 2.0})
    # 同英雄去重: 距离聚类; 分数高的优先, 同分取更靠左的（等级数字永远在最左）
    cands.sort(key=lambda c: (-c["score"], c["numblk"][0]))
    kept: list[dict] = []
    for c in cands:
        dup = False
        for k in kept:
            if (c["_cx"] - k["_cx"]) ** 2 + (c["_cy"] - k["_cy"]) ** 2 <= dedup ** 2:
                dup = True
                break
        if not dup:
            kept.append(c)
    for c in kept:
        c.pop("_cx", None)
        c.pop("_cy", None)
    kept.sort(key=lambda c: (c["y"], c["x"]))
    return kept


def locate_hero(bgr: np.ndarray, *, prev: tuple[float, float] | None = None) -> tuple[float, float, float] | None:
    """找**我方** hero 落点 -> `(x, y, score)`; 找不到返回 None。

    `prev` 给了上次位置时, 多候选里优先取离它最近的（防止把敌方误判成我方）。
    """
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB) if bgr.ndim == 3 and bgr.shape[2] == 3 else bgr
    mine = [u for u in detect_units(rgb) if u["kind"] == "mine"]
    if not mine:
        return None
    if prev is not None and len(mine) > 1:
        px, py = prev
        mine.sort(key=lambda u: (u["x"] - px) ** 2 + (u["y"] - py) ** 2)
    else:
        # 无历史位置时取**分数最高**的：等级数字匹配分很低(如 0.44)的通常是敌方满血绿血条
        # 被 g>r+50 误判成 mine，按 (y,x) 排序会先选中它。真我方 level_score 通常 >=0.8。
        mine.sort(key=lambda u: -u["score"])
    best = mine[0]
    return float(best["x"]), float(best["y"]), float(best["score"])


class HeroLocator:
    """我方英雄定位器 —— 等级块法（用户 m02669：「把我们定位 hero 的主流程换成这个」）。

    与早期「玩家名模板」版接口完全兼容, 供 `control/carousel_walker.py::_my_hero` 第 0 级使用：
      `.tpl`        有等级模板 -> 模板 dict（真值, walker 据此决定是否启用）; 没有 -> None
      `.name`       判据名 "level"
      `.ensure_template(bgr)`  预热（重读模板）, 返回是否就绪
      `.locate(bgr)` -> `((x, y), "level")` 或 None

    为什么比原 `detect_heroes`（血条 + 上方白字 + 217ms）好：等级数字块 + 血条颜色
    一体检测, 单帧 ~15~18ms, 我方落点与人工标注误差 1~2px（2-4 帧落点 960,884 = 真值）。
    """

    def __init__(self) -> None:
        self.name = "level"
        self.src = "level"
        self._prev: tuple[float, float] | None = None

    @property
    def tpl(self):
        try:
            return level_templates() or None
        except Exception:      # noqa: BLE001
            return None

    def ensure_template(self, bgr: np.ndarray | None = None) -> bool:
        try:
            reset_level_templates()
            return bool(level_templates())
        except Exception:      # noqa: BLE001
            return False

    def locate(self, bgr: np.ndarray, *, prev: tuple[float, float] | None = None):
        try:
            hit = locate_hero(bgr, prev=prev if prev is not None else self._prev)
        except Exception:      # noqa: BLE001 - 定位异常退回老判据即可
            return None
        if hit is None:
            return None
        x, y, _sc = hit
        self._prev = (x, y)
        return (float(x), float(y)), "level"


# --------------------------------------------------------------------------- #
# 命令行（人工看图）
# --------------------------------------------------------------------------- #
def annotate(rgb: np.ndarray, units: list[dict]) -> np.ndarray:
    from PIL import Image, ImageDraw

    from utils.show_img import load_cjk_font

    pil = Image.fromarray(rgb).convert("RGB")
    dr = ImageDraw.Draw(pil)
    f = load_cjk_font(20, bold=True)
    for u in units:
        x, y, w, h = u["numblk"]
        col = (40, 255, 80) if u["kind"] == "mine" else (255, 70, 70)
        dr.rectangle([x - 1, y - 1, x + w + 1, y + h + 1], outline=(255, 220, 0), width=2)
        dr.rectangle([x + w + SEG_DX0, y + h // 2 - SEG_DY,
                      x + w + SEG_DX1, y + h // 2 + SEG_DY], outline=col, width=2)
        tag = "我" if u["kind"] == "mine" else "敌"
        if u["level"] is not None:
            tag += f"L{u['level']}"
        dr.text((x - 2, y - 26), tag, fill=col, font=f)
        hx, hy = u["x"], u["y"]
        dr.ellipse([hx - 6, hy - 6, hx + 6, hy + 6], outline=col, width=2)
    return np.asarray(pil)


def main() -> int:
    ap = argparse.ArgumentParser(description="小小英雄定位（等级数字块 + 右侧血条颜色）")
    ap.add_argument("--img", required=True, help="图片路径（任意尺寸, 按 1920x1080 口径）")
    ap.add_argument("--annot", default="", help="标注图输出路径")
    ap.add_argument("--max-x", type=int, default=1600, help="只认 x 小于它的候选 (0=不限制)")
    args = ap.parse_args()

    from PIL import Image

    rgb = np.asarray(Image.open(args.img).convert("RGB"))
    t = time.perf_counter()
    units = detect_units(rgb, max_x=(args.max_x or None))
    ms = (time.perf_counter() - t) * 1000
    tpls = level_templates()
    print(f"[draft-hero] {args.img}  {rgb.shape[1]}x{rgb.shape[0]}  "
          f"共 {len(units)} 个  等级模板 {len(tpls)} 个  耗时 {ms:.1f}ms", flush=True)
    for u in units:
        print(f"   {u['kind']:5} 落点=({u['x']:.1f},{u['y']:.1f}) 等级={u['level']} "
              f"numblk={u['numblk']} rgb={u['rgb']} score={u['score']}", flush=True)
    if args.annot:
        out = annotate(rgb, units)
        Image.fromarray(out).save(args.annot)
        print(f"[标注图] {args.annot}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
