"""对局阶段判断: STAGE 回合号 + 顶栏追踪器亮黄图标形状.

判定规则:
    1. OCR regions.STAGE 解析回合号 r, 如 "2-3" -> 3; 识别失败 -> unknown (非对局界面);
    2. 顶栏第一个槽位 (STAGE 右侧) 金色双卡牌图标模板匹配达标 -> augment 海克斯选择阶段
       (该槽位在普通回合是交叉剑, 海克斯回合变为金色卡牌, 见 recognize/templates/phase_cards.png);
    3. 追踪器区域 (STAGE 右侧图标行, 右界避开金币计数) 内找亮黄团:
       - 有亮黄且剑形模板匹配达标 -> prep  准备阶段 (可自由操作);
       - 无双剑但中央部署人口可见 -> prep（特殊回合头像替代双剑）;
       - 初始野怪回合 (大阶段==1) 且顶栏亮黄 -> prep（1-x 顶栏只有 4 个进度菱形, 剑形实测
         0.44~0.51 < 0.55 且人口常读不到, 但亮黄即准备标记; 战斗/回合结束实测熄灭 yellow<30）;
       - 否则若大阶段≥2 且 r == 4 -> draft 选装备回合 (无法进行棋子操作; 1-4 野怪回合除外);
       - 其余                      -> combat 战斗阶段 (只读).

实现说明: 亮黄图标在 HSV 掩码下是碎块状, 先连通域合并成局部窗,
再在窗内做多尺度剑形匹配 —— 比整行匹配抗干扰, 也无需标定槽位网格.
"""

import re
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from recognize import ocr
from config.config import default_config
from utils import template_match as tm

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "phase_swords.png"
TRACKER_REGION = (700, 0, 1182, 87)  # 1920×1080 STAGE 右侧图标行 (1080 基准 (430,0,665,58))
# 左界 764→700: 实机 4-1 帧的**双剑 prep 标记贴着 764 左边界被切掉一半** → 剑形只拿到 0.286
# 而被判 combat (那帧人口标志头盔 0.953, 明显是准备阶段)。左扩后同一帧 0.286 → **0.890** [OK];
# 战斗帧不受影响 (2-3 事故帧仍 0.570 / 3-5 combat 0.473, 均远低于阈值)。
YELLOW_MIN_PIXELS = 100              # 亮黄像素低于此视为整行熄灭 (战斗阶段实测 <30)
BLOB_MIN_AREA = 40                   # 单个连通域计入亮团的最小像素
BLOB_MERGE_GAP = 40                  # 连通域间距小于此值则并入同团
BLOB_PAD = 20                        # 匹配窗相对黄团 bbox 的外扩
# 阈值统一放在 utils/template_match.THRESHOLDS (唯一配置点), 这里只做别名引用。
SWORD_SCORE_MIN = tm.threshold("phase_swords")
SWORD_SCALES = tm.SWORD_SCALES

CARD_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "phase_cards.png"
CARD_ICON_REGION = (740, 0, 920, 70)  # 顶栏图标槽位 (海克斯回合为金色双卡牌; 右界放宽, 部分局内多头像图标会把卡牌图标右推约50px)
CARD_SCALES = tm.CARD_SCALES
CARD_SCORE_MIN = tm.threshold("phase_cards")
# 海克斯选卡浮层"小眼睛"的阈值: 唯一配置点是 utils/template_match.THRESHOLDS["augment_eye"]
# (recognize.augment.EYE_SCORE_MIN 也是同一来源, 不再各存一份)。
# 用途: 区分 "浮层还开着" 与 "已选完回到准备画面" —— 实测浮层未关 0.998 / 已选完 0.292。
EYE_SCORE_MIN = tm.threshold("augment_eye")

PHASE_PREP = "prep"        # 准备阶段: 可自由操作
PHASE_DRAFT = "draft"      # 选装备回合 (x-4): 无法进行棋子操作
PHASE_AUGMENT = "augment"  # 海克斯选择阶段: 只能选强化符文, 无法进行棋子操作
PHASE_COMBAT = "combat"    # 战斗阶段 (含野怪回合): 只读
PHASE_UNKNOWN = "unknown"  # 非对局界面 (大厅/加载等)

# 阶段代号 -> 中文说明 (extract_info 的 phase 键与 extract_img_info 的 game 表共用)
PHASE_NOTES = {
    PHASE_PREP: "prep 准备阶段",
    PHASE_DRAFT: "draft 选秀阶段",
    PHASE_AUGMENT: "augment 海克斯选择阶段",
    PHASE_COMBAT: "combat 战斗阶段",
    PHASE_UNKNOWN: "unknown 非对局",
}

_stage_box = None


def normalize_1080(img):
    """非 1920×1080 输入等比缩放到基准分辨率, 使全部区域坐标成立 (原样返回 1080p 图)."""
    target = default_config.DEFAULT_RESOLUTION
    if img.size == target:
        return img
    return img.resize(target, Image.LANCZOS)


def parse_stage(img) -> str | None:
    """解析完整回合号（如3-2）；定位框可复用，文字每次重新识别。"""
    global _stage_box
    if _stage_box:
        text, score = ocr.recognize_line(img.crop(_stage_box))
        if score >= 0.9 and re.fullmatch(r"\d+-\d+", text.strip()):
            return text.strip()
    # 开局野怪回合的顶栏较短，回合号会向右移动，不能只读常规回合窄框。
    search = default_config.STAGE_SEARCH_REGION
    for it in ocr.recognize(img.crop(search)):
        m = re.search(r"\d+-\d+", it["text"])
        if m:
            x1, y1, x2, y2 = it["box"]
            _stage_box = (round(search[0] + x1 - 3), max(0, round(search[1] + y1 - 3)),
                          round(search[0] + x2 + 3), round(search[1] + y2 + 3))
            return m.group(0)
    return None


def parse_round(img) -> int | None:
    stage = parse_stage(img)
    return int(stage.split("-")[1]) if stage else None


def _lit_mask(roi_rgb: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(roi_rgb, cv2.COLOR_RGB2HSV)
    return cv2.inRange(hsv, (18, 110, 140), (38, 255, 255))


def _yellow_windows(roi: np.ndarray) -> list[tuple[int, int, int, int]]:
    """亮黄连通域合并为局部匹配窗 (ROI 坐标, x1y1x2y2)."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(_lit_mask(roi).astype(np.uint8))
    boxes = [tuple(int(v) for v in stats[i][:4])
             for i in range(1, n) if stats[i, 4] >= BLOB_MIN_AREA]
    merged: list[list[int]] = []
    for x, y, w, h in boxes:
        for m in merged:
            near = (x < m[0] + m[2] + BLOB_MERGE_GAP and x + w + BLOB_MERGE_GAP > m[0]
                    and y < m[1] + m[3] + BLOB_MERGE_GAP and y + h + BLOB_MERGE_GAP > m[1])
            if near:
                nx1, ny1 = min(m[0], x), min(m[1], y)
                nx2, ny2 = max(m[0] + m[2], x + w), max(m[1] + m[3], y + h)
                m[0], m[1], m[2], m[3] = nx1, ny1, nx2 - nx1, ny2 - ny1
                break
        else:
            merged.append([x, y, w, h])
    return [(max(0, x - BLOB_PAD), max(0, y - BLOB_PAD),
             min(roi.shape[1], x + w + BLOB_PAD), min(roi.shape[0], y + h + BLOB_PAD))
            for x, y, w, h in merged]


_CARD_TPL: np.ndarray | None = None
_SWORD_TPL: np.ndarray | None = None


def _sword_template() -> np.ndarray | None:
    """双剑模板 (只读一次)."""
    global _SWORD_TPL
    if _SWORD_TPL is None:
        try:
            _SWORD_TPL = tm.load_template(TEMPLATE_PATH)
        except Exception:  # noqa: BLE001 - 模板缺失时按"无 prep 标记"处理
            return None
    return _SWORD_TPL


def _card_template() -> np.ndarray | None:
    """卡牌模板 (只读一次; 匹配核心交给 utils.template_match)。"""
    global _CARD_TPL
    if _CARD_TPL is None:
        try:
            _CARD_TPL = tm.load_template(CARD_TEMPLATE_PATH)
        except Exception:  # noqa: BLE001 - 模板缺失时按"无卡牌图标"处理, 不杀主循环
            return None
    return _CARD_TPL


def card_icon_score(rgb: np.ndarray) -> float:
    """顶栏第一个槽位的金色双卡牌图标匹配分 (0~1), 供 augment 判定与脚本复核复用."""
    x0, y0, x1, y1 = CARD_ICON_REGION
    if y1 > rgb.shape[0] or x1 > rgb.shape[1]:
        return 0.0  # 局部小图调试时区域越界, 直接视为无卡牌图标
    tpl = _card_template()
    if tpl is None:
        return 0.0
    return tm.best_score(rgb, tpl, region=CARD_ICON_REGION, scales=CARD_SCALES)


def _is_pve_round(stage: str | None) -> bool:
    """是不是"野怪回合"(双黄剑标记不显示, 需要用人口标志兜底的那类回合).

    用户口径: **1 阶段除 1-1 之外的全部** (1-2 ~ 1-7) + **其它大阶段的 n-7**。
    """
    if not stage or "-" not in stage:
        return False
    try:
        major, rnd = (int(v) for v in stage.split("-")[:2])
    except ValueError:
        return False
    return (major == 1 and rnd >= 2) or rnd == 7


def marker_present(img) -> bool:
    """野怪回合保底判据: **恒 False** (纯图色工程没有人口标志通道).

    新工程不读内存、也没有顶栏头盔模板 —— 这条判据直接退化为"没有这条信息":
    返回 False 只会让 `_is_pve_round(stage) and marker_present(img)` 这个兜底分支不生效,
    该帧仍沿用上面已有的 swords/cards/early-round 判定, 不会把战斗误判成准备 (只有正例
    才该返回 True, 所以恒 False 是最安全的降级)。
    """
    return False


def detect(img, region: tuple[int, int, int, int] | None = None) -> dict:
    """判断对局阶段.

    Args:
        img: 1920×1080 截图.
        region: 自定义扫描区域 (默认追踪器区域; 供局部图调试用, 此时跳过 STAGE 解析).

    Returns:
        {"phase", "stage_round", "yellow", "sword_score", "card_score"}.
    """
    if region is None:
        img = normalize_1080(img)  # 模拟器窗口尺寸不一致时区域坐标仍成立
    rgb = np.array(img.convert("RGB"))
    x0, y0, x1, y1 = region or TRACKER_REGION
    roi = rgb[y0:y1, x0:x1]
    yellow = int(cv2.countNonZero(_lit_mask(roi)))
    stage = parse_stage(img) if region is None else None
    stage_r = int(stage.split("-")[1]) if stage else None
    stage_m = int(stage.split("-")[0]) if stage else None  # 大阶段号 (1-4 野怪不算选装备)
    cards = card_icon_score(rgb)

    tpl = _sword_template()
    best = 0.0
    if tpl is not None and yellow >= YELLOW_MIN_PIXELS:
        # 匹配核心与阈值都走 utils.template_match (亮窗预筛 + 多尺度 NCC 统一在这里)
        hits = tm.match(rgb, tpl, region=(x0, y0, x1, y1), scales=SWORD_SCALES, topk=1,
                        prefilter_hsv=tm.YELLOW_HSV, prefilter_min_px=YELLOW_MIN_PIXELS,
                        prefilter_pad=BLOB_PAD)
        best = hits[0]["score"] if hits else 0.0

    # 特殊回合顶栏会用角色头像替代双剑。
    # "选完海克斯后顶栏卡牌图标仍常亮" 与 "选卡浮层还开着" 的区分: 用**浮层自己的小眼睛**
    # (augment_eye 模板), 而不是"中央人口数字能不能读到" —— 后者是间接代理且实测不可靠:
    # 三帧真实画面 (两张准备画面 + 一张浮层) 人口数字**全部读不到** (`7 / 7` 被 OCR 成 '717'),
    # 于是判据退化成"图标亮 = augment", 把准备画面判成 augment → 主循环反复走海克斯分支、
    # roster 也拒绝点击 → 整个准备回合被白扔 (实机 4-2 卡死)。小眼睛实测分得很开:
    # 浮层未关 0.998 / 已选完 0.292 (门槛 0.8), 与 recognize.augment.judge_augment_screen 判 chosen 的判据同源。
    eye = 0.0
    refresh = 0.0
    if stage is not None and region is None and cards >= tm.threshold("phase_cards"):
        from recognize import augment as _augment  # 函数内导入, 避免 recognize 内部循环依赖
        _rgb = np.array(img.convert("RGB"))
        eye = _augment.eye_score(_rgb)  # eye_score 吃 ndarray
        # 2026-11-14 用户 m00351: 三张卡下方的"刷新按钮"(⟳)是浮层独有控件 ——
        # 小眼睛被系统通知/弹窗盖住时(3-2 血案 eye=0.280), 靠它把"浮层还开着"救回来。
        refresh = _augment.refresh_score(_rgb)
    if stage_r is None:
        phase = PHASE_UNKNOWN
    elif cards >= tm.threshold("phase_cards") and stage_r != 4 and (
            eye >= tm.threshold("augment_eye") or refresh >= tm.threshold("augment_refresh")):
        phase = PHASE_AUGMENT  # 卡牌图标亮 **且(小眼睛在 或 刷新按钮在)** = 选卡浮层还开着
    elif best >= tm.threshold("phase_swords") or (
            cards >= tm.threshold("phase_cards") and eye < tm.threshold("augment_eye")
            and refresh < tm.threshold("augment_refresh")):
        # 双剑标记亮; 或卡牌图标亮但小眼睛和刷新按钮都不在 = 已选完回到准备画面 (prepare)
        phase = PHASE_PREP
    elif stage_m == 1 and yellow >= YELLOW_MIN_PIXELS:
        # 初始野怪回合 (1-x) 兜底: 顶栏无常规 prep 剑形标记 (1-x 只有 4 个进度菱形),
        # 剑形实测仅 0.44~0.51 (<0.55) → 原逻辑误判 combat 导致整段 1-1~1-4 不进入准备流程
        # (不购买). 战斗/回合结束实测顶栏熄灭 (yellow<30), 亮起即有准备标记 → 判 prep
        # (数据: 实机 1-1~1-4 帧 yellow 352~423).
        phase = PHASE_PREP
    elif stage_r == 4 and stage_m >= 2:
        phase = PHASE_DRAFT  # 大阶段≥2 的 x-4 才是选装备回合; 1-4 是野怪回合
    elif _is_pve_round(stage) and marker_present(img):
        # 野怪回合保底 (用户口径): 双黄剑标记在野怪回合不显示 (顶栏换成野怪图标),
        # 此时用"场上人口标志"做正例判据 —— 只有**识别到标志**才升级为 prep,
        # 认不出来就当没这条信息 (不覆盖上面任何判定, 也不影响其它回合)。
        phase = PHASE_PREP
    else:
        phase = PHASE_COMBAT
    if phase == PHASE_AUGMENT:
        # 小眼睛在 = 常规; 小眼睛被系统通知遮挡但刷新按钮还在 = 2026-11-14 3-2 血案那种帧
        reason = ("cards_with_eye" if eye >= tm.threshold("augment_eye")
                  else "cards_with_refresh")
    elif best >= tm.threshold("phase_swords"):
        reason = "swords"
    elif cards >= tm.threshold("phase_cards"):
        reason = "cards_no_eye"          # 卡牌图标亮但浮层已关 = 已选完回到准备画面
    elif phase == PHASE_PREP and _is_pve_round(stage):
        reason = "pve_population_marker"  # 野怪回合: 人口标志命中 (头盔模板 / OCR)
    elif phase == PHASE_PREP:
        reason = "early_round_yellow"  # 1-x 野怪回合: 顶栏亮起兜底判 prep
    else:
        reason = "no_prep_marker"
    return {"phase": phase, "stage": stage, "stage_round": stage_r,
            "yellow": yellow, "sword_score": round(best, 3),
            "card_score": round(cards, 3),
            "eye_score": round(eye, 3),
            "refresh_score": round(refresh, 3),
            "reason": reason}


def current_phase(img) -> str:
    """返回 'prep' / 'draft' / 'augment' / 'combat' / 'unknown'."""
    return detect(img)["phase"]

