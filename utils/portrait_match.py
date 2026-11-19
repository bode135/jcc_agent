# -*- coding: utf-8 -*-
"""棋子头像模板匹配: 透明模板库 + 去背景 + 掩码 NCC + 候选制 (用户 2026-10-21).

## 为什么不用旧的直接比像素

旧 `units/matcher.py` 的做法是: 把 104×89 的**含棋盘背景**的 RGB 模板, 直接拿
`cv2.matchTemplate(TM_CCOEFF_NORMED)` 跟同尺寸的头像 crop 比。问题是两边背景不同
(模板是另一张截图, 背景地块/草地颜色全不一样), 相关系数被背景主导 —— 实测 126 张模板
对真实帧的头像只有 0.25~0.47 分, 全库没有一个能过 0.55 的阈值。

## 现在的做法 (2026-10-21 实测有效)

三件事一起做, 缺一不可:

1. **透明模板库**: `units/templates_alpha/*.png` 是 RGBA (alpha=前景), 生成时已经
   抠掉背景 (`make_alpha` 的 GrabCut + 中心椭圆先验), 并**对齐**到统一画布:
   前景 bbox 等比缩放到长边 `CONTENT=74` 居中放进 104×89 (见 `align`)。这样模板之间的
   尺度/位置差异被抹平, 匹配退化成"同尺寸比对"。
2. **probe 也做同样处理**: 真实截图的头像也要抠背景 (同样的 `make_alpha`) + 去
   UI 带 (顶部血条/装备图标) + 对齐。**两边必须同算法**, 只对模板去背景反而更差
   (实测: 原图 vs 去背景模板 0.00, 两边都去背景才 0.55)。
3. **候选制**: 只比对 YOLO/血条给出的前 2~3 个候选名字的模板 (用户 2026-10-20:
   "我们使用 yolo, 可以直接猜测出这个 slot 的分类前 2~3, 所以不必在意模板库总量")。
   全库 126 张也能跑 (~10ms), 但候选制更快且天然排除无关模板的误命中。

实测 (2026-10-21, r1c1 的 峡谷迅捷蟹 vs 乐芙兰, 见 dev_docs/透明模板库_改造_20261021.md):

    条件                         迅捷蟹    乐最高    判定
    原图 vs 原模板 (旧 matcher)   0.247     0.288     乐芙兰胜 (旧方法错)
    两边去背景                    0.549     0.359     迅捷蟹胜
    两边去背景 + 对齐             0.562     0.343     迅捷蟹胜

## 用法

    from utils import portrait_match as pm

    # 全库识别 (内部自动抠背景 + 对齐, 一次 ~10ms)
    name, score, ranked = pm.identify(portrait_rgb)
    if name is None:
        ...   # 低于阈值, 认不出来 (宁可留 unknown, 不可猜)

    # 候选制 (YOLO/血条已经给了 2~3 个候选类名)
    name, score, ranked = pm.identify(portrait_rgb, candidates=["乐芙兰", "峡谷迅捷蟹"])

    # 只看排名 (调试/报告用)
    for nm, sc in pm.TemplateLib.shared().match(portrait_rgb, topk=5):
        print(nm, sc)

## 透明模板库从哪来

`python -m scripts.build_alpha_templates` (或 `pm.build_alpha_templates()`): 读
`units/templates/*.png`, 抠背景 + 对齐后写 `units/templates_alpha/*.png` (RGBA)。
首次调用 `identify` 时如果 alpha 库不存在会自动生成 (会慢一次, 之后只读 PNG)。

## 参数选择 (为什么是这些数)

* `CANVAS=(89,104)` / `CONTENT=74`: 与 `recognize/board.py` 的头像 crop 尺寸一致
  (PORTRAIT_HALF_W=52, PORTRAIT_H=92 → 104×89); CONTENT=74 是实测"前景长边"的合适
  尺度 —— 再大模板堆会被填满, 再小细节丢失。
* `FILL=128`: 背景填中性灰。不填 0 是为了不让 DCC 的均值被纯黑拉偏。
* `SCALES=(0.92, 0.96, 1.0, 1.04, 1.08)`: 对齐后只剩很小的尺度残差, 兜一下即可。
* `FLIP=True`: 棋盘上单位有左/右朝向, 模板只有一种; 翻转后取 max (实测对 乐芙兰
  的分数从 0.359 抬到 0.375, 对蟹 0.549→0.551, 双向都有利, 保留)。
* 默认 `mode="fill"`: 背景填灰后整图 CCOEFF (与验证脚本同一口径)。`mode="mask"`
  只在前景交集上算 Pearson, 理论更纯, 但实测对"掩码边缘锯齿"更敏感, 默认不启用。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from config.config import default_config

PROJECT_ROOT = Path(default_config.PROJECT_ROOT)

# 模板库目录: `templates` 是原始 RGB (人工裁剪, 兼容旧路径), `templates_alpha` 是
# 生成出来的透明库 (RGBA, 已对齐)。两目录文件名一一对应。
LEGACY_DIR = PROJECT_ROOT / "units" / "templates"
ALPHA_DIR = PROJECT_ROOT / "units" / "templates_alpha"

CANVAS = (89, 104)      # (h, w) 画布: 与 board.PORTRAIT_H / PORTRAIT_HALF_W 一致
CONTENT = 74            # 对齐时前景 bbox 的长边 (像素)
FILL = 128              # 背景填充灰度 (中性灰, 不拉偏均值)
# 多尺度: 透明模板**保留原始几何** (不去缩放), probe 统一对齐到前景长边 74 —— 模板前景
# 大小本来就参差 (蟹 79×75, 乐芙兰 63×82), 靠 0.9~1.5 的尺度序列兜住。
# 2026-10-21 实测 (r1c1 蟹): 只用 0.92~1.08 时蟹 0.31 / 乐芙兰 0.32; 放宽到 1.5 后
# 蟹 0.55 / 乐 0.36 —— 窄尺度序列会漏掉"模板比 probe 大一圈"的情况。
SCALES = (0.9, 1.0, 1.1, 1.25, 1.5)
MIN_PROBE_PX = 64       # 前景像素少于此值视为"没抠出东西", 不参与判定
DEFAULT_THRESHOLD = 0.55   # 与旧 matcher 默认一致; 透明库实测区间见模块 docstring

# 头像 crop 里"属于 UI, 不是棋子"的区域 (相对 104×89 的像素矩形), 抠图前先抹掉。
# 顶部 0~14 行是血条/等级牌; 左上角装备图标叠在单位头顶, 也一并抹。
PROBE_UI_RECTS = (
    (0, 0, 104, 14),        # 血条 + 等级牌
    (0, 0, 46, 40),         # 左上角装备图标 (叠在单位头顶)
)
# 模板的 UI 带 (顶部 10px / 底部 8px): 模板库里有一部分是连血条/图标一起裁的,
# 位置比 probe 更整齐 (都贴边), 统一抹掉。**不抹更多**, 免得把棋子本体削掉。
TPL_UI_BAND = ((0, 0, 0, 10), (0, -8, 0, 0))   # (x1, y1, x2, y2) 其中 0 表示"到边/到角"

# **血条残片检测** (用户 2026-10-21: "我们的 target 和 template, 可否再进行一次去除被血条
# 遮挡区域的操作? templates 中有大量星级和残存的血条, 可能会影响结果")。
#
# 实测 `units/templates` 126 张里 **27 张**含血条残片 (亮荧光绿横条, 宽 24~46 / 高 6~7),
# 位置不固定 (左贴边 x=0~12 或右侧 x=66~71), 固定矩形盖不住, 只能按"颜色 + 形状"检测:
#   * 颜色: 血条是**高饱和高亮度**的荧光绿 (H 35~85, S>=110, V>=160);
#     绿色棋子 (魔沼蛙 / 峡谷迅捷蟹) 的绿明显偏暗偏灰, V 到不了 160, 不会被误伤;
#   * 形状: 横向细条 —— 宽>=18, 高<=18, 宽高比>=2.0。
BAR_HSV_H = (35, 85)      # OpenCV H 量程是 0~180 (不是 360)
BAR_S_MIN = 110
BAR_V_MIN = 160
BAR_MIN_W = 18
BAR_MAX_H = 18
BAR_MIN_RATIO = 2.0
BAR_PAD = 3               # 命中的条外扩几个像素再抹 (把黑边/斜角一起带走)


def health_bar_rects(rgb: np.ndarray) -> list[tuple[int, int, int, int]]:
    """检测头像里的**血条残片**, 返回要抹掉的矩形列表 (已外扩 `BAR_PAD`)。

    宁可少抹: 检测不到就返回 [] (只是保持原样), 绝不因为"想抹干净"把棋子本体削掉。
    """
    h, w = rgb.shape[:2]
    if h < 12 or w < 12:
        return []
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = ((hue >= BAR_HSV_H[0]) & (hue <= BAR_HSV_H[1])
            & (sat >= BAR_S_MIN) & (val >= BAR_V_MIN)).astype(np.uint8) * 255
    if not mask.any():
        return []
    # 横向开运算: 只留"够长的横条", 打散零星绿色高光
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, 9), np.uint8))
    n, _lab, st, _c = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    rects: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x, y, bw, bh = int(st[i, 0]), int(st[i, 1]), int(st[i, 2]), int(st[i, 3])
        if bw < BAR_MIN_W or bh > BAR_MAX_H or bw / max(1, bh) < BAR_MIN_RATIO:
            continue
        rects.append((max(0, x - BAR_PAD), max(0, y - BAR_PAD),
                      min(w, x + bw + BAR_PAD), min(h, y + bh + BAR_PAD)))
    return rects


# ------------------------------------------------------------------ 抠图
def _bg_centers(ring: np.ndarray, k: int = 6) -> np.ndarray:
    """确定性地估 k 个背景色中心 (四边像素颜色量化后取最多的 k 个颜色格).

    **不能用 cv2.kmeans**: 它带 KMEANS_PP_CENTERS 随机初始化, 同一张图两次调用可能给出
    不同背景簇 → 同一个 probe 两次匹配分数能差 0.07 (实测 r1c1 蟹: 0.333 / 0.402)。
    模板匹配是要写进采集链路的, 分数必须可复现。
    """
    q = (ring // 16).astype(np.uint8)
    uniq, counts = np.unique(q, axis=0, return_counts=True)
    order = np.argsort(-counts)[:k]
    return (uniq[order].astype(np.float32) * 16.0 + 8.0)


def make_alpha(rgb: np.ndarray, iters: int = 2) -> np.ndarray:
    """GrabCut(中心椭圆先验 + 背景色先验) 抠前景, 返回 0/255 的 uint8 掩码.

    ``iters`` 默认 **2** (用户 2026-11-19 提速): 实测同一头像 iters=2 与 iters=10 的
    前景像素 3504 vs 3503, 匹配 top3 分数**完全相同** (0.527 / 0.444 / 0.438),
    而耗时 31ms vs 125ms —— 10 轮迭代在 2 轮后早已收敛, 多出来的 8 轮纯浪费。

    做法 (踩过的坑都在注释里):
      * 四边 3px 一律 GC_BGD —— 头像 crop 的边框一定是背景;
      * 中心椭圆 (w*0.44 × h*0.44, 中心在 55% 高) 给"软前景", 内嵌小椭圆 (0.22) 给"硬前景";
      * 椭圆内再按"到背景色簇 (四边像素量化取 top6) 的 Lab 距离"细分:
        距离 <10 退回软背景, >32 抬成软前景 —— 只靠椭圆会把压在椭圆里的草地也留下;
      * **椭圆外一律 GC_PR_BGD**: 早先版本让"离背景色远"的草地被拽成前景, 结果蟹模板
        (青绿蟹 vs 青灰地块, Lab 距离只有 7~12) 被抠碎, 而乐芙兰 (棕/白/亮绿) 完整保留
        —— 这就是"去背景反而更差"的真正原因, 不是算法不行而是掩码有偏置。
    """
    h, w = rgb.shape[:2]
    # GrabCut 内部用 kmeans 初始化 GMM, 而 OpenCV 的 kmeans 默认用全局 RNG —— 不固定种子
    # 同一张图两次抠出来的掩码能不一样 (实测同一 probe: 候选制 0.538 / 全库 0.395)。
    # 模板匹配要写进采集链路, 分数必须可复现。
    cv2.setRNGSeed(0)
    ring = np.concatenate([
        rgb[:4].reshape(-1, 3), rgb[-4:].reshape(-1, 3),
        rgb[:, :4].reshape(-1, 3), rgb[:, -4:].reshape(-1, 3),
    ]).astype(np.uint8)
    centers = _bg_centers(ring)
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    c_lab = cv2.cvtColor(centers.reshape(-1, 1, 3).astype(np.uint8),
                         cv2.COLOR_RGB2LAB).reshape(-1, 3).astype(np.float32)
    d = np.linalg.norm(lab[:, :, None, :] - c_lab[None, None, :, :], axis=3).min(axis=2)

    gc = np.full((h, w), cv2.GC_PR_BGD, np.uint8)
    b = 3
    gc[:b, :] = gc[-b:, :] = cv2.GC_BGD
    gc[:, :b] = gc[:, -b:] = cv2.GC_BGD
    ell = np.zeros((h, w), np.uint8)
    cv2.ellipse(ell, (w // 2, int(h * 0.55)), (int(w * 0.44), int(h * 0.44)), 0, 0, 360, 1, -1)
    cv2.ellipse(ell, (w // 2, int(h * 0.55)), (int(w * 0.22), int(h * 0.22)), 0, 0, 360, 2, -1)
    gc[ell == 2] = cv2.GC_FGD
    gc[ell == 1] = cv2.GC_PR_FGD
    gc[(ell == 1) & (d < 10)] = cv2.GC_PR_BGD
    gc[(ell == 1) & (d > 32)] = cv2.GC_PR_FGD

    gcimg = np.ascontiguousarray(rgb.astype(np.uint8))
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    cv2.grabCut(gcimg, gc, None, bgd, fgd, iters, cv2.GC_INIT_WITH_MASK)
    m = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    k = np.ones((3, 3), np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    n, labcc, st, _ = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
    keep = np.zeros_like(m)
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] >= 40:
            keep[labcc == i] = 255
    return keep


def wipe_rects(rgb: np.ndarray, alpha: np.ndarray, rects) -> None:
    """把 UI 区域 (血条/装备图标) 从图里抹掉: 像素填灰 + 掩码清零 (原地)."""
    for x1, y1, x2, y2 in rects:
        rgb[y1:y2, x1:x2] = FILL
        alpha[y1:y2, x1:x2] = 0


def prepare_probe(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """处理一张**待识别头像**: 抹 UI → 抠背景 → 对齐. 返回 (RGB(灰底), alpha).

    注意顺序: 先抹 UI 再抠图 —— 否则血条/装备图标会被 GrabCut 当成前景的一部分,
    掩码 bbox 被撑大, 对齐就偏了。

    **只对 probe 做对齐** (模板保留原始几何): probe 的对齐把"前景长边"统一到 74px,
    模板那侧的尺度差异交给 `SCALES` 序列兜。两边都对齐会把模板的原始比例也压平,
    实测反而把 r1c1 的蟹从 0.55 拉到 0.31 (2026-10-21)。

    顺序必须是 **先抠背景、再抹 UI**: GrabCut 的种子点来自原图像素, 先抹成纯灰反而
    会让"血条/装备块"周围的边界判错 (实测先抹后抠比先抠后抹低 0.13 分)。
    """
    img = np.array(rgb, dtype=np.uint8, copy=True)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    img = img[:, :, :3]
    alpha = make_alpha(img)
    wipe_rects(img, alpha, PROBE_UI_RECTS)
    # 顶部 14 行只盖住血条的一部分, 残片 (尤其侧边贴条) 还要单独抹 —— 否则会被 GrabCut
    # 当前景, 对齐时把前景 bbox 撑大, 匹配直接偏。
    wipe_rects(img, alpha, health_bar_rects(img))
    return align_to_canvas(img, alpha)


def prepare_template(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """处理一张**模板**: 抠背景 → 抹上下 UI 带. 返回 (RGB(灰底), alpha), **保持原始几何**.

    不缩放: 模板库是人工裁剪的, 前景大小反映了"棋子在该裁剪框里的真实占比",
    这个比例本身是身份信息 (蟹扁而宽, 乐芙兰细高)。统一缩放到同一长边会把这条信息丢掉。
    """
    img = np.array(rgb, dtype=np.uint8, copy=True)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    img = img[:, :, :3]
    alpha = make_alpha(img)
    h, w = img.shape[:2]
    wipe_rects(img, alpha, ((0, 0, w, 10), (0, max(0, h - 8), w, h)))
    # 血条残片 (实测 126 张模板里 27 张有): 亮绿横条贴着两侧, 上面的固定带盖不住。
    # 不抹的话它会被 GrabCut 算成前景, 把 bbox 撑大 → 匹配时尺度和位置都错位。
    wipe_rects(img, alpha, health_bar_rects(img))
    return img, alpha


def align_to_canvas(rgb: np.ndarray, alpha: np.ndarray,
                    content: int = CONTENT, canvas: tuple[int, int] = CANVAS
                    ) -> tuple[np.ndarray, np.ndarray]:
    """前景 bbox 等比缩到长边 content, 居中放进 canvas; 背景填灰, 掩码清零.

    这一步是"透明模板能工作"的关键: 模板各有各的裁偏 (同一棋子的 v1~v5 之间原本只
    0.03~0.92 分, 全因为裁偏), 对齐后尺度/位置差异被抹平, 才能比出真实相似度。
    """
    ch, cw = canvas
    ys, xs = np.where(alpha > 0)
    if len(xs) < 20:
        out = np.full((ch, cw, 3), FILL, np.uint8)
        outm = np.zeros((ch, cw), np.uint8)
        h, w = min(rgb.shape[0], ch), min(rgb.shape[1], cw)
        out[:h, :w] = rgb[:h, :w]
        outm[:h, :w] = (alpha[:h, :w] > 0).astype(np.uint8)
        out[outm == 0] = FILL
        return out, outm
    x1, x2 = int(xs.min()), int(xs.max())
    y1, y2 = int(ys.min()), int(ys.max())
    crop = rgb[y1:y2 + 1, x1:x2 + 1]
    cm = alpha[y1:y2 + 1, x1:x2 + 1]
    h, w = crop.shape[:2]
    s = min(content / h, content / w)
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    ci = cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA)
    cmr = cv2.resize(cm, (nw, nh), interpolation=cv2.INTER_NEAREST)
    out = np.full((ch, cw, 3), FILL, np.uint8)
    om = np.zeros((ch, cw), np.uint8)
    ox, oy = (cw - nw) // 2, (ch - nh) // 2
    out[oy:oy + nh, ox:ox + nw] = ci
    om[oy:oy + nh, ox:ox + nw] = (cmr > 0).astype(np.uint8)
    out[om == 0] = FILL
    return out, om


# ------------------------------------------------------------------ 打分
def masked_ncc(rgb_a: np.ndarray, rgb_b: np.ndarray,
               mask_a: np.ndarray, mask_b: np.ndarray) -> float:
    """只在前景交集上算 Pearson 相关 (mode="mask")."""
    inter = (mask_a > 0) & (mask_b > 0)
    if int(inter.sum()) < MIN_PROBE_PX:
        return 0.0
    a = rgb_a[inter].astype(np.float32)
    b = rgb_b[inter].astype(np.float32)
    a = a - a.mean()
    b = b - b.mean()
    den = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if den < 1e-6:
        return 0.0
    return float((a * b).sum() / den)


def _ncc_fill(rgb_a: np.ndarray, rgb_b: np.ndarray) -> float:
    """背景已填灰的匹配: `matchTemplate(大图, 小模板).max()` (a 必须不小于 b).

    早先版本要求两者形状完全相同 —— 于是多尺度（模板缩到 0.9 后比 probe 小）那一支全
    返回 0, 只剩 1.0 尺度有效, 分数从 0.38 掉到 0.33。滑窗本来就是 matchTemplate 的本事,
    只要保证 "图 >= 模板" 即可。
    """
    if rgb_a.shape[0] < rgb_b.shape[0] or rgb_a.shape[1] < rgb_b.shape[1]:
        return 0.0
    res = cv2.matchTemplate(rgb_a, rgb_b, cv2.TM_CCOEFF_NORMED)
    return float(np.nan_to_num(res, nan=0.0).max())


def score_pair(probe_rgb: np.ndarray, probe_mask: np.ndarray,
               tpl_rgb: np.ndarray, tpl_mask: np.ndarray,
               *, flip: bool = True, scales=None, mode: str = "mask") -> float:
    """一张 probe 对一张模板的相似度 (多尺度 + 可选左右镜像, 取最高).

    mode="mask" (默认): 两边都已 `align_to_canvas` 到同一画布, 只在前景交集上算 Pearson
    相关 —— 背景不参与, 是区分"同色不同形"的关键; 尺寸已归一, 不再做多尺度 (缩放只会扭曲).
    mode="fill": 背景填灰后的整图 CCOEFF 滑窗, 多尺度.
    """
    if scales is None:
        scales = (1.0,) if mode == "mask" else SCALES
    best = -1.0
    h, w = tpl_rgb.shape[:2]
    for s in scales:
        if abs(s - 1.0) < 1e-6:
            t_rgb, t_mask = tpl_rgb, tpl_mask
        else:
            th, tw = max(1, int(round(h * s))), max(1, int(round(w * s)))
            t_rgb = cv2.resize(tpl_rgb, (tw, th), interpolation=cv2.INTER_AREA)
            t_mask = cv2.resize(tpl_mask, (tw, th), interpolation=cv2.INTER_NEAREST)
        for t0 in (t_rgb, np.ascontiguousarray(t_rgb[:, ::-1])) if flip else (t_rgb,):
            tm0 = t_mask if t0 is t_rgb else np.ascontiguousarray(t_mask[:, ::-1])
            if mode == "mask":
                v = masked_ncc(probe_rgb, t0, probe_mask, tm0)
            else:
                v = _ncc_any(probe_rgb, t0)
            if v > best:
                best = v
    return max(0.0, best)


def _ncc_any(a: np.ndarray, b: np.ndarray) -> float:
    """背景已填灰的两块图: 同尺寸直接比, 不同尺寸让大的当图、小的当模板滑窗."""
    if a.shape == b.shape:
        return _ncc_fill(a, b)
    if a.shape[0] >= b.shape[0] and a.shape[1] >= b.shape[1]:
        return _ncc_fill(a, b)
    if b.shape[0] >= a.shape[0] and b.shape[1] >= a.shape[1]:
        return _ncc_fill(b, a)
    return 0.0


# ------------------------------------------------------------------ 模板库
class TemplateLib:
    """透明模板库 (单例). 首次加载时读 `units/templates_alpha/*.png`; 库不存在就现场生成."""

    _shared: "TemplateLib | None" = None

    def __init__(self, alpha_dir: Path | str | None = None,
                 legacy_dir: Path | str | None = None):
        self.alpha_dir = Path(alpha_dir or ALPHA_DIR)
        self.legacy_dir = Path(legacy_dir or LEGACY_DIR)
        # name -> [(rgb, alpha)]; 一个名字可能有多个变体 (文件名 "__v2" 等)
        self._by_name: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}
        self._variants: list[tuple[str, np.ndarray, np.ndarray]] = []
        self._loaded = False
        self.failed: list[str] = []

    # ---- 单例
    @classmethod
    def shared(cls) -> "TemplateLib":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    @classmethod
    def reload(cls) -> "TemplateLib":
        """丢弃缓存重建 (改完模板库/换目录时用)."""
        cls._shared = cls()
        return cls._shared

    # ---- 加载
    def load(self) -> "TemplateLib":
        if self._loaded:
            return self
        if not self.alpha_dir.is_dir() or not list(self.alpha_dir.glob("*.png")):
            # 透明库还没生成: 现场从原始库生成一次 (慢, 但只一次)
            build_alpha_templates(self.legacy_dir, self.alpha_dir)
        files = sorted(self.alpha_dir.glob("*.png"))
        for path in files:
            try:
                rgb, alpha = load_rgba(path)
            except Exception as exc:  # noqa: BLE001 - 单张模板坏了不该带崩全库
                self.failed.append(path.name)
                print(f"[模板库] 读不出模板, 跳过: {path.name} ({exc!r})")
                continue
            if int((alpha > 0).sum()) < MIN_PROBE_PX:
                self.failed.append(path.name)
                continue
            # 加载即对齐: 抹平模板之间的裁偏/尺度差异 (同一棋子的 v1~v5 原本只 0.03~0.92 分),
            # 这样 mode="mask" 可以直接在统一画布上比前景交集。
            rgb, alpha = align_to_canvas(rgb, alpha)
            name = path.stem.split("__")[0]     # 多变体命名约定: "名字__v2.png"
            self._by_name.setdefault(name, []).append((rgb, alpha))
            self._variants.append((name, rgb, alpha))
        self._loaded = True
        return self

    @property
    def names(self) -> list[str]:
        self.load()
        return sorted(self._by_name)

    @property
    def n_variants(self) -> int:
        self.load()
        return len(self._variants)

    @property
    def variants(self) -> list[tuple[str, np.ndarray, np.ndarray]]:
        """全部 (名字, 对齐后RGB, 掩码) 变体 —— 给自测脚本两两互测用."""
        self.load()
        return list(self._variants)

    # ---- 匹配
    def match(self, portrait, *, candidates: list[str] | None = None,
              flip: bool = True, scales=None, mode: str = "mask",
              topk: int | None = None) -> list[tuple[str, float]]:
        """把一个头像跟模板库比, 返回 [(名字, 分数)] 按分数降序.

        ``candidates`` 给定则只比这些名字的模板 (候选制, 用户 2026-10-20 要求):
        YOLO/血条先给出前 2~3 个候选类, 模板匹配只在候选里挑 —— 既快又不会被无关模板误命中。
        """
        self.load()
        probe_rgb, probe_mask = prepare_probe(to_rgb(portrait))
        if int((probe_mask > 0).sum()) < MIN_PROBE_PX:
            return []
        if mode == "mask":
            probe_rgb, probe_mask = align_to_canvas(probe_rgb, probe_mask)
        pool = self._variants
        if candidates:
            want = {str(c).split("__")[0] for c in candidates}
            pool = [v for v in self._variants if v[0] in want]
        best: dict[str, float] = {}
        for name, t_rgb, t_mask in pool:
            s = score_pair(probe_rgb, probe_mask, t_rgb, t_mask,
                           flip=flip, scales=scales, mode=mode)
            if s > best.get(name, -1.0):
                best[name] = s
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        return ranked if topk is None else ranked[:topk]

    def identify(self, portrait, *, candidates: list[str] | None = None,
                 threshold: float | None = None, topk: int = 3,
                 flip: bool = True, scales=None, mode: str = "mask"
                 ) -> tuple[str | None, float, list[tuple[str, float]]]:
        """高层入口: 返回 (名字|None, 最高分, 全部排名).

        低于 ``threshold`` (默认 0.55) 一律返回名字 None —— **宁可留 unknown, 不可猜名字**
        (数据正确性最高优先级: 错标签比漏标更糟)。
        """
        if threshold is None:
            threshold = DEFAULT_THRESHOLD
        ranked = self.match(portrait, candidates=candidates, flip=flip,
                            scales=scales, mode=mode)
        if not ranked:
            return None, 0.0, []
        name, score = ranked[0]
        return (name if score >= threshold else None), round(float(score), 3), \
            [(n, round(float(s), 3)) for n, s in ranked[:max(topk, 1)]]


# ------------------------------------------------------------------ 透明库生成
def load_rgba(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """读 RGBA 透明模板 → (RGB(背景已填 FILL), alpha 0/1).

    **背景要填 FILL**: 磁盘上的透明模板保留原始背景像素 (方便人眼看/复用), 但匹配时如果
    模板背景是"原图棋盘色"而 probe 背景是 FILL 灰, CCOEFF 就被背景色差吃掉 ——
    实测同一对图: 模板背景不填灰 0.215 vs 两边都填灰 0.305。
    """
    arr = np.array(Image.open(path).convert("RGBA"))
    alpha = (arr[:, :, 3] > 127).astype(np.uint8)
    rgb = np.ascontiguousarray(arr[:, :, :3])
    rgb = np.where(alpha[:, :, None] > 0, rgb, FILL).astype(np.uint8)
    return rgb, alpha


def save_rgba(path: Path, rgb: np.ndarray, alpha: np.ndarray) -> None:
    """写 RGBA 透明模板: alpha 二值化 (0/255), 背景像素不动 (靠 alpha 透明)."""
    out = np.dstack([rgb, np.where(alpha > 0, 255, 0).astype(np.uint8)])
    Image.fromarray(out, mode="RGBA").save(path)


def build_alpha_templates(legacy_dir: Path | str | None = None,
                          alpha_dir: Path | str | None = None,
                          *, overwrite: bool = False, verbose: bool = True
                          ) -> tuple[int, int, list[str]]:
    """把 `units/templates/*.png` 批量转成透明(对齐)模板库.

    Returns:
        (成功数, 跳过数, 失败文件名列表)
    """
    legacy = Path(legacy_dir or LEGACY_DIR)
    alpha_dir_path = Path(alpha_dir or ALPHA_DIR)
    alpha_dir_path.mkdir(parents=True, exist_ok=True)
    files = sorted(legacy.glob("*.png"))
    ok = skipped = 0
    failed: list[str] = []
    try:
        from tqdm import tqdm
    except Exception:  # noqa: BLE001 - 没有 tqdm 也要能跑
        tqdm = None
    it = tqdm(files, desc="生成透明模板", ncols=80) if (tqdm and verbose) else files
    for path in it:
        dst = alpha_dir_path / path.name
        if dst.exists() and not overwrite:
            skipped += 1
            continue
        try:
            rgb = np.array(Image.open(path).convert("RGB"))
            t_rgb, t_mask = prepare_template(rgb)
            if int((t_mask > 0).sum()) < MIN_PROBE_PX:
                failed.append(path.name)
                continue
            save_rgba(dst, t_rgb, t_mask)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - 单张失败不影响整体
            failed.append(path.name)
            if verbose:
                print(f"[透明模板] 失败: {path.name} ({exc!r})")
    if verbose:
        print(f"[透明模板] 生成完成: 新增 {ok} 张, 已存在跳过 {skipped} 张, "
              f"失败 {len(failed)} 张 → {alpha_dir_path}")
    return ok, skipped, failed


# ------------------------------------------------------------------ 便捷入口
def to_rgb(img) -> np.ndarray:
    """PIL Image / ndarray → RGB ndarray (与 portrait 同口径)."""
    if isinstance(img, Image.Image):
        return np.asarray(img.convert("RGB"))
    arr = np.asarray(img)
    if arr.ndim == 2:
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2RGB)
    return arr[:, :, :3]


def _portrait_crop(arr: np.ndarray, bar) -> np.ndarray | None:
    """血条下方头像区域 (与 recognize/board.py 的裁剪口径一致)."""
    x1, y1, x2, y2 = (int(v) for v in bar)
    cx = (x1 + x2) // 2
    y0 = y2 + 3
    crop = arr[y0:y0 + CANVAS[0], cx - CANVAS[1] // 2:cx + CANVAS[1] // 2]
    return crop if crop.size else None


def identify(portrait, *, candidates: list[str] | None = None,
             threshold: float | None = None, topk: int = 3,
             flip: bool = True, scales=None, mode: str = "mask"
             ) -> tuple[str | None, float, list[tuple[str, float]]]:
    """模块级便捷入口, 用共享模板库 (见 `TemplateLib.identify`)."""
    return TemplateLib.shared().identify(
        portrait, candidates=candidates, threshold=threshold, topk=topk,
        flip=flip, scales=scales, mode=mode)
