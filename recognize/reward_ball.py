"""奖励球识别: 野怪回合掉落的战利品球按颜色定位 (银/蓝/金).

背景: 击杀野怪后棋盘上会浮起若干奖励球, 点击球心即可拾取。球体为圆形发光体,
用 HSV 颜色阈值 + 连通域形状过滤定位; 三色靠饱和度/色相互斥:
蓝球高饱和青蓝, 银球低饱和青灰, 金球亮金 (预留, 待实机样本校准).

阈值标定自 images/sample/image_test__一堆蓝白色奖励球.png:
    蓝 H≈96-102 S≈170-200 V≈190-215; 银 H≈91 S≈58 V≈145.
常见混淆源均落在阈值外: 金币 V<80, 装备砧子 H≈45/S≈108, 草地 H≈32-45,
纯白文字 S<25, 血条蓝段 (长宽比非圆形).
"""

import re

import cv2
import numpy as np
from PIL import Image

from recognize import ocr, phase

# 每色 HSV 上下界 (OpenCV H 0-179); 银球与蓝球色相相近, 靠饱和度区分
COLORS: dict[str, tuple[tuple, tuple]] = {
    "blue": ((88, 130, 140), (118, 255, 255)),
    "silver": ((80, 25, 100), (105, 95, 255)),
    "gold": ((12, 150, 180), (32, 255, 255)),
}
# 颜色阈值外的 blob 级核验: {色: (高亮V界, 高亮像素占比下限)}.
# 真球是发光体 (高亮占比实测 0.29-0.67), 蓝色小怪身体 0.18, 以此排除.
BRIGHT_CHECK: dict[str, tuple[int, float]] = {"blue": (190, 0.25)}
AREA_RANGE = {"blue": (1000, 12000), "silver": (500, 6000), "gold": (1500, 12000)}
# 棋盘活动区。y 上界取 150: 顶部 HUD 的物品槽 (x1150-1340, y40-110) 是低饱和浅色块,
# 实测被 silver 阈值整块吃进来 (2026-11-14 用户指出示例图里的"球"离谱, 逐候选裁图确认:
# board_ingame.png 的 4 个球全是误检 = 2 个 HUD 物品槽 + "6人口"按钮高光 + 小小英雄高光)。
BOARD_REGION = (250, 150, 1510, 710)  # 棋盘活动区: 避开顶栏/商店/右缘火把装饰(x>1510)/备战席(y>710)
ASPECT_RANGE = (0.55, 1.8)           # 包围盒宽高比 (圆形)
MIN_FILL = {"blue": 0.4, "silver": 0.4, "gold": 0.45}  # 包围盒填充率; 金色与装备散件同色系, 圆形度加严  # 0.62 -> 0.45 (2026-10-13 实测校准, 见下)

# **亮度/饱和度二次核验 (2026-11-14 实测标定)**: 球是"被场景照亮的发光体", 而误检源是
# 界面高光与小小英雄身上的青色高亮, 两者的 V/S 分布明显不同 —— 用 blob 均值一刀切即可分开:
#   银球 (4 个真样本) V=127-132 / S=60-61 ; 误检(HUD 物品槽 V=211, "6人口"按钮 V=191, 精灵光斑 V=183)
#   蓝球 (5 个真样本) S=181-204, V=194-211 ; 误检(小小英雄高亮 S=166, V=181), 另有 S=226 的界面蓝光
SILVER_V_MAX = 175     # 银球 V 上限 (真实样本最高 132, 误检最低 183 -> 取中值偏保守)
BLUE_S_MIN = 175       # 蓝球 S 下限 (真实样本最低 181, 小小英雄高光 166)
BLUE_V_MIN = 190       # 蓝球 V 下限 (真实样本最低 194, 小小英雄高光 181)

# **金球阈值校准 (2026-10-13, 用户样例 images/examples/金色球和银色混合样本.png)**
#   原金色范围 (12,140,130)~(40,255,255) 把**草地**一起吃进来 (该图草地区 2382 个"金色"像素) ->
#   金球与草地连成 124x187 的大块 -> 填充率跌到 0.27 -> 被 MIN_FILL["gold"]=0.62 判掉, 于是
#   "金球+银球重叠"时**金球一个都不报** (模板法同期也只给 0.548 < 0.70)。
#   实测收紧到 H<=32 / S>=150 / V>=180: 草地区只剩 37 像素 (-98%), 金球独立成块
#   (846,215,67,59) 面积 2094 填充 0.53 -> 填充阈值同步放到 0.45 (银球压在金球上本就会压低填充)。
COLOR_NOTES = {"blue": "蓝球", "silver": "银球", "gold": "金球"}


# "人口提示"浮层: 棋盘中央上方那坨大号半透明蓝字 + 人形图标 (形如 "3/4"、"1/2")。
# 用户口径: 它与奖励球**同色同尺寸**, 实测颜色阈值/模板匹配/环形几何三条路的分布**完全重叠**
# (真球 disc 0.34-0.75, 浮层 0.39-0.78), 分不开; 但它带**可读数字** → 用它做正面识别再排除。
# 是否用"人口提示浮层"的 OCR 框排除同色候选。**默认关**:
# 实测它有**误伤** —— 标定样本里两块真蓝球正好压在 "4/4" 浮层上(已用标注图肉眼确认),
# 一开就被一起排掉。所以做成开关, 由你按"宁可漏 vs 宁可误"取舍。
EXCLUDE_POPULATION_OVERLAY = False
POPULATION_OCR_REGION = (700, 140, 1300, 430)
POPULATION_OCR_UPSCALE = 2
POPULATION_RE = re.compile(r"\d\s*/\s*\d")
POPULATION_PAD = (-170, -18, 45, 18)   # (左, 上, 右, 下): 往左多留, 数字左边是人形图标


def population_overlay_box(img) -> tuple | None:
    """识别"人口提示"浮层的包围盒 (整帧坐标); 没识别到返回 None.

    形如 "3/4" 的大号数字是它的指纹 —— 奖励球上没有这种文本。
    """
    x0, y0, _, _ = POPULATION_OCR_REGION
    crop = img.crop(POPULATION_OCR_REGION)
    big = crop.resize((crop.width * POPULATION_OCR_UPSCALE,
                       crop.height * POPULATION_OCR_UPSCALE), Image.LANCZOS)
    for it in ocr.recognize(big):
        if not POPULATION_RE.search(str(it.get("text") or "")):
            continue
        bx1, by1, bx2, by2 = it["box"]
        dl, du, dr, dd = POPULATION_PAD
        box = (x0 + bx1 / POPULATION_OCR_UPSCALE + dl, y0 + by1 / POPULATION_OCR_UPSCALE + du,
               x0 + bx2 / POPULATION_OCR_UPSCALE + dr, y0 + by2 / POPULATION_OCR_UPSCALE + dd)
        print(f"[reward_ball] 检出人口提示浮层 {str(it.get('text'))!r} → 排除区 "
              f"({box[0]:.0f},{box[1]:.0f},{box[2]:.0f},{box[3]:.0f})", flush=True)
        return box
    return None


def _inside_ratio(a: tuple, b: tuple) -> float:
    """a 落在 b 内的面积占比 (0~1)."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max((a[2] - a[0]) * (a[3] - a[1]), 1)
    return ix * iy / area


def _overlaps(a: tuple, b: tuple) -> bool:
    """两个矩形是否有交叠."""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def find_balls(img) -> list[dict]:
    """定位画面中所有奖励球.

    Returns:
        [{"color": "blue"/"silver"/"gold", "center": (x, y), "rect": (x1,y1,x2,y2), "area": int}, ...]
        按 (y, x) 排序, 坐标为 1920×1080 基准.
    """
    img = phase.normalize_1080(img)
    rgb = np.asarray(img.convert("RGB"))
    x0, y0, x1, y1 = BOARD_REGION
    roi = rgb[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    result = []
    for color, (lo, hi) in COLORS.items():
        mask = cv2.inRange(hsv, lo, hi)
        # **金球额外做闭运算** (2026-10-13 实测, 用户样例 金色球和银色混合样本.png):
        #   金球内部被 "?" 图案 + 高光 + 叠在上面的银球打成碎片, 只做开运算时连通块像素面积
        #   只有 1312 (< AREA_RANGE[gold] 下限 1500) 且填充 0.34 (< MIN_FILL[gold]) -> **永远检不出**,
        #   这正是"金+银重叠时两个都报不出来"的原因。闭 5x5 把碎片补成整块 (面积 1823~2094, 填充 0.53)
        #   -> 能过阈值。**只对金色做**: 对蓝/银做闭运算会把相邻的球合并成一个 (实测 vloop2 帧
        #   蓝 6->4 / 银 3->1 = 新的假阴性, 违反"宁多勿漏")。
        if color == "gold":
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        else:
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
        amin, amax = AREA_RANGE[color]
        # **2026-10-13 用户报"蓝球还是容易漏检"的根因**: 球挨近时连通域粘成一大块
        #   (实测 test-2/2-3_093712_350.png: 215x171 area=15275 = 3 个球粘一起), 而面积上限/宽高比/
        #   填充率三连过滤会把**整块丢掉** -> 里面的球全漏。这里先估"单个球典型宽度", 超过 1.45 倍
        #   就当作粘连, 用距离变换拆球 (峰值处距离值 = 该球半径)。
        _vals = [int(stats[j][2]) for j in range(1, n) if amin <= stats[j][4] <= amax]
        _single_w = max(40, int(np.median(_vals)) if _vals else 56)
        _big_w = int(_single_w * 1.45)
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            if not (amin <= area <= amax):
                # 粘连大块: 上面的三连过滤会整块丢 -> 先拆成多个球 (宁多勿漏)
                if w >= _big_w or h >= _big_w or area > amax:
                    try:
                        bm = (labels == i).astype(np.uint8)
                        dist = cv2.distanceTransform(bm, cv2.DIST_L2, 5)
                        dmax = float(dist.max())
                        dd = dist.copy()
                        _peaks = []
                        while len(_peaks) < 6:
                            _, mx, _, loc = cv2.minMaxLoc(dd)
                            if mx < max(6.0, 0.45 * dmax):
                                break
                            _peaks.append((loc[0], loc[1], float(mx)))
                            cv2.circle(dd, loc, int(mx * 1.5) + 3, 0, -1)
                        for (px, py, rr) in _peaks:
                            r = int(max(14, min(rr, 60)))
                            _a = int(3.14159 * r * r)
                            if not (amin * 0.35 <= _a <= amax * 1.2):
                                continue
                            result.append({"color": color,
                                           "center": (x0 + int(px), y0 + int(py)),
                                           "rect": (x0 + int(px) - r, y0 + int(py) - r,
                                                    x0 + int(px) + r, y0 + int(py) + r),
                                           "area": _a, "split": True})
                    except Exception:      # noqa: BLE001 - 拆分失败按普通块丢弃
                        pass
                continue
            if not (ASPECT_RANGE[0] <= w / h <= ASPECT_RANGE[1]):
                continue
            if area / (w * h) < MIN_FILL[color]:
                continue
            blob = hsv[y:y + h, x:x + w][labels[y:y + h, x:x + w] == i]
            s_mean = float(blob[:, 1].mean())
            v_mean = float(blob[:, 2].mean())
            # 亮度/饱和度二次核验: 挡住界面高光与小小英雄身上的青色高亮 (见常量注释)
            if color == "silver" and v_mean > SILVER_V_MAX:
                continue
            if color == "blue" and (s_mean < BLUE_S_MIN or v_mean < BLUE_V_MIN):
                continue
            if color in BRIGHT_CHECK:
                v_min, ratio_min = BRIGHT_CHECK[color]
                if float((blob[:, 2] >= v_min).mean()) < ratio_min:
                    continue
            result.append({"color": color,
                           "center": (x0 + x + w // 2, y0 + y + h // 2),
                           "rect": (x0 + x, y0 + y, x0 + x + w, y0 + y + h),
                           "area": area})
    if result and EXCLUDE_POPULATION_OVERLAY:   # 默认关 (会误伤压在浮层上的真球)
        overlay = population_overlay_box(img)
        if overlay is not None:
            # 只在**大部分落在浮层内**时排除 (≥50% 面积): 避免把仅仅擦到边界的好球误伤
            kept = [b for b in result if _inside_ratio(b["rect"], overlay) < 0.5]
            if len(kept) != len(result):
                print(f"[reward_ball] 排除人口提示浮层内的 {len(result) - len(kept)} 个同色误检 "
                      f"(剩余 {len(kept)} 个)", flush=True)
            result = kept
    result.sort(key=lambda b: (b["center"][1], b["center"][0]))

    return result


def annotate(img, balls: list[dict] | None = None):
    """在图上框出奖励球 (蓝/银/金三色方框+标签), 返回标注后的 PIL 图."""
    from PIL import ImageDraw, ImageFont
    from pathlib import Path
    img = phase.normalize_1080(img)
    if balls is None:
        balls = find_balls(img)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 22)
    except OSError:
        font = ImageFont.load_default()
    box_colors = {"blue": (70, 150, 255), "silver": (220, 220, 220), "gold": (255, 190, 40)}
    for b in balls:
        c = box_colors[b["color"]]
        x1, y1, x2, y2 = b["rect"]
        draw.rectangle((x1 - 4, y1 - 4, x2 + 4, y2 + 4), outline=c, width=3)
        draw.text((x1 - 4, y2 + 6), f"{COLOR_NOTES[b['color']]} {b['center']}", fill=c, font=font)
    return img
