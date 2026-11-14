"""scripts/game.py - 纯图色版游戏操作脚手架 (CLI)

本工程 (jcc_robot_by_img) 是"只靠截图识别 + 模拟输入"的新链路, 没有内存读取能力。
本文件是**一次对局里"看图 -> 点哪里"的全部动作入口**, 设计口径:

1. 坐标一律用 **1920×1080 基准**; 换算到屏幕/客户区/Android 像素由 `control.input` 负责,
   这里只算基准坐标 (这样离线图 (`--img tests/images/xxx.png`) 与实机截图走同一套数字)。
2. 每个动作命令都是"**先打印计划 (坐标 + 判定依据), 再执行**";
   `--dry-run` 只打印不执行 —— 没插模拟器时也能验坐标/校验识别是否对。
3. 点击一律走 `control.fallback.click_with_fallback`: 逐通道 (bg/fg/adb) 点击 + 复核,
   不生效自动换通道; 复核判据按动作选 (买卡=槽位卡面消失, 买经验=金币减少,
   刷新=商店区变化且金币减少, 捡球=球消失, 选格=右侧详情面板变化)。
4. `--json` 时 **stdout 只有一行 JSON** (`{"ok":..., "cmd":..., "data"/"error":...}`),
   人类可读输出全部转到 stderr; 退出码见 EXIT_* 常量。
5. 输出禁用 emoji, 统一前缀 `[OK]` / `[X]` / `[!]` / `[i]`。

未实现 (纯图色链路不适配, 有意不做, --help 里标注 "[未实现]"):
    search-buy (自动刷牌买卡, 依赖连续对局状态机) / start (启动游戏, 依赖窗口尺寸与启动脚本)
    / exit (结算退出, 依赖关卡轮转) / blessing (野怪赐福, 依赖 beast 专用识别模块)。

用法示例 (工程根目录):
    python -X utf8 -u -m scripts.game units --img tests/images/board_ingame.png
    python -X utf8 -u -m scripts.game shop  --img tests/images/board_ingame.png --json
    python -X utf8 -u -m scripts.game buy --slot 1 --dry-run
    python -X utf8 -u -m scripts.game equip-to --item 死亡之刃 --slot 3 --dry-run
    python -X utf8 -u -m scripts.game combine --a 死亡之刃 --b 死亡之刃 --dry-run
"""

from __future__ import annotations

# 直接运行支持: `python scripts/game.py ...` 与 `python -m scripts.game ...` 等价。
# 直接跑脚本时 sys.path[0] 是 scripts/ 目录, 项目根不在搜索路径里 -> 这里补上。
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from capture.save import save_to_dir
from capture.screen import capture_game, resize
from config.config import InfoTypes, default_config
from control.fallback import (ClickFailed, PixelDiffVerifier, click_with_fallback,
                              describe_verify, pixel_diff, region_around)
from recognize import augment, board, equipment, extract, phase, regions
from recognize.grid import board_cells
from utils.reward_ball import collect_balls, print_balls


# ---------------------------------------------------------------------------
# 退出码 / 常量 (与源工程 scripts/game.py 同口径, 便于外部脚本沿用)
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_STATE = 3
EXIT_OCR = 4
EXIT_TARGET = 5
EXIT_ACTION = 6
EXIT_TIMEOUT = 7
EXIT_INTERNAL = 8

KEY_APPLY_WAIT = 1.5        # 点完按钮后等界面反应的超时
LEVELUP_CLICK_GAP = 0.25    # 连续点同一按钮(买经验/刷新)之间的间隔
CLICK_SETTLE = 0.25         # 单击后等画面稳定再复核

SHOP_DIFF_REGION = (210, 906, 1700, 1010)   # 刷新后"商店卡面区域"的变化范围
SHOP_DIFF_MIN = 200                          # 区域差像素 > 此值才算商店有变化
SHOP_GOLD_BUBBLE_DIFF = 30000                # 差 < 此值时要额外看金币 (金币不足气泡只有 800+)
SHOP_RENDER_RETRY_TIMES = 4
SHOP_RENDER_RETRY_GAP = 0.25
SHOP_EXPECT_CARDS = 5

CONFIRM_TEMPLATE = "recognize/templates/combine_confirm_ok.png"
CONFIRM_THRESHOLD = 0.8
CONFIRM_SEARCH_WIDTH = 7      # 确认按钮在拖拽落点右侧几倍宽的范围内搜
CONFIRM_POPUP_WAIT = 4.0
CONFIRM_RETRY_GAP = 0.8

DETAIL_VERIFY_DIFF_MIN = 200  # 点棋子后右侧详情面板的变化阈值
DRAG_VERIFY_DIFF_MIN = 200    # 拖拽后落点/起点区域的变化阈值

EQUIP_SHOW_COLS = [("slot", "格号", 4), ("row", "行", 2), ("col", "列", 2),
                   ("equipId", "id", 5), ("name", "名称", 12), ("type2", "类型", 9),
                   ("count", "数量", 2), ("formula_name", "合成公式", 24),
                   ("score", "分数", 5), ("center", "center", 11)]

_GAME_SHOW_COLS = ("stage", "phase", "hex", "round_time", "level", "xp", "gold",
                   "xp_buy_cost", "refresh_cost", "free_refresh", "is_over")

INFO_EXPLAINS = {
    "stage": "回合号, 如 2-1",
    "phase": "对局阶段 (prep 准备 / draft 选秀 / augment 海克斯 / combat 战斗)",
    "hex": "海克斯选择状态: hex_0 未选择 / hex_1 已选择",
    "round_time": "本回合剩余秒数",
    "level": "当前等级",
    "xp": "经验 [当前, 升级所需]",
    "gold": "金币",
    "xp_buy_cost": "购买经验按钮的费用 (金币)",
    "refresh_cost": "刷新商店按钮的费用 (金币)",
    "free_refresh": "免费刷新剩余次数",
    "is_over": "对局是否已结束",
    "traits": "左侧羁绊列表",
    "players": "右侧玩家血量列表",
    "shop": "商店 5 张卡牌 (name / price / center)",
    "board": "场上奕子 (location 含 row/column/own_row)",
    "bench": "备战席奕子 (location.slot)",
    "little_legend": "小小英雄位置",
    "hextech": "海克斯强化符文卡",
    "equip": "左侧装备栏 (equipment.extract_equipment 的输出)",
}

# 动作命令的"判定依据"一句话 (--dry-run 时打印, 也是 --help 与报告里的口径)
VERIFY_NOTES = {
    "buy": "买卡: 该商店槽的卡面区域像素变化 > 阈值 (卡被买走 -> 槽位变空)",
    "levelup": "买经验: 金币数值变小",
    "refresh": "刷新: 商店卡面区域变化且金币减少 (只看变化会把'金币不足'气泡误判成功)",
    "cell": "选格: 右侧详情面板 (DETAIL_CARD) 像素变化",
    "equip-to": "装备: 装备栏里该件装备消失 (局部像素差)",
    "combine": "合成: 装备栏总件数减少 1 (或确认弹窗出现并点掉)",
    "use-remover": "拆卸: 装回去后装备栏件数 +1",
    "place": "上场: 该备战席格子的区域像素变化",
    "swap": "交换: 源格子区域像素变化",
    "sell": "卖出: 源格子区域像素变化(棋子消失)",
    "balls": "捡球: 该球从画面消失",
    "hex-pick": "选海克斯: 选完后 augment.extract 的 chosen=True",
    "hex-refresh": "刷新海克斯: 卡面区域或剩余刷新次数变化",
}


class GameError(Exception):
    """带错误码/退出码的业务错误 (main 捕获后打印 [X] 而不是 traceback)."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_ACTION) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _error_code_for(exc: BaseException) -> tuple[str, int]:
    if isinstance(exc, GameError):
        return exc.code, exc.exit_code
    if isinstance(exc, ValueError):
        return "INVALID_ARGUMENT", EXIT_USAGE
    if isinstance(exc, TimeoutError):
        return "TIMEOUT", EXIT_TIMEOUT
    if isinstance(exc, FileNotFoundError):
        return "NOT_FOUND", EXIT_TARGET
    return "INTERNAL", EXIT_INTERNAL


def _say(*parts) -> None:
    print(*parts, flush=True)


def _to_jsonable(obj):
    """dict/list/tuple/Path/numpy -> 可 json 序列化的结构 (保持键顺序)."""
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, Image.Image):
        return {"size": list(obj.size), "mode": obj.mode}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


# ---------------------------------------------------------------------------
# 截图 / 离线图
# ---------------------------------------------------------------------------

def _shot():
    """实时截图并归一化到 1920×1080 基准; 没有窗口时给中文报错而不是 traceback."""
    try:
        return resize(capture_game())
    except RuntimeError as exc:
        raise GameError(
            "NO_WINDOW",
            f"取不到游戏画面: {exc}。请先启动雷电模拟器并进入对局画面; "
            "只想核对坐标/识别结果请加 --dry-run, 或加 --img 指向离线截图",
            EXIT_STATE) from exc


def _load_img(path: str) -> Image.Image:
    p = Path(path)
    if not p.exists():
        raise GameError("NOT_FOUND", f"图片不存在: {p}", EXIT_TARGET)
    img = Image.open(p).convert("RGB")
    if tuple(img.size) != tuple(default_config.DEFAULT_RESOLUTION):
        _say(f"[i] 离线图尺寸 {img.size} != 基准 {default_config.DEFAULT_RESOLUTION}: "
             "先等比缩放到基准再识别 (否则识别区域/坐标为另一套比例, 不能与实机点击数字对齐)")
        img = resize(img)
    return img


def _save_flash(img, tag: str, *, parent=None) -> Path:
    """把一张截图存进 CAPTURE_DIR (或 parent) 并返回路径.

    `capture.save.save_to_dir` 只会 `image.save(parent / image_name)`, 所以文件名**必须带扩展名**
    (早期传裸 tag 会抛 `ValueError: unknown file extension: `), 这里统一补时间戳 + .png。
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    ms = int(time.perf_counter() * 1000) % 1000
    return save_to_dir(img, f"{tag}_{stamp}_{ms:03d}.png", parent_dir=parent)


def _shot_or_img(path: str | None, *, tag: str = "game_live"):
    """--img 则用离线图; 否则实时截图并留档 (排障用)."""
    if path:
        img = _load_img(path)
        _say(f"[i] 来源图片: {path}")
        return img
    img = _shot()
    try:
        saved = _save_flash(img, tag)
        _say(f"[i] 截图留档: {saved}")
    except Exception as exc:      # noqa: BLE001 - 留档失败不影响主流程
        _say(f"[!] 截图留档失败: {type(exc).__name__}: {exc}")
    return img


def debug_shot() -> Path | None:
    """实机截图存档并打印路径 (--debug)."""
    img = _shot()
    saved = _save_flash(img, "debug")
    _say(f"[i] debug 截图: {saved}")
    return saved


# ---------------------------------------------------------------------------
# 阶段校验 + 单位身份 (T1 实测口径: 身份优先 YOLO, 血条/模板只作交叉校验)
# ---------------------------------------------------------------------------

PHASE_PREP = ("prep",)              # 只允许准备阶段的动作 (买/卖/上场/装备/刷新/买经验)
PHASE_GAME = ("prep", "combat")     # 对局中的任意阶段 (选格看详情/捡球)
PHASE_AUGMENT = ("augment",)        # 海克斯选择界面


def _units_yolo(img) -> dict:
    """单位识别: 优先 YOLO (`yolo_units.extract_units`, 11n 实测 name 0.97~0.99, 0.26s/帧).

    回退: 模板图色 `board.extract_units` (血条定位准但 name 弱, 只当交叉校验/兜底)。
    """
    from recognize import yolo_units
    try:
        units = yolo_units.extract_units(img)
        if units.get("board") or units.get("bench"):
            return units
        _say("[!] YOLO 没识别到单位 (可能不是对局帧), 回退模板图色 board.extract_units")
    except Exception as exc:      # noqa: BLE001 - 模型缺失/onnx 异常都要能退
        _say(f"[!] YOLO 不可用 ({type(exc).__name__}: {exc}), 回退模板图色 board.extract_units")
    return board.extract_units(img)


def _phase_gate(args, *, what: str, allow: tuple[str, ...] = PHASE_PREP,
                img=None) -> dict:
    """点前阶段校验: 非对局帧/非预期阶段一律不点 (遗漏比误检好).

    dry-run 取不到画面时只提示跳过; 实跑取不到画面由 _shot 抛 NO_WINDOW 中文错误。
    """
    dry = bool(getattr(args, "dry_run", False))
    if img is None:
        try:
            img = _shot_or_img(getattr(args, "img", None), tag="phase_check")
        except GameError as exc:
            if dry:
                _say(f"[!] dry-run 取不到画面, 跳过阶段校验: {exc}")
                return {"phase": None, "skipped": True}
            raise
    try:
        info = phase.detect(img)
    except Exception as exc:      # noqa: BLE001 - 阶段检测失败不该阻断看坐标
        _say(f"[!] 阶段检测失败 ({type(exc).__name__}: {exc}), 跳过阶段校验")
        return {"phase": None, "skipped": True}
    ph = info.get("phase")
    stage = info.get("stage")
    _say(f"[i] 阶段校验: phase={ph} stage={stage} reason={info.get('reason')}")
    if ph in allow:
        return info
    msg = (f"当前画面阶段为 {ph!r} (stage={stage}, reason={info.get('reason')}), "
           f"不在允许阶段 {list(allow)}; 拒绝执行{what} —— "
           "先在准备阶段(或对应界面)再跑, 避免点空/误点")
    if dry:
        _say(f"[!] {msg} (dry-run 只提示, 实跑会拒绝)")
        return info
    raise GameError("NOT_IN_GAME", msg, EXIT_STATE)


# ---------------------------------------------------------------------------
# 坐标: 全部 1920×1080 基准
# ---------------------------------------------------------------------------

def _check(value, lo: int, hi: int, name: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}={value!r} 不是整数")
    if not lo <= v <= hi:
        raise ValueError(f"{name}={v} 超出范围 [{lo}, {hi}]")
    return v


def board_cell_center(row, column) -> tuple[int, int]:
    """己方第 row(1-4) 行第 column(1-7) 列的中心坐标 (整盘第 row+4 行)."""
    row = _check(row, 1, 4, "row")
    column = _check(column, 1, default_config.BOARD_COLUMNS, "column")
    cells = {(c["row"] - 4, c["column"]): tuple(c["cell_center"])
             for c in board_cells() if c["row"] >= 5}
    target = cells.get((row, column))
    if target is None:
        raise ValueError(f"棋盘格位 (己方 {row},{column}) 不在网格里")
    return int(target[0]), int(target[1])


def bench_slot_center(slot) -> tuple[int, int]:
    """备战席第 slot 格 (1..9) 的中心坐标."""
    slots = default_config.BENCH_SLOT_CENTERS
    slot = _check(slot, 1, len(slots), "slot")
    x, y = slots[slot - 1]
    return int(x), int(y)


def shop_slot_center(index) -> tuple[int, int]:
    """商店第 index 张卡 (1..5) 的中心坐标."""
    lefts = regions.SHOP_SLOT_LEFTS
    index = _check(index, 1, len(lefts), "index")
    x1, y1, x2, y2 = regions.SHOP
    return (int(lefts[index - 1] + regions.SHOP_SLOT_WIDTH // 2), int((y1 + y2) // 2))


def shop_slot_rect(index) -> tuple[int, int, int, int]:
    """商店第 index 张卡的复核区域 (买卡后卡面消失 -> 这块像素必变)."""
    lefts = regions.SHOP_SLOT_LEFTS
    index = _check(index, 1, len(lefts), "index")
    x1, y1, _, y2 = regions.SHOP
    x0 = int(lefts[index - 1])
    return (x0, int(y1), x0 + int(regions.SHOP_SLOT_WIDTH), int(y2))


def shop_center() -> tuple[int, int]:
    x1, y1, x2, y2 = regions.SHOP
    return (int(x1 + x2) // 2, int(y1 + y2) // 2)


def parse_spec(text: str) -> tuple:
    """位置描述 -> ("board", 行, 列) / ("bench", 格)."""
    raw = str(text).strip()
    parts = [p.strip() for p in raw.split(",")]
    if parts and parts[0].lower() in ("board", "棋盘"):
        if len(parts) != 3:
            raise ValueError(f"位置描述 {text!r} 格式应为 board,行,列 或 bench,格")
        return ("board", _check(parts[1], 1, 4, "行"), _check(parts[2], 1, 7, "列"))
    if parts and parts[0].lower() in ("bench", "备战席"):
        if len(parts) != 2:
            raise ValueError(f"位置描述 {text!r} 格式应为 board,行,列 或 bench,格")
        return ("bench", _check(parts[1], 1, len(default_config.BENCH_SLOT_CENTERS), "格"))
    raise ValueError(f"位置描述 {text!r} 格式应为 board,行,列 或 bench,格")


def resolve_center(spec) -> tuple[int, int]:
    if isinstance(spec, str):
        spec = parse_spec(spec)
    if spec[0] == "board":
        return board_cell_center(spec[1], spec[2])
    if spec[0] == "bench":
        return bench_slot_center(spec[1])
    raise ValueError(f"未知位置类型 {spec[0]!r}")


def _spec_label(spec) -> str:
    if isinstance(spec, str):
        spec = parse_spec(spec)
    if spec[0] == "board":
        return f"己方棋盘({spec[1]},{spec[2]})"
    return f"备战席第{spec[1]}格"


# ---------------------------------------------------------------------------
# 装备解析 / 识别辅助
# ---------------------------------------------------------------------------

def equip_centers(img) -> dict:
    """装备栏 -> {slot: center} (按槽位号)."""
    return {int(e["slot"]): tuple(e["center"]) for e in equipment.extract_equipment(img)}


def _equip_center(centers: dict, slot: int):
    if slot not in centers:
        raise ValueError(f"装备栏第 {slot} 格未识别到装备 (当前有: {sorted(centers)})")
    return centers[slot]


def _is_slot_spec(spec) -> bool:
    if isinstance(spec, int):
        return True
    if isinstance(spec, str):
        return spec.strip().isdigit()
    return False


def resolve_equip(spec, entries: list[dict] | None = None, img=None,
                  exclude_slots=(), context: str = "") -> dict:
    """把"格号"或"装备名字"解析成装备栏里的一件装备 (返回 entries 里的那条 dict).

    为什么按名字时要**现读现解析**: 装备栏的"第 N 格"是"当前已识别装备自上而下"的序号,
    合成/装上/卖出后整体平移, 写死格号会点错. 名字是稳定标识; 同名多件时要求用完整名。
    """
    eqs = entries if entries is not None else equipment.extract_equipment(_shot() if img is None else img)
    excl = {int(s) for s in exclude_slots}
    ctx = f" ({context})" if context else ""
    if _is_slot_spec(spec):
        slot = int(spec)
        for e in eqs:
            if int(e["slot"]) == slot:
                if slot in excl:
                    raise ValueError(f"装备格 {slot} 已被占用{ctx}, 不能重复选同一格")
                return e
        raise ValueError(f"装备栏第 {slot} 格没有装备{ctx} (当前有: {sorted(int(e['slot']) for e in eqs)})")
    want = str(spec).strip()
    if not want:
        raise ValueError(f"装备描述不能为空{ctx}")
    bar = ", ".join(str(e.get("name") or "<未识别>") for e in eqs) or "<空>"
    for e in eqs:                                  # 1) 完整名精确命中
        if int(e["slot"]) in excl:
            continue
        if str(e.get("name") or "") == want:
            return e
    hits = [e for e in eqs                       # 2) 唯一子串命中
            if int(e["slot"]) not in excl and want in str(e.get("name") or "")]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ValueError(f"{want!r} 匹配到多件装备{ctx}, 请用完整名: "
                         + ", ".join(f"格{e['slot']}={e['name']}" for e in hits))
    raise ValueError(f"装备栏里没有 {want!r}{ctx} (当前有: {bar})")


def _extract_equips_with_retry(times: int = SHOP_RENDER_RETRY_TIMES,
                               gap: float = SHOP_RENDER_RETRY_GAP,
                               img=None) -> list[dict]:
    """读装备栏 (界面刚切过来时可能还没渲染好 -> 空结果/异常时重试几次).

    img 非 None 时用这张图 (离线图/干跑), 不做实时截图与重试 (没有"重渲染"这回事).
    """
    if img is not None:
        return equipment.extract_equipment(img)
    last: list[dict] = []
    for i in range(max(1, times)):
        try:
            last = equipment.extract_equipment(_shot())
        except Exception as exc:      # noqa: BLE001 - 重试期间不抛
            _say(f"[!] 读装备栏失败(第{i + 1}次): {type(exc).__name__}: {exc}")
            last = []
        if last:
            return last
        if i + 1 < times:
            time.sleep(gap)
    return last


def _equip_count(eqs: list[dict]) -> int:
    """装备栏里"认出了名字"的件数 (合成的物证判据)."""
    return sum(1 for e in eqs if e.get("name"))


def _unit_at(img, spec) -> dict | None:
    """识别结果里找落在该格/该备战席位的单位 (取 score 最高的一条).

    身份口径见 `_units_yolo`: YOLO 优先 (name 置信度高), 模板图色兜底。
    """
    if isinstance(spec, str):
        spec = parse_spec(spec)
    units = _units_yolo(img)
    cand: list[dict] = []
    for u in list(units.get("board", [])) + list(units.get("bench", [])):
        loc = u.get("location") or {}
        if spec[0] == "board":
            if loc.get("own_row") == spec[1] and loc.get("column") == spec[2]:
                cand.append(u)
        elif loc.get("slot") == spec[1]:
            cand.append(u)
    if not cand:
        return None
    return max(cand, key=lambda u: float(u.get("score") or 0.0))


def _region_diff(before, after, region) -> int:
    """区域像素差 (stride=1, 与源工程 _region_diff 同口径; >30 的 RGB 通道算变化)."""
    return pixel_diff(before, after, region, stride=1)


def _poll_until(probe, *, timeout: float, interval: float = 0.1,
                min_wait: float = 0.0, ok=bool):
    """轮询 probe 直到 ok(值) 为真或超时; 返回 (最后值, 实际耗时, 是否命中)."""
    t0 = time.perf_counter()
    val = None
    while True:
        elapsed = time.perf_counter() - t0
        val = probe()
        if elapsed >= min_wait and ok(val):
            return val, elapsed, True
        if elapsed >= timeout:
            return val, elapsed, False
        time.sleep(interval)


# ---------------------------------------------------------------------------
# 点击 / 拖拽 (统一入口: 有复核, 失败抛 GameError)
# ---------------------------------------------------------------------------

def _click(x, y, *, verify=None, region=None, diff_min: int = 200,
           settle: float = CLICK_SETTLE, dry_run: bool = False,
           label: str = "", note: str | None = None,
           channels=("bg", "fg", "adb")):
    """带兜底的单击: dry_run 只打印计划; 实点失败抛 GameError("CLICK_FAILED")."""
    x, y = int(x), int(y)
    if dry_run:
        click_with_fallback(x, y, verify=verify, region=region, diff_min=diff_min,
                            channels=channels, dry_run=True, note=note)
        return None
    try:
        res = click_with_fallback(x, y, verify=verify, region=region, diff_min=diff_min,
                                  settle=settle, channels=channels, note=note)
    except ClickFailed as exc:
        raise GameError("CLICK_FAILED", f"{label}点击失败: {exc}", EXIT_ACTION) from exc
    return res


def _drag(x1, y1, x2, y2, *, timing: str | None = None, dry_run: bool = False,
          label: str = "", verify_region=None, diff_min: int = DRAG_VERIFY_DIFF_MIN):
    """拖拽 (没有多通道兜底: 拖拽本身只有一种注入路径, 失败就重来或报错).

    timing: "board" = 首次拖拽慢时序 (棋盘上单元移动), "equip" = 装备档时序,
            None = 默认时序 (装备栏内部合成等短拖)。
    """
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    plan = f"{label}拖拽计划: ({x1}, {y1}) -> ({x2}, {y2}) timing={timing or 'default'}"
    if verify_region is not None:
        plan += f" 复核区域={tuple(verify_region)}"
    if dry_run:
        _say(f"[i] {plan} (dry-run, 未执行)")
        return None
    _say(f"[i] {plan}")
    before = None
    if verify_region is not None:
        before = _shot()
    from control.mouse import drag_point
    drag_point(x1, y1, x2, y2, timing=timing)
    if verify_region is None or before is None:
        _say("[OK] 拖拽已发出 (未做复核)")
        return {"verified": None}
    val, waited, hit = _poll_until(
        lambda: _region_diff(before, _shot(), verify_region),
        timeout=KEY_APPLY_WAIT, interval=0.15, min_wait=0.15,
        ok=lambda d: d is not None and d > diff_min)
    if hit:
        _say(f"[OK] 拖拽生效: 复核区域变化 {val} > {diff_min} ({waited:.2f}s)")
        return {"verified": True, "diff": int(val), "waited": round(waited, 2)}
    _say(f"[!] 拖拽后复核区域只变化 {val} (< {diff_min}): 可能没拖到位/目标不可放置")
    return {"verified": False, "diff": int(val), "waited": round(waited, 2)}


# ---------------------------------------------------------------------------
# 复核器: 按动作语义判"生效"
# ---------------------------------------------------------------------------

class _GoldVerifier:
    """买经验: 金币变小才算生效."""

    __doc__ = "买经验: 金币数值变小"

    def __init__(self, before_gold, *, shot=None) -> None:
        self.before_gold = before_gold
        self._shot = shot or _shot
        self.gold_now = None

    def reset(self) -> None:
        return None

    def __call__(self, info: dict | None = None) -> dict:
        try:
            self.gold_now = extract.extract_gold(self._shot())
        except Exception as exc:      # noqa: BLE001
            return {"ok": False, "verify": "gold", "error": f"{type(exc).__name__}: {exc}"}
        ok = (self.gold_now is not None and self.before_gold is not None
              and self.gold_now < self.before_gold)
        return {"ok": ok, "verify": "gold",
                "gold_before": self.before_gold, "gold_after": self.gold_now}


class _ShopRefreshVerifier:
    """刷新商店: 商店卡面区域变化 **且** 金币减少.

    关键坑 (2026-11-06): 金币不足时会弹"金币不足"气泡, 商店区域像素差也能到 800+,
    只看像素差会把"没刷新"误判成成功; 所以差分不够大时再比一次金币。
    """

    __doc__ = "刷新: 商店区域变化且金币减少(金币不足气泡会骗过纯像素差)"

    def __init__(self, *, shot=None) -> None:
        self._shot = shot or _shot
        self.before = None
        self.before_gold = None
        self.diff = 0

    def reset(self) -> None:
        self.before = self._shot()
        try:
            self.before_gold = extract.extract_gold(self.before)
        except Exception:      # noqa: BLE001
            self.before_gold = None

    def __call__(self, info: dict | None = None) -> dict:
        if self.before is None:
            self.reset()
        after = self._shot()
        self.diff = _region_diff(self.before, after, SHOP_DIFF_REGION)
        if self.diff <= SHOP_DIFF_MIN:
            return {"ok": False, "verify": "shop_refresh", "diff": int(self.diff)}
        gold_after = None
        try:
            gold_after = extract.extract_gold(after)
        except Exception:      # noqa: BLE001
            gold_after = None
        if self.diff < SHOP_GOLD_BUBBLE_DIFF:
            ok = (self.before_gold is not None and gold_after is not None
                  and gold_after < self.before_gold)
        else:
            ok = True
        return {"ok": ok, "verify": "shop_refresh", "diff": int(self.diff),
                "gold_before": self.before_gold, "gold_after": gold_after}


# ---------------------------------------------------------------------------
# 信息打印
# ---------------------------------------------------------------------------

def _shop_lines(shop: list[dict]) -> None:
    if not shop:
        _say("  [商店] 空")
        return
    for i, card in enumerate(shop, 1):
        try:
            center = shop_slot_center(i)
        except ValueError:
            center = None
        name = card.get("name")
        price = card.get("price")
        _say(f"  {i}. {name}({price}金) center={center}")


def _print_info(info: dict) -> None:
    for key in _GAME_SHOW_COLS:
        if key in info:
            _say(f"  {key}: {info[key]}  ({INFO_EXPLAINS.get(key, '')})".rstrip())
    for key in ("traits", "players", "shop", "hextech", "equip", "board", "bench",
                "little_legend"):
        if key not in info:
            continue
        value = info[key]
        if key == "shop":
            _say(f"  shop: {len(value or [])} 张")
            _shop_lines(value or [])
        elif key == "equip":
            _say(f"  equip: {len(value or [])} 件")
            for e in value or []:
                _say("    " + f"格{e.get('slot')} {e.get('name')} 数量={e.get('count')} "
                              f"分数={e.get('score')} center={e.get('center')}")
        elif key == "hextech":
            for c in value or []:
                _say(f"    卡{c.get('card_no')}: {c.get('name')} center={c.get('center')} "
                     f"剩余刷新={c.get('refresh_left')}")
        elif key in ("board", "bench", "little_legend"):
            for u in value or []:
                loc = u.get("location") or {}
                where = (f"row{loc.get('row')}/own{loc.get('own_row')}/col{loc.get('column')}"
                         if u in (info.get("board") or []) else f"slot{loc.get('slot')}")
                _say(f"    {u.get('name')} {where} 分数={u.get('score')} "
                     f"center={u.get('center')}")
        else:
            _say(f"  {key}: {value}")


def _unit_to_entries(value: list[dict] | None) -> list[dict]:
    out = []
    for u in value or []:
        loc = u.get("location") or {}
        out.append({"name": u.get("name"), "score": u.get("score"),
                    "center": u.get("center"), "location": loc})
    return out


def parse_info_types(text: str):
    """逗号分隔的类型串 -> 单值或 list (交给 extract.extract_info)."""
    if isinstance(text, (list, tuple)):
        return [str(t).strip() for t in text if str(t).strip()]
    items = [t.strip() for t in str(text or "").split(",") if t.strip()]
    if not items:
        raise ValueError("--info-types 不能为空")
    return items[0] if len(items) == 1 else items


def _info_to_entries(info: dict) -> list[dict]:
    """dict -> [{"key","explain","value"}] (保持识别输出顺序)."""
    out = []
    for key, value in info.items():
        out.append({"key": key, "explain": INFO_EXPLAINS.get(key, ""),
                    "value": _to_jsonable(value)})
    return out


def extract_game_info(img_path: str | None = None, info_types: str = "game") -> list[dict]:
    """识别并打印对局信息 (--json 时返回结构化条目)."""
    img = _shot_or_img(img_path, tag="extract")
    types = parse_info_types(info_types)
    _say(f"[i] 识别类型: {types}")
    info = extract.extract_info(img, types)
    _say("[i] 识别结果:")
    _print_info(info)
    return _info_to_entries(info)


# ---------------------------------------------------------------------------
# 子命令: 识别类
# ---------------------------------------------------------------------------

def cmd_units_ocr(img_path: str | None) -> dict:
    """units --ocr: 走 extract_info 的 Board/Bench 组 (纯图色模板+识别内核)."""
    img = _shot_or_img(img_path, tag="units_ocr")
    info = extract.extract_info(img, [InfoTypes.Board, InfoTypes.Bench])
    _say("[i] extract_info(board/bench) 结果:")
    _print_units(info, "all")
    return {"units": "ocr", "board": _unit_to_entries(info.get("board")),
            "bench": _unit_to_entries(info.get("bench"))}


def _print_units(units: dict, part: str) -> None:
    board_units = units.get("board") or []
    bench_units = units.get("bench") or []
    if part in ("board", "all"):
        _say(f"[棋盘] 识别到 {len(board_units)} 个单位:")
        own = []
        for u in board_units:
            loc = u.get("location") or {}
            if loc.get("own_row") is None:
                continue
            own.append((loc["own_row"], loc["column"], u))
        if not own:
            _say("  (己方半场没有识别到单位)")
        for own_row, column, u in sorted(own, key=lambda t: (t[0], t[1])):
            _say(f"  己方({own_row},{column}) {u.get('name')} 分数={u.get('score')} "
                 f"center={u.get('center')}")
        enemy = [u for u in board_units if (u.get("location") or {}).get("own_row") is None]
        if enemy:
            _say(f"  (另识别到 {len(enemy)} 个未映射到己方格位的单位: 敌方或重叠)")
    if part in ("bench", "all"):
        _say(f"[备战席] 识别到 {len(bench_units)} 个单位:")
        if not bench_units:
            _say("  (空)")
        for u in sorted(bench_units, key=lambda u: ((u.get("location") or {}).get("slot") or 99)):
            loc = u.get("location") or {}
            _say(f"  第{loc.get('slot')}格 {u.get('name')} 分数={u.get('score')} "
                 f"center={u.get('center')}")


def cmd_units_bench(img_path: str | None, *, yolo: bool = False) -> dict:
    img = _shot_or_img(img_path, tag="units_bench")
    t0 = time.perf_counter()
    units = _units_yolo(img)
    _say(f"[i] 识别方式: YOLO 优先 (--yolo={bool(yolo)} 仅兼容旧参数; 空结果才回退模板图色)")
    items = board.extract_bench_items(img)
    _print_units(units, "bench")
    if items:
        _say(f"[备战席道具] {len(items)} 件:")
        for it in items:
            _say(f"  第{it.get('slot')}格 {it.get('name')} 分数={it.get('score')} "
                 f"center={it.get('center')}")
    occupied = len(units.get("bench") or []) + len(items)
    total = len(default_config.BENCH_SLOT_CENTERS)
    _say(f"[i] 备战席占用 {occupied}/{total} (棋子 {len(units.get('bench') or [])} + "
         f"道具 {len(items)}), 耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
    return {"units": "bench", "yolo": bool(yolo), "occupied": occupied, "total": total,
            "units_bench": _unit_to_entries(units.get("bench")), "items": _to_jsonable(items),
            "cost_ms": round((time.perf_counter() - t0) * 1000, 1)}


def cmd_units_board(img_path: str | None) -> dict:
    img = _shot_or_img(img_path, tag="units_board")
    t0 = time.perf_counter()
    units = _units_yolo(img)
    _say("[i] 识别方式: YOLO 优先 (yolo_units.extract_units), 空结果回退模板图色")
    _print_units(units, "board")
    _say(f"[i] 耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
    return {"units": "board", "board": _unit_to_entries(units.get("board")),
            "cost_ms": round((time.perf_counter() - t0) * 1000, 1)}


def cmd_shop(args) -> dict:
    return {"command": "shop", "entries": extract_game_info(args.img, args.info_types)}


def cmd_equip(args) -> dict:
    return {"command": "equip", "entries": extract_game_info(args.img, args.info_types)}


def cmd_extract(args) -> list[dict]:
    return extract_game_info(args.img, args.info_types)


def cmd_cell(args) -> dict:
    """选格/查看某格: 打印格位中心 + 识别结果, 点击后用右侧详情面板复核."""
    spec = parse_spec(args.cell)
    center = resolve_center(spec)
    img = _shot_or_img(args.img, tag="cell")
    _phase_gate(args, what="选格", allow=PHASE_GAME, img=img)
    unit = _unit_at(img, spec)
    _say(f"[i] 格位: {_spec_label(spec)} center={center}")
    if unit:
        _say(f"[i] 该格识别到: {unit.get('name')} 分数={unit.get('score')}")
    else:
        _say("[i] 该格未识别到单位 (空格或识别失败)")
    if args.save_dir:
        try:
            saved = _save_flash(img, f"cell_{spec[0]}_{spec[1]}", parent=args.save_dir)
            _say(f"[i] 截图存档: {saved}")
        except Exception as exc:      # noqa: BLE001
            _say(f"[!] 存档失败: {type(exc).__name__}: {exc}")
    detail_region = regions.DETAIL_CARD
    _say(f"[i] 判定依据: {VERIFY_NOTES['cell']} region={detail_region}")
    res = _click(center[0], center[1], verify=PixelDiffVerifier(
        center[0], center[1], region=detail_region, diff_min=DETAIL_VERIFY_DIFF_MIN),
        region=detail_region, diff_min=DETAIL_VERIFY_DIFF_MIN,
        dry_run=args.dry_run, label="选格", note=VERIFY_NOTES["cell"])
    return {"mode": "cell", "cell": args.cell, "spec": list(spec), "center": list(center),
            "unit": _to_jsonable(unit), "dry_run": bool(args.dry_run),
            "click": res.as_dict() if res else None}


def cmd_bench(args) -> dict:
    """备战席空位统计 (数量 + 每格占用情况)."""
    img = _shot_or_img(args.img, tag="bench")
    t0 = time.perf_counter()
    if args.yolo:
        from recognize import yolo_units
        units = yolo_units.extract_units(img)
    else:
        units = board.extract_units(img)
    items = board.extract_bench_items(img)
    total = len(default_config.BENCH_SLOT_CENTERS)
    occ: dict[int, str] = {}
    for u in units.get("bench") or []:
        slot = (u.get("location") or {}).get("slot")
        if slot:
            occ[int(slot)] = str(u.get("name") or "?")
    for it in items:
        slot = int(it.get("slot"))
        occ[slot] = f"{it.get('name')}(道具)"
    free = total - len({*occ})
    _say(f"[bench] 空位 {free}/{total}, 占用: "
         + " ".join(f"{i}:{occ.get(i, '空')}" for i in range(1, total + 1))
         + f", 耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")
    return {"free": free, "occupied": total - free, "total": total, "slots": occ,
            "cost_ms": round((time.perf_counter() - t0) * 1000, 1)}


# ---------------------------------------------------------------------------
# 子命令: 动作类
# ---------------------------------------------------------------------------

def cmd_buy(args) -> dict:
    """买商店卡: --slot N / --all (1..5)."""
    if args.all:
        slots = list(range(1, len(regions.SHOP_SLOT_LEFTS) + 1))
    elif args.slot is not None:
        slots = [_check(args.slot, 1, len(regions.SHOP_SLOT_LEFTS), "slot")]
    else:
        raise GameError("INVALID_ARGUMENT", "buy 需要 --slot 1..5 或 --all", EXIT_USAGE)
    _say(f"[i] 买卡槽位: {slots}; 判定依据: {VERIFY_NOTES['buy']}")
    _phase_gate(args, what="买卡", allow=PHASE_PREP)
    out = []
    for i, slot in enumerate(slots):
        center = shop_slot_center(slot)
        rect = shop_slot_rect(slot)
        res = _click(center[0], center[1], region=rect, diff_min=200,
                     dry_run=args.dry_run, label=f"买卡槽{slot}",
                     note=VERIFY_NOTES["buy"])
        rec = {"slot": slot, "center": list(center), "verify_region": list(rect),
               "click": res.as_dict() if res else None}
        out.append(rec)
        if not args.dry_run and i + 1 < len(slots):
            time.sleep(default_config.SHOP_BUY_CLICK_GAP)
    return {"slots": out, "dry_run": bool(args.dry_run)}


def cmd_equip_to(args) -> dict:
    """把装备栏里的一件装备拖到某个单位 (棋盘格 / 备战席格)."""
    spec = args.item if args.item is not None else args.slot
    if spec is None:
        raise GameError("INVALID_ARGUMENT", "equip-to 需要 --item <装备名> 或 --slot <格号>",
                        EXIT_USAGE)
    if args.bench is not None:
        target = ("bench", _check(args.bench, 1, len(default_config.BENCH_SLOT_CENTERS), "bench"))
    elif args.row is not None and args.col is not None:
        target = ("board", _check(args.row, 1, 4, "row"),
                  _check(args.col, 1, default_config.BOARD_COLUMNS, "col"))
    else:
        raise GameError("INVALID_ARGUMENT",
                        "equip-to 需要 --bench N, 或 --row R --col C 指定目标单位", EXIT_USAGE)
    img = _shot_or_img(args.img, tag="equip_to")
    _phase_gate(args, what="装备", allow=PHASE_PREP, img=img)
    eqs = _extract_equips_with_retry(img=img)
    ent = resolve_equip(spec, entries=eqs, img=img, context="equip-to")
    start = tuple(int(v) for v in ent["center"])
    end = resolve_center(target)
    rect = tuple(int(v) for v in ent["rect"])
    _say(f"[i] 装备计划: {ent.get('name')}(格{ent.get('slot')}) {start} -> "
         f"{_spec_label(target)} {end}")
    _say(f"[i] 判定依据: {VERIFY_NOTES['equip-to']} region={rect}")
    if args.dry_run:
        _say("[i] (dry-run, 未执行)")
        return {"item": ent.get("name"), "slot": ent.get("slot"), "from": list(start),
                "to": list(end), "target": list(target), "dry_run": True}
    result = _drag(*start, *end, timing="equip", label="装备",
                   verify_region=region_around(*start, half=60))
    return {"item": ent.get("name"), "slot": ent.get("slot"), "from": list(start),
            "to": list(end), "target": list(target), "dry_run": False,
            "drag": _to_jsonable(result)}


def _find_confirm(img, b_rect, *, template_path: str | None = None):
    """在拖拽落点右侧搜"合成确认"按钮; 返回 (center, score) 或 None."""
    path = Path(template_path or str(default_config.PROJECT_ROOT / CONFIRM_TEMPLATE))
    if not path.exists():
        return None
    tpl = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if tpl is None:
        return None
    arr = np.asarray(img.convert("RGB"))[:, :, ::-1].copy()   # RGB -> BGR
    H, W = arr.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in b_rect)
    w = max(1, x2 - x1)
    th, tw = tpl.shape[:2]
    rx1 = max(0, x1)
    ry1 = max(0, y1 - w)
    rx2 = min(W, x2 + CONFIRM_SEARCH_WIDTH * w)
    ry2 = min(H, y2 + w)
    if rx2 - rx1 < tw or ry2 - ry1 < th:
        return None
    roi = arr[ry1:ry2, rx1:rx2]
    score_map = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(score_map)
    if max_val < CONFIRM_THRESHOLD:
        return None
    cx = rx1 + max_loc[0] + tw // 2
    cy = ry1 + max_loc[1] + th // 2
    return (int(cx), int(cy)), round(float(max_val), 3)


def cmd_combine(args) -> dict:
    """两件装备合成一件 (拖 A 到 B -> 等确认弹窗 -> 点确认 -> 装备栏件数复核)."""
    off_img = _load_img(args.img) if args.img else None
    _phase_gate(args, what="合成装备", allow=PHASE_PREP, img=off_img)
    eqs = _extract_equips_with_retry(img=off_img)
    a = resolve_equip(args.a, entries=eqs, context="combine")
    b = resolve_equip(args.b, entries=eqs, exclude_slots={int(a["slot"])}, context="combine")
    if int(a["slot"]) == int(b["slot"]):
        raise ValueError("合成需要两件不同装备")
    start = tuple(int(v) for v in a["center"])
    end = tuple(int(v) for v in b["center"])
    b_rect = tuple(int(v) for v in b["rect"])
    before_count = _equip_count(eqs)
    _say(f"[i] 合成计划: {a.get('name')}(格{a.get('slot')}) -> "
         f"{b.get('name')}(格{b.get('slot')}) {start} -> {end}")
    _say(f"[i] 判定依据: {VERIFY_NOTES['combine']} (合成前件数={before_count})")
    if args.dry_run:
        _say("[i] (dry-run, 未执行)")
        return {"a": a.get("name"), "b": b.get("name"), "from": list(start),
                "to": list(end), "dry_run": True, "equip_count_before": before_count}
    from control.mouse import drag_point
    drag_point(*start, *end)
    t0 = time.perf_counter()
    confirmed = None
    while time.perf_counter() - t0 <= CONFIRM_POPUP_WAIT:
        found = _find_confirm(_shot(), b_rect)
        if found:
            confirmed = found
            _say(f"[OK] 找到合成确认按钮 center={found[0]} score={found[1]}")
            _click(found[0][0], found[0][1], dry_run=False, label="合成确认")
            break
        time.sleep(CONFIRM_RETRY_GAP)
    if confirmed is None:
        _say("[!] 未在超时内找到合成确认弹窗, 去装备栏找物证 (件数是否减少)")
    after_eqs = _extract_equips_with_retry()
    after_count = _equip_count(after_eqs)
    ok = after_count < before_count
    if ok:
        _say(f"[OK] 合成生效: 装备栏件数 {before_count} -> {after_count}")
    else:
        _say(f"[X] 合成未确认: 装备栏件数 {before_count} -> {after_count} (无合成物证)")
    data = {"a": a.get("name"), "b": b.get("name"), "from": list(start),
            "to": list(end), "dry_run": False, "confirm": _to_jsonable(confirmed),
            "equip_count_before": before_count, "equip_count_after": after_count, "ok": ok}
    if not ok:
        raise GameError("ACTION_FAILED",
                        f"合成未确认: 确认弹窗 {'出现' if confirmed else '超时'} 且装备栏无合成物证 "
                        f"(件数 {before_count} -> {after_count})", EXIT_ACTION)
    return data


def cmd_use_remover(args) -> dict:
    """用装备拆卸器把某个单位身上的装备拆下来 (点拆卸器 -> 点该单位)."""
    if args.bench is not None:
        target = ("bench", _check(args.bench, 1, len(default_config.BENCH_SLOT_CENTERS), "bench"))
    elif args.row is not None and args.col is not None:
        target = ("board", _check(args.row, 1, 4, "row"),
                  _check(args.col, 1, default_config.BOARD_COLUMNS, "col"))
    else:
        raise GameError("INVALID_ARGUMENT",
                        "use-remover 需要 --bench N, 或 --row R --col C 指定目标单位", EXIT_USAGE)
    img = _shot_or_img(args.img, tag="use_remover")
    _phase_gate(args, what="拆卸装备", allow=PHASE_PREP, img=img)
    eqs = _extract_equips_with_retry(img=img)
    before_count = _equip_count(eqs)
    ent = resolve_equip(args.item, entries=eqs, context="use-remover")
    remover_pos = tuple(int(v) for v in ent["center"])
    target_center = resolve_center(target)
    _say(f"[i] 拆卸计划: 先点拆卸器 {ent.get('name')}(格{ent.get('slot')}) {remover_pos}, "
         f"再点 {_spec_label(target)} {target_center}")
    _say(f"[i] 判定依据: {VERIFY_NOTES['use-remover']} (当前件数={before_count})")
    if args.dry_run:
        _say("[i] (dry-run, 未执行)")
        return {"item": ent.get("name"), "remover_center": list(remover_pos),
                "target": list(target), "target_center": list(target_center),
                "dry_run": True, "equip_count_before": before_count}
    _click(remover_pos[0], remover_pos[1], region=tuple(int(v) for v in ent["rect"]),
           label="点拆卸器")
    after_click = _extract_equips_with_retry()
    result = _click(target_center[0], target_center[1],
                    verify=PixelDiffVerifier(target_center[0], target_center[1],
                                             region=regions.DETAIL_CARD,
                                             diff_min=DETAIL_VERIFY_DIFF_MIN),
                    region=regions.DETAIL_CARD, label="点目标单位")
    after_eqs = _extract_equips_with_retry()
    after_count = _equip_count(after_eqs)
    ok = after_count > before_count
    _say(f"[{'OK' if ok else 'X'}] 拆卸结果: 装备栏件数 {before_count} -> {after_count}")
    return {"item": ent.get("name"), "remove_click": result.as_dict() if result else None,
            "target": list(target), "dry_run": False, "equip_count_before": before_count,
            "equip_count_after": after_count, "ok": ok}


def cmd_place(args) -> dict:
    """把备战席上的单位拖上场 (bench,N -> board,R,C)."""
    slot = _check(args.slot, 1, len(default_config.BENCH_SLOT_CENTERS), "slot")
    row = _check(args.row, 1, 4, "row")
    col = _check(args.col, 1, default_config.BOARD_COLUMNS, "col")
    start = bench_slot_center(slot)
    end = board_cell_center(row, col)
    _say(f"[i] 上场计划: 备战席第{slot}格 {start} -> 己方棋盘({row},{col}) {end}")
    _phase_gate(args, what="上场", allow=PHASE_PREP)
    _say(f"[i] 判定依据: {VERIFY_NOTES['place']} region={region_around(*start, half=70)}")
    result = _drag(*start, *end, timing="board", dry_run=args.dry_run, label="上场",
                   verify_region=region_around(*start, half=70))
    return {"from": list(start), "to": list(end), "dry_run": bool(args.dry_run),
            "drag": _to_jsonable(result)}


def cmd_swap(args) -> dict:
    """两个格位互换 (棋盘<->棋盘 / 棋盘<->备战席)."""
    src, dst = args.src, args.dst
    start = resolve_center(src)
    end = resolve_center(dst)
    _say(f"[i] 交换计划: {src} {start} -> {dst} {end}")
    _phase_gate(args, what="交换", allow=PHASE_PREP)
    _say(f"[i] 判定依据: {VERIFY_NOTES['swap']} region={region_around(*start, half=70)}")
    result = _drag(*start, *end, timing="board", dry_run=args.dry_run, label="交换",
                   verify_region=region_around(*start, half=70))
    return {"from": src, "to": dst, "from_center": list(start), "to_center": list(end),
            "dry_run": bool(args.dry_run), "drag": _to_jsonable(result)}


def cmd_sell(args) -> dict:
    """卖单位: 拖到商店区 (--bench N 或 --row R --col C)."""
    if args.bench is not None:
        slot = _check(args.bench, 1, len(default_config.BENCH_SLOT_CENTERS), "bench")
        start = bench_slot_center(slot)
        src = f"备战席第{slot}格"
    elif args.row is not None and args.col is not None:
        start = board_cell_center(args.row, args.col)
        src = f"己方({_check(args.row, 1, 4, 'row')},{_check(args.col, 1, 7, 'col')})"
    else:
        raise GameError("INVALID_ARGUMENT", "sell 需要 --bench N, 或 --row R --col C",
                        EXIT_USAGE)
    end = shop_center()
    _say(f"[i] 卖出计划: {src} {start} -> 商店中心 {end}")
    _phase_gate(args, what="卖出", allow=PHASE_PREP)
    _say(f"[i] 判定依据: {VERIFY_NOTES['sell']} region={region_around(*start, half=70)}")
    result = _drag(*start, *end, dry_run=args.dry_run, label="卖出",
                   verify_region=region_around(*start, half=70))
    return {"target": src, "from": list(start), "to": list(end),
            "dry_run": bool(args.dry_run), "drag": _to_jsonable(result)}


def cmd_refresh(args) -> dict:
    """刷新商店 (--times N): 点击 + 商店区复核 + 金币兜底判据."""
    times = max(1, int(args.times))
    x1, y1, x2, y2 = regions.REFRESH_BUTTON
    center = ((x1 + x2) // 2, (y1 + y2) // 2)
    _say(f"[i] 刷新按钮 region={regions.REFRESH_BUTTON} center={center} 次数={times}")
    _say(f"[i] 判定依据: {VERIFY_NOTES['refresh']}")
    _phase_gate(args, what="刷新商店", allow=PHASE_PREP)
    if args.dry_run:
        click_with_fallback(center[0], center[1], dry_run=True)
        _say("[i] (dry-run, 未执行)")
        return {"times": times, "center": list(center), "dry_run": True}
    verifier = _ShopRefreshVerifier()
    verifier.reset()
    gold_before = verifier.before_gold
    for i in range(times):
        time.sleep(LEVELUP_CLICK_GAP)
        _click(center[0], center[1], verify=verifier, label=f"刷新第{i + 1}次",
           note=VERIFY_NOTES["refresh"])
    val, waited, hit = _poll_until(lambda: _ShopRefreshVerifier.__call__(verifier),
                                   timeout=KEY_APPLY_WAIT, interval=0.2, min_wait=0.2,
                                   ok=lambda d: bool(d and d.get("ok")))
    if not hit:
        _say(f"[!] 刷新复核未通过: {val} (等待 {waited:.2f}s)")
    return {"times": times, "hit": bool(hit), "verify": _to_jsonable(val),
            "gold_before": gold_before, "dry_run": False}


def cmd_search(args) -> dict:
    """刷一次牌并报告商店/备战席/阶段状态 (便于脚本判断)."""
    times = max(1, int(args.times))
    shop_before = extract.extract_info(_shot(), InfoTypes.Shop).get("shop") if not args.dry_run else []
    if args.dry_run:
        refresh = {"times": 0, "dry_run": True}
    else:
        refresh = cmd_refresh(argparse.Namespace(times=times, dry_run=False, json=args.json,
                                                 ld_index=args.ld_index, img=args.img))
    img = _shot_or_img(args.img, tag="search")
    info = extract.extract_info(img, [InfoTypes.Shop, InfoTypes.Gold, InfoTypes.Level,
                                      InfoTypes.Stage, InfoTypes.Phase, InfoTypes.RoundTime])
    shop = info.get("shop") or []
    names_before = [c.get("name") for c in (shop_before or [])]
    names_after = [c.get("name") for c in shop]
    bench_res = cmd_bench(argparse.Namespace(img=args.img, yolo=False, json=args.json,
                                             ld_index=args.ld_index, dry_run=args.dry_run))
    changed = bool(shop_before) and names_before != names_after
    _say(f"[search] stage={info.get('stage')} phase={info.get('phase')} "
         f"round_time={info.get('round_time')} gold={info.get('gold')} level={info.get('level')}")
    _say(f"[search] 备战席空位={bench_res['free']}/{bench_res['total']} "
         f"商店变化={changed}")
    _shop_lines(shop)
    return {"times": times, "dry_run": bool(args.dry_run), "refresh": _to_jsonable(refresh),
            "changed": changed, "before_names": names_before, "shop": _to_jsonable(shop),
            "stage": info.get("stage"), "phase": info.get("phase"),
            "round_time": info.get("round_time"), "bench": bench_res,
            "gold": info.get("gold"), "level": info.get("level")}


def cmd_levelup(args) -> dict:
    """买经验 (--times N): 金币减少才算生效."""
    times = max(1, int(args.times))
    x1, y1, x2, y2 = regions.XP_BUY_BUTTON
    center = ((x1 + x2) // 2, (y1 + y2) // 2)
    _say(f"[i] 买经验按钮 region={regions.XP_BUY_BUTTON} center={center} 次数={times}")
    _say(f"[i] 判定依据: {VERIFY_NOTES['levelup']}")
    img0 = None
    try:
        img0 = _shot_or_img(args.img, tag="levelup")
    except GameError:
        if not args.dry_run:
            raise
    _phase_gate(args, what="买经验", allow=PHASE_PREP, img=img0)
    gold0 = extract.extract_info(img0, InfoTypes.Gold).get("gold") if img0 is not None else None
    _say(f"[i] 当前金币={gold0}")
    if args.dry_run:
        click_with_fallback(center[0], center[1], dry_run=True)
        return {"times": times, "center": list(center), "gold_before": gold0, "dry_run": True}
    if gold0 is not None and gold0 < 4:
        _say(f"[!] 金币 {gold0} < 4, 买不起经验, 放弃")
        return {"times": 0, "gold_before": gold0, "gold_after": gold0, "hit": False,
                "dry_run": False}
    verifier = _GoldVerifier(gold0)
    tries = 0
    for i in range(times):
        tries += 1
        _click(center[0], center[1], verify=verifier, label=f"买经验第{i + 1}次",
           note=VERIFY_NOTES["levelup"])
        time.sleep(LEVELUP_CLICK_GAP)
    gold1, waited, hit = _poll_until(lambda: extract.extract_gold(_shot()),
                                     timeout=KEY_APPLY_WAIT, interval=0.2, min_wait=0.3,
                                     ok=lambda g: g is not None and gold0 is not None and g < gold0)
    if not hit:
        _say(f"[!] 金币未减少 ({gold0} -> {gold1}), 买经验可能未生效")
    return {"times": tries, "gold_before": gold0, "gold_after": gold1, "hit": bool(hit),
            "waited": round(waited, 2), "dry_run": False}


def cmd_hex(args) -> dict:
    """海克斯选择: --pick N 选第 N 张 / --refresh N 刷新第 N 张."""
    if (args.pick is None) == (args.refresh is None):
        raise GameError("INVALID_ARGUMENT", "hex 需要 --pick N 或 --refresh N (二选一)",
                        EXIT_USAGE)
    n = args.pick if args.pick is not None else args.refresh
    n = _check(n, 1, len(augment.CARD_TEXT_REGIONS), "n")
    img = _shot_or_img(args.img, tag="hex")
    _phase_gate(args, what="海克斯", allow=PHASE_AUGMENT, img=img)
    aug = augment.extract(img)
    if not aug.get("is_augment"):
        raise GameError("NOT_AUGMENT_SCREEN", "当前不是海克斯选择界面", EXIT_STATE)
    if aug.get("chosen"):
        raise GameError("ALREADY_CHOSEN", "已完成海克斯选择, 不能重复操作", EXIT_STATE)
    card = aug["cards"][n - 1]
    _say(f"[i] 海克斯卡{n}: {card.get('name')} center={card.get('center')} "
         f"剩余刷新={card.get('refresh_left')}")
    if args.pick is not None:
        _say(f"[i] 判定依据: {VERIFY_NOTES['hex-pick']}")
        if args.dry_run:
            click_with_fallback(*tuple(int(v) for v in card["center"]), dry_run=True,
                                note=VERIFY_NOTES["hex-pick"])
            return {"action": "pick", "n": n, "center": list(card["center"]),
                    "dry_run": True}
        center = tuple(int(v) for v in card["center"])
        _click(center[0], center[1], label="选海克斯")
        for attempt, gap in ((1, 0.4), (2, 0.8)):
            time.sleep(gap)
            after = augment.extract(_shot())
            if after.get("chosen"):
                _say(f"[OK] 海克斯选择已生效 (第{attempt}次复核)")
                return {"action": "pick", "n": n, "ok": True, "attempts": attempt}
        _say("[!] 点完海克斯卡后 chosen 仍为 False (可能点偏/未响应)")
        return {"action": "pick", "n": n, "ok": False, "attempts": 2}
    # refresh
    x1, y1, x2, y2 = augment.REFRESH_DIGIT_REGIONS[n - 1]
    refresh_center = ((x1 + x2) // 2, (y1 + y2) // 2)
    left0 = card.get("refresh_left")
    names0 = [c.get("name") for c in aug["cards"]]
    _say(f"[i] 刷新第{n}张: 按钮 center={refresh_center} 刷新前剩余={left0}")
    if args.dry_run:
        click_with_fallback(refresh_center[0], refresh_center[1], dry_run=True,
                            note=VERIFY_NOTES["hex-refresh"])
        return {"action": "refresh", "n": n, "center": list(refresh_center),
                "refresh_left_before": left0, "dry_run": True}
    _click(refresh_center[0], refresh_center[1], label="刷新海克斯")
    time.sleep(KEY_APPLY_WAIT)
    after = augment.extract(_shot())
    left1 = after["cards"][n - 1].get("refresh_left")
    hit = (left0 is not None and left1 is not None and left1 < left0) \
        or [c.get("name") for c in after["cards"]] != names0
    _say(f"[{'OK' if hit else '!'}] 刷新后剩余={left1}, 卡面变化={hit}")
    return {"action": "refresh", "n": n, "refresh_left_before": left0,
            "refresh_left_after": left1, "cards_changed": hit, "ok": bool(hit)}


def cmd_balls(args) -> dict:
    """捡奖励球 (--rounds N): 识别 -> 排序 -> 点击 -> 球消失复核."""
    _say(f"[i] 捡球开始: max_rounds={args.rounds} 判定依据: {VERIFY_NOTES['balls']}")
    _phase_gate(args, what="捡球", allow=PHASE_GAME)
    picked = collect_balls(max_rounds=args.rounds, dry_run=args.dry_run)
    print_balls(picked)
    return {"picked": _to_jsonable(picked), "dry_run": bool(args.dry_run)}


# ---------------------------------------------------------------------------
# 未实现命令 (纯图色链路不适配)
# ---------------------------------------------------------------------------

NOT_IMPLEMENTED = {
    "search-buy": "自动刷牌买卡: 依赖「识别-刷新-买-回退」的连续对局状态机与金币预算模型, "
                  "本工程只保留单步动作 (需要时用 refresh + buy 组合)",
    "start": "启动游戏: 依赖窗口尺寸初始化与自动开局脚本 (utils.window_size / auto_start_game), "
             "新工程未移植; 请手动打开模拟器与对局",
    "exit": "退出对局: 依赖关卡轮转与结算页识别 (restart_after_stage), 新工程未移植",
    "blessing": "野怪赐福: 依赖 recognize/beast.py 专用识别模块与优先级配置, 新工程没有该模块",
}


def _not_implemented(name: str) -> None:
    raise GameError("NOT_IMPLEMENTED", f"{name} 未实现: {NOT_IMPLEMENTED[name]}", EXIT_USAGE)


def cmd_blessing(args) -> dict:
    _not_implemented("blessing")


def cmd_start(args) -> dict:
    _not_implemented("start")


def cmd_exit(args) -> dict:
    _not_implemented("exit")


def cmd_search_buy(args) -> dict:
    _not_implemented("search-buy")


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", nargs="?", default=None, type=str,
                        help=argparse.SUPPRESS)   # 兼容源工程: 带值即 debug 截图
    common.add_argument("--json", action="store_true", help="stdout 只输出一行 JSON, 人类输出转 stderr")
    common.add_argument("--ld-index", type=int, default=None,
                        help="指定雷电实例号 (等价 config.LDPLAYER_WINDOW=该实例, 影响 adb 通道)")
    common.add_argument("--img", default=None, help="用离线图片代替实时截图 (纯识别命令/干跑用)")
    common.add_argument("--dry-run", action="store_true", help="只打印计划(坐标+判定依据), 不执行任何点击/拖拽")
    # 注意: --info-types **不放在 common 里** —— 子命令共用同一个 action 对象时,
    # 某个子命令的 set_defaults 会污染其它子命令 (实测 shop/equip 会互相窜), 所以每个
    # 子命令自己声明默认值。

    p = argparse.ArgumentParser(
        prog="python -X utf8 -u -m scripts.game",
        description="纯图色版游戏操作脚手架: 识别看板子, 点击带兜底与效果复核",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="未实现: search-buy / start / exit / blessing (纯图色链路不适配, 见 --help 各子命令说明)")
    p.add_argument("--img", default=None, help="离线图片路径 (识别/干跑用)")
    p.add_argument("--info-types", default="game", help="默认识别类型 (缺省 game)")
    sub = p.add_subparsers(dest="cmd", required=False)

    def _sub(name, help_text):
        s = sub.add_parser(name, parents=[common], help=help_text,
                           description=help_text, formatter_class=argparse.RawDescriptionHelpFormatter)
        return s

    s = _sub("extract", "识别图片信息 (默认 game 组; 不写子命令即等价于此命令)")
    s.add_argument("--info-types", default="game",
                   help="识别类型, 逗号分隔 (缺省 game; 例: shop,game / equip)")

    g = _sub("units", "识别场上奕子/备战席 (--ocr / --bench / --board)")
    mx = g.add_mutually_exclusive_group()
    mx.add_argument("--ocr", action="store_true", help="走 extract_info 的 board/bench 组")
    mx.add_argument("--bench", action="store_true", help="只看备战席 (含道具)")
    mx.add_argument("--board", action="store_true", help="只看己方棋盘")

    s = _sub("shop", "识别商店 5 张卡 (默认 info-types=shop,game)")
    s.add_argument("--info-types", default="shop,game",
                   help="识别类型, 逗号分隔 (缺省 shop,game)")
    s = _sub("equip", "识别左侧装备栏 (默认 info-types=equip)")
    s.add_argument("--info-types", default="equip",
                   help="识别类型, 逗号分隔 (缺省 equip)")

    s = _sub("cell", "查看/选中某个格位 (board,行,列 或 bench,格)")
    s.add_argument("cell", help="位置描述, 如 board,3,4 / bench,3")
    s.add_argument("--save-dir", default=None, help="截图存档目录 (缺省 workspace/captures)")

    s = _sub("buy", "买商店卡 --slot N / --all (点后复核槽位卡面消失)")
    s.add_argument("--slot", type=int, default=None, help="商店卡槽位 1..5")
    s.add_argument("--all", action="store_true", help="全部 5 个槽位各点一次")

    s = _sub("equip-to", "把装备栏里的装备拖给某个单位")
    s.add_argument("--item", default=None, help="装备名 (按名字解析, 推荐; 支持唯一子串)")
    s.add_argument("--slot", type=int, default=None, help="装备栏格号 1..N (会随合成平移, 慎用)")
    s.add_argument("--row", type=int, default=None, help="目标: 己方棋盘行 1..4")
    s.add_argument("--col", type=int, default=None, help="目标: 棋盘列 1..7")
    s.add_argument("--bench", type=int, default=None, help="目标: 备战席格 1..9")

    s = _sub("combine", "两件装备合成 (拖 A 到 B, 点确认, 装备栏件数复核)")
    s.add_argument("--a", required=True, help="装备 A (名字或格号)")
    s.add_argument("--b", required=True, help="装备 B (名字或格号)")

    s = _sub("use-remover", "用拆卸器拆下某单位身上装备 (先点拆卸器再点单位)")
    s.add_argument("--item", default="拆卸器", help="拆卸器在装备栏里的名字 (缺省 拆卸器)")
    s.add_argument("--row", type=int, default=None, help="目标: 己方棋盘行 1..4")
    s.add_argument("--col", type=int, default=None, help="目标: 棋盘列 1..7")
    s.add_argument("--bench", type=int, default=None, help="目标: 备战席格 1..9")

    s = _sub("place", "备战席单位上场 (--slot -> --row/--col)")
    s.add_argument("--slot", type=int, required=True, help="备战席格 1..9")
    s.add_argument("--row", type=int, required=True, help="己方棋盘行 1..4")
    s.add_argument("--col", type=int, required=True, help="棋盘列 1..7")

    s = _sub("swap", "两个格位互换 (board,行,列 / bench,格)")
    s.add_argument("--src", required=True, help="源位置, 如 board,2,3")
    s.add_argument("--dst", required=True, help="目标位置, 如 bench,1")

    s = _sub("sell", "卖单位 (拖到商店区): --bench N 或 --row R --col C")
    s.add_argument("--bench", type=int, default=None, help="备战席格 1..9")
    s.add_argument("--row", type=int, default=None, help="己方棋盘行 1..4")
    s.add_argument("--col", type=int, default=None, help="棋盘列 1..7")

    s = _sub("refresh", "刷新商店 --times N (复核商店变化 + 金币减少)")
    s.add_argument("--times", type=int, default=1, help="刷新次数 (缺省 1)")

    s = _sub("search", "刷一次牌并把商店/备战席/阶段状态报出来")
    s.add_argument("--times", type=int, default=1, help="刷新次数 (缺省 1)")

    s = _sub("bench", "备战席空位统计 (--yolo 用 YOLO 识别)")
    s.add_argument("--yolo", action="store_true", help="用 YOLO 识别代替模板图色")

    s = _sub("levelup", "买经验 --times N (复核金币减少)")
    s.add_argument("--times", type=int, default=1, help="买几次 (缺省 1)")

    s = _sub("hex", "海克斯选择: --pick N 选第 N 张 / --refresh N 刷新第 N 张")
    s.add_argument("--pick", type=int, default=None, help="选第 N 张 (1..3)")
    s.add_argument("--refresh", type=int, default=None, help="刷新第 N 张 (1..3)")

    s = _sub("balls", "捡奖励球 --rounds N (点后复核球消失)")
    s.add_argument("--rounds", type=int, default=3, help="最多识别-点击轮数 (缺省 3)")

    for name in ("search-buy", "start", "exit", "blessing"):
        s = _sub(name, f"[未实现] {NOT_IMPLEMENTED[name]}")
        if name == "search-buy":
            s.add_argument("--targets", default=None, help="(未实现) 目标卡名")
            s.add_argument("--max-gold", type=float, default=20.0, help="(未实现)")
            s.add_argument("--keep-gold", type=float, default=0.0, help="(未实现)")
            s.add_argument("--max-refresh", type=int, default=10, help="(未实现)")
        elif name == "start":
            s.add_argument("--timeout", type=float, default=600.0, help="(未实现)")
        elif name == "blessing":
            s.add_argument("--priority", default=None, help="(未实现)")
    return p


def _apply_ld_index(args) -> None:
    """--ld-index: 把指定雷电实例提到 find_ldplayer()[0] (adb 通道随之指向它)."""
    idx = getattr(args, "ld_index", None)
    if idx is None:
        return
    if int(idx) < 0:
        raise GameError("INVALID_ARGUMENT", f"--ld-index={idx} 不能为负", EXIT_USAGE)
    default_config.LDPLAYER_WINDOW = str(int(idx))
    _say(f"[i] 指定雷电实例: {int(idx)} (config.LDPLAYER_WINDOW={default_config.LDPLAYER_WINDOW})")


def _dispatch(args, parser):
    cmd = args.cmd
    if cmd is None:
        return cmd_extract(args)
    if cmd == "extract":
        return cmd_extract(args)
    if cmd == "units":
        if args.ocr or (args.img and not args.bench and not args.board):
            return cmd_units_ocr(args.img)
        if args.bench:
            return cmd_units_bench(args.img, yolo=False)
        if args.board:
            return cmd_units_board(args.img)
        _say("[i] units 需要 --ocr/--bench/--board 之一; 缺省按 --board 处理")
        return cmd_units_board(args.img)
    if cmd == "shop":
        return cmd_shop(args)
    if cmd == "equip":
        return cmd_equip(args)
    if cmd == "cell":
        return cmd_cell(args)
    if cmd == "buy":
        return cmd_buy(args)
    if cmd == "equip-to":
        return cmd_equip_to(args)
    if cmd == "combine":
        return cmd_combine(args)
    if cmd == "use-remover":
        return cmd_use_remover(args)
    if cmd == "place":
        return cmd_place(args)
    if cmd == "swap":
        return cmd_swap(args)
    if cmd == "sell":
        return cmd_sell(args)
    if cmd == "refresh":
        return cmd_refresh(args)
    if cmd == "search":
        return cmd_search(args)
    if cmd == "bench":
        return cmd_bench(args)
    if cmd == "levelup":
        return cmd_levelup(args)
    if cmd == "hex":
        return cmd_hex(args)
    if cmd == "balls":
        return cmd_balls(args)
    if cmd == "search-buy":
        return cmd_search_buy(args)
    if cmd == "start":
        return cmd_start(args)
    if cmd == "exit":
        return cmd_exit(args)
    if cmd == "blessing":
        return cmd_blessing(args)
    raise GameError("UNKNOWN_COMMAND", f"未知子命令 {cmd!r}", EXIT_USAGE)


def _run_json(args, parser, real_stdout=None) -> int:
    """--json: stdout 只留一行 JSON, 人类输出全部进 stderr."""
    real_stdout = real_stdout if real_stdout is not None else sys.stdout
    payload_cmd = args.cmd or "extract"
    try:
        with redirect_stdout(sys.stderr):
            data = _dispatch(args, parser)
        payload = {"ok": True, "cmd": payload_cmd, "data": _to_jsonable(data)}
        code = EXIT_OK
    except SystemExit:
        raise
    except BaseException as exc:      # noqa: BLE001 - json 模式要统一成结构化错误
        code_name, code = _error_code_for(exc)
        payload = {"ok": False, "cmd": payload_cmd,
                   "error": {"code": code_name, "exit_code": code,
                             "message": f"{type(exc).__name__}: {exc}"}}
    text = json.dumps(payload, ensure_ascii=False)
    try:                              # cp936 终端等编不出中文时退回 \uXXXX
        text.encode(real_stdout.encoding or "utf-8")
    except Exception:                 # noqa: BLE001
        text = json.dumps(payload, ensure_ascii=True)
    real_stdout.write(text + "\n")
    real_stdout.flush()
    return code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # --json 时必须在**解析参数之前**就把 stdout 整体挪到 stderr: 识别层 (onnxruntime/
    # 顶层 import) 会绕过 sys.stdout 直接写 fd 1, 实测 yolo 载入日志会污染那一行 JSON。
    real_stdout = sys.stdout
    if "--json" in argv:
        #  保存一份"真正的 stdout" fd, 再把 fd 1 指到 stderr —— 这样 ONNXRuntime / 顶层
        #  import 这些绕过 sys.stdout 直接写 fd 1 的日志也进不了 JSON; 最后用 real_stdout 写那一行。
        saved_fd = None
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            saved_fd = os.dup(sys.stdout.fileno())
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):   # 无真实 fd (被重定向到内存的测试场景)
            if saved_fd is not None:
                try:
                    os.close(saved_fd)
                except OSError:
                    pass
            saved_fd = None
        if saved_fd is not None:
            real_stdout = os.fdopen(saved_fd, "w",
                                    encoding=(sys.stderr.encoding or "utf-8"),
                                    errors="replace")
        sys.stdout = sys.stderr
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "info_types", None) is None:
        args.info_types = "game"
    try:
        _apply_ld_index(args)
    except GameError as exc:
        _say(f"[X] {exc}")
        return exc.exit_code
    if args.json:
        return _run_json(args, parser, real_stdout=real_stdout)
    t0 = time.perf_counter()
    try:
        if args.debug:
            debug_shot()
        data = _dispatch(args, parser)
    except GameError as exc:
        _say(f"[X] {exc.code}: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        _say("[X] 已中断 (Ctrl+C)")
        return EXIT_STATE
    except RuntimeError as exc:       # 截图/窗口层的 RuntimeError 也是给人看的中文
        _say(f"[X] 运行失败: {exc}")
        return EXIT_STATE
    except ValueError as exc:
        _say(f"[X] 参数错误: {exc}")
        return EXIT_USAGE
    _say(f"[OK] {args.cmd or 'extract'} 完成, 耗时 {time.perf_counter() - t0:.2f}s")
    if data is not None:
        _say(f"[i] 结构化结果: {len(data) if hasattr(data, '__len__') else 1} 项 "
             "(加 --json 取完整 JSON)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
