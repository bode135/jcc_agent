"""海克斯选择界面: 三张强化符文卡的信息抽取 (名称/描述/剩余刷新次数).

界面布局 (1920×1080, 见 images/sample/选择海克斯界面.png):
    三张卡等距排布 (中心间距约 505px), 每张卡上方为特质图标, 中部为标题+描述,
    卡下方是蓝色刷新按钮 (刷新图标 + 剩余次数数字).

判定: 综合判定在本模块 `judge_augment_screen()` 内实现
(刷新按钮 > 小眼睛 > 卡牌图标+棋盘绿度, 见该函数 docstring).
已选判定三条独立证据:
    1. 每张卡下方的「刷新按钮」(⟳) 还在 = 浮层开着, 尚未选择 (最硬, 用户 m00351 指定);
    2. 屏幕底部中央"小眼睛"图标还在 = 尚未选择 (会被系统通知/弹窗遮挡, 不可单用);
    3. 两者都消失 + 棋盘清晰可见 = 已完成选择.
"""

import re
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from recognize import ocr, phase
from utils import template_match as tm

# 三张卡的文字区域 (标题+描述) 与刷新按钮数字区域, x 中心分别约 456/960/1464
CARD_TEXT_REGIONS = (
    (285, 340, 632, 680),
    (790, 340, 1136, 680),
    (1295, 340, 1641, 680),
)
REFRESH_DIGIT_REGIONS = (
    (443, 704, 491, 763),
    (946, 704, 994, 763),
    (1449, 704, 1497, 763),
)
EYE_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "augment_eye.png"
EYE_SEARCH_REGION = (870, 900, 1070, 1020)  # 屏幕底部中央"小眼睛"搜索窗 (实测 box 917,933-1016,988)
# 阈值统一放在 utils/template_match.THRESHOLDS["augment_eye"] (唯一配置点)
EYE_SCORE_MIN = tm.threshold("augment_eye")

# 每张卡下方「刷新按钮」(⟳ + 剩余次数) 的模板与搜索窗 —— 用户 m00351 指定的判据:
# 这个按钮**只在海克斯三选一浮层开着时存在**, 浮层一关就没有, 比小眼睛可靠得多
# (2026-11-14 3-2 血案: 小眼睛被 Windows 系统通知盖住 -> 判成"已选" -> 整回合卡死)。
# 模板 `augment_refresh.png` 裁自实机帧 (69×54): 按钮左边框 + ⟳ 图标, **避开会变的数字**
# (数字在不同卡/不同刷新次数下会变, 裁进去会把分数拉低)。
# 实测 (2026-11-14, 1920×1080): 浮层开着 1.000 / 0.991 / 0.997, 浮层已关 0.28~0.31。
REFRESH_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "augment_refresh.png"
REFRESH_SEARCH_REGIONS = (
    (326, 660, 586, 800),     # 第 1 张卡刷新按钮搜索窗 (实测命中 (393,706))
    (829, 660, 1089, 800),    # 第 2 张 (实测命中 (896,706))
    (1332, 660, 1592, 800),   # 第 3 张 (实测命中 (1400,706))
)
# 阈值统一放在 utils/template_match.THRESHOLDS["augment_refresh"] (唯一配置点)
LINE_Y_TOL = 18  # 同一行 OCR 条目的 y 中心容差

_REFRESH_TPL = None  # 模板缓存 (每次判定都读盘太慢; 模板是静态文件, 进程内缓存安全)


def eye_score(rgb: np.ndarray) -> float:
    """底部中央"小眼睛"匹配分 (0~1): 存在=尚未选择海克斯, 消失=已完成选择.

    匹配核心 (多尺度 NCC + 取最高分) 走 utils.template_match, 阈值同为统一配置。
    """
    if rgb.shape[0] < EYE_SEARCH_REGION[3] or rgb.shape[1] < EYE_SEARCH_REGION[2]:
        return 0.0
    try:
        tpl = tm.load_template(EYE_TEMPLATE_PATH)
    except Exception:  # noqa: BLE001 - 模板缺失按"未命中"处理
        return 0.0
    return tm.best_score(rgb, tpl, region=EYE_SEARCH_REGION,
                         scales=(0.9, 0.95, 1.0, 1.05, 1.1))


def refresh_score(rgb: np.ndarray) -> float:
    """三张卡下方「刷新按钮」的最高匹配分 (0~1): 按钮在=浮层还开着, 尚未选择.

    三张卡各有一个按钮, 取三者最高分 (只要还在浮层里, 三个都该命中)。
    这是用户 m00351 指定的判据, 也是"是否已选"**最强**的一条证据:
    按钮是浮层独有的控件, 浮层一关就没了, 不受底部系统通知遮挡影响。

    Args:
        rgb: 1920×1080 的 RGB 数组.

    Returns:
        最高匹配分; 模板缺失/截图过小/未命中都返回 0.0 (调用方按"没看到按钮"处理).
    """
    global _REFRESH_TPL
    if rgb.shape[0] < 800 or rgb.shape[1] < 1592:
        return 0.0
    if _REFRESH_TPL is None:
        try:
            _REFRESH_TPL = tm.load_template(REFRESH_TEMPLATE_PATH)
        except Exception:  # noqa: BLE001 - 模板缺失按"未命中"处理
            return 0.0
    best = 0.0
    for region in REFRESH_SEARCH_REGIONS:
        try:
            score = tm.best_score(rgb, _REFRESH_TPL, region=region,
                                  scales=(0.95, 1.0, 1.05))
        except Exception:  # noqa: BLE001 - 单个窗口出错不影响其它两张卡
            continue
        if score > best:
            best = score
    return float(best)


def _group_lines(items: list[dict]) -> list[str]:
    """OCR 条目按 y 中心聚成行, 行内按 x 排序拼接文本."""
    lines: list[list[dict]] = []
    for it in sorted(items, key=lambda i: ((i["box"][1] + i["box"][3]) / 2, i["box"][0])):
        cy = (it["box"][1] + it["box"][3]) / 2
        if lines and abs(cy - (lines[-1][0]["box"][1] + lines[-1][0]["box"][3]) / 2) <= LINE_Y_TOL:
            lines[-1].append(it)
        else:
            lines.append([it])
    return ["".join(t["text"] for t in sorted(line, key=lambda i: i["box"][0]))
            for line in lines]


def _read_card_text(img, region: tuple[int, int, int, int]) -> tuple[str, str]:
    """读取单张卡的标题与描述: 首行为标题, 其余行拼接为描述."""
    lines = _group_lines(ocr.recognize_region(img, region))
    if not lines:
        return "", ""
    title = lines[0].strip()
    desc = "".join(line.strip() for line in lines[1:])
    return title, desc


def _read_refresh_left(img, region: tuple[int, int, int, int]) -> int | None:
    """读取卡下方刷新按钮剩余次数 (小数字, 放大后二次识别)."""
    items = ocr.recognize_region(img, region, upscale=4.0)
    m = re.search(r"\d+", "".join(it["text"] for it in items))
    return int(m.group()) if m else None


def extract_card(img, idx: int) -> dict:
    """抽取第 idx 张卡 (0/1/2) 的信息, 含点击坐标."""
    name, desc = _read_card_text(img, CARD_TEXT_REGIONS[idx])
    x1, y1, x2, y2 = CARD_TEXT_REGIONS[idx]
    return {
        "name": name,
        "desc": desc,
        "refresh_left": _read_refresh_left(img, REFRESH_DIGIT_REGIONS[idx]),
        "rect": CARD_TEXT_REGIONS[idx],               # 可点击区域 (卡牌文字面板, 两样本验证)
        "center": ((x1 + x2) // 2, (y1 + y2) // 2),   # 建议点击坐标
        "text_rect": CARD_TEXT_REGIONS[idx],
        "refresh_rect": REFRESH_DIGIT_REGIONS[idx],
    }


# ---------------------------------------------------------------- 界面状态综合判定
# 判定逻辑在本模块内实现, 棋盘 ROI 由 recognize.grid 的格位现算。
#: 棋盘 ROI「草地绿」像素占比低于此值 = 棋盘被浮层遮罩压暗 (或黑屏/过场).
#: 实测: 海克斯三选一卡片帧 0.36~0.45 / 正常 prep 0.66~0.99 / combat 0.99.
BOARD_OVERLAY_GREEN_MAX = 0.60
#: 低于此值视为黑帧/过场(不是浮层), 不参与"浮层还在"判定.
BOARD_DARK_GREEN_MAX = 0.10
#: 棋盘 ROI 兜底常量 (1920×1080 基准; 己方 4 行实测并集约 554,400-1434,702)
BOARD_ROI_FALLBACK = (554, 400, 1434, 702)
_BOARD_ROI: tuple[int, int, int, int] | None = None


def board_roi() -> tuple[int, int, int, int]:
    """棋盘 (己方 4 行) 格位中心的并集 —— 遮挡/绿度判定的 ROI (1920×1080 基准).

    格位来自 `recognize.grid.board_cells()` (由 config.BOARD_GRID_ROWS 推导), 只取 row>=5
    (己方 4 行); 取不到格位时用实测常量兜底, 绝不因此中断判定。
    """
    global _BOARD_ROI
    if _BOARD_ROI is None:
        cells = []
        try:
            from recognize import grid
            cells = [c for c in grid.board_cells() if int(c.get("row", 0)) >= 5]
        except Exception:  # noqa: BLE001 - 格位表异常时用兜底 ROI
            cells = []
        if not cells:
            _BOARD_ROI = BOARD_ROI_FALLBACK
        else:
            half = 52  # 格位半径 (棋子框 78 宽 -> 半宽 39, 再留一点边缘)
            _BOARD_ROI = (
                min(int(c["cell_center"][0]) for c in cells) - half,
                min(int(c["cell_center"][1]) for c in cells) - half,
                max(int(c["cell_center"][0]) for c in cells) + half,
                max(int(c["cell_center"][1]) for c in cells) + half,
            )
    return _BOARD_ROI


def board_green_frac(img) -> float:
    """棋盘 ROI 内「草地绿」像素占比 (0~1). 正常棋盘 >0.6, 被卡片/遮罩压暗 <0.5."""
    if not isinstance(img, Image.Image):
        img = Image.fromarray(np.asarray(img))
    x0, y0, x1, y1 = board_roi()
    a = np.asarray(img.convert("RGB").crop((x0, y0, x1, y1)), dtype=np.int16)
    green = a[..., 1] - np.maximum(a[..., 0], a[..., 2])
    return float((green > 10).mean())


def overlay_present(green: float | None) -> bool:
    """棋盘是否被浮层遮罩压暗 (= 海克斯三选一浮层还开着的最强旁证)."""
    if green is None:
        return False
    return BOARD_DARK_GREEN_MAX <= green < BOARD_OVERLAY_GREEN_MAX


def judge_augment_screen(img) -> tuple[dict, Image.Image]:
    """综合判定是否处于海克斯选择界面及选择状态 (刷新按钮 > 小眼睛 > 卡牌图标+棋盘绿度).

    Args:
        img: 截图 (PIL Image 或 RGB numpy 数组均可, 非 1920×1080 自动归一化).

    Returns:
        (state, img):
            state: {
                "is_augment":  是否海克斯选择界面,
                "chosen":      False=未选择(卡牌可点击) / True=已选择 / None=非海克斯界面,
                "eye_score":   小眼睛匹配分,
                "card_score":  顶栏卡牌图标匹配分,
                "refresh_score": 三张卡下方刷新按钮(⟳)的最高匹配分,
                "green":       棋盘 ROI 草地绿占比 (None=算不出来),
                "overlay":     棋盘是否被浮层遮罩压暗 (小眼睛被挡时的交叉证据),
                "stage":       回合号 (如 "3-2"), 非海克斯界面为 None,
            }
            img: 归一化到 1920×1080 的 PIL 截图, 供后续区域 OCR/点击复用.
    """
    img = phase.normalize_1080(img)
    rgb = np.array(img.convert("RGB"))
    eye = eye_score(rgb)
    card = phase.card_icon_score(rgb)
    try:
        refresh = refresh_score(rgb)
    except Exception:  # noqa: BLE001 - 模板缺失等异常退回老判据, 绝不因此中断
        refresh = 0.0
    eye_thr = tm.threshold("augment_eye")
    card_thr = tm.threshold("phase_cards")
    refresh_thr = tm.threshold("augment_refresh")
    try:
        green = board_green_frac(img)
    except Exception:  # noqa: BLE001 - 绿度只是辅证, 算不出来就退回只看图标的老判据
        green = None
    overlay = overlay_present(green)
    is_augment = (refresh >= refresh_thr) or (eye >= eye_thr) or (card >= card_thr)
    if not is_augment:
        chosen = None
    elif refresh >= refresh_thr:
        chosen = False                      # 刷新按钮还在: 浮层开着, 尚未选择 (最硬证据)
    elif eye >= eye_thr:
        chosen = False                      # 小眼睛在: 浮层开着, 尚未选择
    elif overlay:
        chosen = False                      # 眼睛被通知遮挡, 但浮层遮罩还在 -> 仍未选
    else:
        chosen = True                       # 眼睛不在 + 棋盘清晰 -> 浮层已关闭
    return {
        "is_augment": is_augment,
        "chosen": chosen,
        "eye_score": round(eye, 3),
        "card_score": round(card, 3),
        "refresh_score": round(refresh, 3),
        "green": None if green is None else round(green, 3),
        "overlay": overlay,
        "stage": phase.parse_stage(img) if is_augment else None,
    }, img


def extract(img) -> dict:
    """抽取海克斯选择界面全部信息 (状态综合判定见本模块 judge_augment_screen).

    Args:
        img: 截图 (非 1920×1080 会自动归一化).

    Returns:
        {"is_augment", "chosen", "eye_score", "card_score", "refresh_score", "green",
         "overlay", "stage", "cards": [{"name","desc","refresh_left","rect","center",...}]}.
        chosen=True 表示已完成选择 (切换/刷新按钮都消失), 此时卡牌已不在画面上, cards 为空.
    """
    state, img = judge_augment_screen(img)
    cards = ([extract_card(img, i) for i in range(3)]
             if state["is_augment"] and not state["chosen"] else [])
    return {**state, "cards": cards}
