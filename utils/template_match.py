"""模板匹配公共工具: 多尺度 NCC 匹配 + **统一阈值表**.

背景 (实机事故): `cv2.matchTemplate(TM_CCOEFF_NORMED)` 的调用散落在 11 个文件、19 处, 每处
自己写缩放序列/预筛/阈值 —— 出问题时既看不清口径, 也没法"一处调阈值、全局生效"。
本模块把匹配过程与**阈值**收拢: 阈值只在上面的 `THRESHOLDS` 里定义, 调用方通过
`threshold(name)` 读取, 想改就改这一处 (或运行时 `set_threshold`)。

实测参考 (2026-10-07, run_1007__090902 / run_1007__082426):
    顶栏双剑 (phase_swords): 真 prep 0.891~0.896; 但真剑图标被压暗/被装饰遮挡时低到 ~0.67;
                             假阳性 (海克斯符文图标、金色卡牌图标、平坦暗块) 0.52~0.80 —— **区间重叠**,
                             所以单靠阈值必然有取舍 (宁可漏真 prep 还是宁可放行假阳性)。
    小眼睛 (augment_eye): 浮层未关 0.998 / 已选完 0.292 —— 分得很开, 0.8 很稳。
    顶栏卡牌 (phase_cards): 海克斯回合 >0.98 / 普通回合 <0.75。

用法:
    from utils import template_match as tm
    tpl = tm.load_template("recognize/templates/phase_swords.png")
    hits = tm.match(rgb, tpl, region=(764, 0, 1182, 87), scales=tm.SWORD_SCALES,
                    prefilter_hsv=tm.YELLOW_HSV, prefilter_min_px=100)
    if hits and hits[0]["score"] >= tm.threshold("phase_swords"):
        ...
"""

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from config.config import default_config

# 项目根走 config (2026-10-18 exe 打包: 打包后 `__file__` 在解包目录, 不能用它推项目根)。
PROJECT_ROOT = default_config.PROJECT_ROOT

# ---------------------------------------------------------------- 唯一阈值配置点
# 改这里 = 改所有调用方的判定门槛 (调用方统一用 threshold("名字") 读取, 不再各写常量)。
THRESHOLDS: dict[str, float] = {
    "phase_swords": 0.85,   # 顶栏双剑 (prep 标记)。0.55→0.80: 实机 2-3 战斗帧的符文图标拿
                            # 0.570 越过 0.55 → 误判 prep → 战斗中点棋盘 (swap 被放行)。
                            # [!] 代价: 真剑被压暗时会低到 ~0.67 (实测 3-7 帧 0.661~0.675),
                            # 那条会改判 combat → 该准备回合不操作。取舍见文件头说明。
    "phase_cards": 0.90,    # 顶栏金色双卡牌图标 (海克斯回合标记)
    "augment_eye": 0.85,    # 海克斯浮层"小眼睛" (在=浮层开着 / 不在=已选完)
    "augment_refresh": 0.60,  # 海克斯三选一"刷新按钮"⟳ (用户 m00351 指定, 强于小眼睛)。
                              # 只有浮层开着才有这个按钮 → 在=未选 / 不在=已选完。
                              # 实测 2026-11-14 (1920×1080, recognize/templates/augment_refresh.png):
                              # 浮层开着 1.000/0.991/0.997, 浮层已关 0.28~0.31 → 0.60 居中偏保守。
                              # 比 augment_eye 可靠: 小眼睛会被 Windows 系统通知/弹窗盖住(3-2 卡死根因),
                              # 刷新按钮在卡片下方, 不受底部遮挡影响。
    "population_helm": 0.65,   # 人口头盔图标 (population_helm_{blue,grey}.png)
                               # 实测 prep 0.649~1.000 / 战斗 0.05~0.575 → 0.65 可分开
    "population_digit": 0.80,  # 人口区域 OCR 置信度下限 (原 from_digit 里的硬编码 0.8)
    "bench_full_toast": 0.70,  # 备战席满员弹窗 "您的备战席已满！…" (recognize/bench_full.py)。
                               # 实测 2026-10-13: 示例图 score=1.000 / 94 张数据集帧 中位 0.114
                               # 最大 0.148 → 0.70 余量极大 (弹窗半透明, 留余量抵背景差异)。
}

# 常用缩放序列 (按模板原始尺寸的比例)
SWORD_SCALES = (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.4)
CARD_SCALES = (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15)
FINE_SCALES = tuple(round(0.60 + 0.05 * i, 3) for i in range(12))  # 0.60~1.15 (诊断用细网格)

# "亮色预筛" 默认色域 (HSV, cv2 口径) —— 与 phase 原先的亮黄判据一致
YELLOW_HSV = ((18, 110, 140), (38, 255, 255))
BRIGHT_V_MIN = 140      # 形态统计里"亮像素"的 V 门槛


def threshold(name: str) -> float:
    """取统一阈值; 未登记的名字直接报错 (防止悄悄用了一个没人管的常量)。"""
    if name not in THRESHOLDS:
        raise KeyError(f"未登记的阈值: {name!r} (可选 {sorted(THRESHOLDS)})")
    return float(THRESHOLDS[name])


def set_threshold(name: str, value: float) -> None:
    """运行时改阈值 (调试/实机灰度用); 改了会打印一行, 便于日志回溯。"""
    if name not in THRESHOLDS:
        raise KeyError(f"未登记的阈值: {name!r}")
    old = THRESHOLDS[name]
    THRESHOLDS[name] = float(value)
    if old != value:
        print(f"[template_match] 阈值 {name}: {old} → {value}")


def load_template(path) -> np.ndarray:
    """读模板为 RGB ndarray (cv2.imread 不支持中文路径, 统一走 PIL)."""
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        raise FileNotFoundError(f"模板不存在: {p}")
    return np.array(Image.open(p).convert("RGB"))


def bright_windows(rgb: np.ndarray, hsv_lo, hsv_hi, min_px: int, pad: int = 20,
                   min_area: int = 40, merge_gap: int = 40) -> list[tuple]:
    """把"亮色连通域"合并成若干搜索窗 (只在窗内做匹配, 等价于 phase 原先的亮窗预筛).

    合并语义**原样照搬** phase 原来的 `_yellow_windows` (含 merge_gap 的相邻判据与遍历顺序),
    这样把 phase 切到本工具后, 分数与切换前逐帧一致 —— 只改阈值, 不动其它行为。

    返回 [(x0, y0, x1, y1)] (rgb 坐标); 亮像素总量不足 min_px 时返回空表。
    """
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    mask = cv2.inRange(hsv, tuple(hsv_lo), tuple(hsv_hi))
    if int(cv2.countNonZero(mask)) < min_px:
        return []
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
    boxes = [tuple(int(v) for v in stats[i][:4]) for i in range(1, n) if stats[i, 4] >= min_area]
    merged: list[list[int]] = []
    for x, y, w, h in boxes:
        for m in merged:
            near = (x < m[0] + m[2] + merge_gap and x + w + merge_gap > m[0]
                    and y < m[1] + m[3] + merge_gap and y + h + merge_gap > m[1])
            if near:
                nx1, ny1 = min(m[0], x), min(m[1], y)
                nx2, ny2 = max(m[0] + m[2], x + w), max(m[1] + m[3], y + h)
                m[0], m[1], m[2], m[3] = nx1, ny1, nx2 - nx1, ny2 - ny1
                break
        else:
            merged.append([x, y, w, h])
    h_img, w_img = rgb.shape[:2]
    return [(max(0, x0 - pad), max(0, y0 - pad), min(w_img, x0 + w + pad), min(h_img, y0 + h + pad))
            for x0, y0, w, h in merged]


def match(rgb: np.ndarray, tpl: np.ndarray, *, region=None, scales=FINE_SCALES,
          topk: int = 1, nms_px: int = 20, prefilter_hsv=None, prefilter_min_px: int = 100,
          prefilter_pad: int = 20) -> list[dict]:
    """多尺度模板匹配, 返回按分数降序的前 topk (位置去重).

    Args:
        rgb: RGB 截图 (ndarray).
        tpl: 模板 (RGB ndarray, 通常来自 load_template).
        region: 只在 (x0,y0,x1,y1) 内搜索; None = 全图.
        scales: 缩放序列.
        prefilter_hsv: (lo, hi) 给定则先做"亮色预筛", 只在亮窗内匹配 (phase 的剑形就是这套).
        topk / nms_px: 取前几名 + 相邻命中去重距离 (同处只留最高分).

    Returns:
        [{"score", "loc", "size", "scale", "stats"}] —— stats 是命中图块的亮度/色相统计,
        仅作**诊断**用 (本模块不据它判定; 阈值只看 score)。
    """
    x0, y0, x1, y1 = region or (0, 0, rgb.shape[1], rgb.shape[0])
    if prefilter_hsv:
        # 在 **region 内部**算亮窗 (不是整图算完再筛): 与 phase 原实现同语义, 避免
        # 跨 region 边界的连通域把窗口撑大 —— 保证"换成公共工具后分数逐帧不变"。
        local = bright_windows(rgb[y0:y1, x0:x1], *prefilter_hsv, prefilter_min_px, prefilter_pad)
        sub_regions = [(a + x0, b + y0, c + x0, d + y0) for a, b, c, d in local]
    else:
        sub_regions = [(x0, y0, x1, y1)]
    hits: list[dict] = []
    for sx0, sy0, sx1, sy1 in sub_regions:
        roi = rgb[sy0:sy1, sx0:sx1]
        for scale in scales:
            t = cv2.resize(tpl[:, :, ::-1], None, fx=scale, fy=scale)[:, :, ::-1]  # BGR 缩放
            if t.shape[0] >= roi.shape[0] or t.shape[1] >= roi.shape[1]:
                continue
            res = np.nan_to_num(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED), nan=0.0)
            for _ in range(topk):
                _, mx, _, loc = cv2.minMaxLoc(res)
                if mx <= 0:
                    break
                gx, gy = sx0 + loc[0], sy0 + loc[1]
                tw, th = t.shape[1], t.shape[0]
                hits.append({"score": float(mx), "loc": (gx, gy), "size": (tw, th),
                             "scale": float(scale),
                             "stats": patch_stats(rgb[gy:gy + th, gx:gx + tw])})
                res[max(0, loc[1] - nms_px):loc[1] + nms_px,
                    max(0, loc[0] - nms_px):loc[0] + nms_px] = -1.0   # 抹掉已取峰附近
    hits.sort(key=lambda d: d["score"], reverse=True)
    kept: list[dict] = []
    for hit in hits:
        if all(abs(hit["loc"][0] - k["loc"][0]) > nms_px
               or abs(hit["loc"][1] - k["loc"][1]) > nms_px for k in kept):
            kept.append(hit)
        if len(kept) >= topk:
            break
    return kept


def best_score(rgb: np.ndarray, tpl: np.ndarray, **kw) -> float:
    """只取最高分 (调用方只关心"像不像"时的便捷入口); 无命中返回 0.0。"""
    kw.pop("topk", None)
    hits = match(rgb, tpl, topk=1, **kw)
    return hits[0]["score"] if hits else 0.0


def patch_stats(patch: np.ndarray) -> dict:
    """命中图块的形态统计 (亮占比 / 色相std / 黄占比 / 饱和度) —— 仅供人工诊断。"""
    if patch.size == 0:
        return {"bright": 0.0, "hue_std": 0.0, "yellow": 0.0, "sat": 0.0}
    hsv = cv2.cvtColor(patch, cv2.COLOR_RGB2HSV)
    h, s, v = (hsv[:, :, i].astype("float32") for i in range(3))
    bright = v > BRIGHT_V_MIN
    if not bright.any():
        return {"bright": 0.0, "hue_std": 0.0, "yellow": 0.0, "sat": 0.0}
    yellow = (h >= 15) & (h <= 40) & (s > 90) & (v > BRIGHT_V_MIN)
    return {"bright": float(bright.mean()),
            "hue_std": float(np.std(h[bright])),
            "yellow": float(yellow[bright].mean()),
            "sat": float(s[bright].mean())}

