#!/usr/bin/env python
"""Overlay 客户端 (Tk GUI): 勾选要显示哪几类框 + 每类自己的刷新间隔, 点「应用」即时生效.

它就是把 `scripts/overlay_info.py` 那条"截图 -> 识别 -> 画到 overlay 窗口"的循环, 换成一个
可以随时改配置的图形外壳: 勾上/取消某个勾选框、改一下间隔输入框, 按右下角「应用」, 下一个
绘制节拍 (<=0.5s) 就按新配置跑, 不用重启进程、不用改 config.py。

显示项 (默认全勾):
    棋子框 (棋盘)      OVERLAY_SHOW_UNITS           YOLO 棋子 + 名字 + 星级 + 星级徽记
    备战席框           OVERLAY_SHOW_BENCH_UNITS     备战席格子里的棋子 (客户端私有开关)
    商店 (识别+区域框) OVERLAY_SHOW_SHOP           5 张卡的卡面 OCR, 勾上才会去跑 OCR
    棋子身上装备       OVERLAY_SHOW_BOARD_EQUIPS   血条上方那几枚图标
    装备栏装备         OVERLAY_SHOW_EQUIP_BAR      左侧库存面板的格子

纯图色: 只截图 + 识别 + 画, 本文件全文没有 control.* 的导入, 绝不点击/按键。

刷新间隔的下限 (秒) 由实测识别耗时决定, 填得再小也会被钳到下限并给提示:
    棋子 0.4 / 商店 1.0 / 装备栏 1.5 / 棋子携带装备 2.5

用法:
    python -X utf8 -u -m scripts.overlay_client                 # 连雷电模拟器 (默认序号)
    python -X utf8 -u -m scripts.overlay_client --ld-index 0
    python -X utf8 -u -m scripts.overlay_client --img tests/images/board_ingame.png   # 离线看效果
    python -X utf8 -u -m scripts.overlay_client --selftest       # 无头自检 (不弹窗)
"""

from __future__ import annotations

# 直接运行支持: `python scripts/overlay_client.py ...` 与 `python -m scripts.overlay_client ...` 等价。
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk

from PIL import Image

from config.config import default_config
from overlay import debug_overlay, live_draw

TARGET_SIZE = (1920, 1080)
EMPTY_UNITS = {"board": [], "bench": []}

# 只在"对局画面"里画: 大厅/登录/选英雄/结算 的界面美术会被当成球和棋子 (实测大厅帧 23 个假球)
IN_GAME_PHASES = {"prep", "combat", "augment", "draft"}
PHASE_INTERVAL = 2.0
GRAB_INTERVAL = 0.1      # 抓帧 + 重画 (10fps)      # 阶段判定间隔 (phase.detect 25~48ms)
UI_POLL_MS = 200          # 状态栏刷新周期

# 状态行/提示行的固定像素盒子 (见 App._text_box): 文本每 200ms 变一次,
# 用会自己换行的 Label 会让窗口高度跟着跳, 所以固定成像素盒, 多出来的裁掉。
TEXT_BOX_W = 430
STATUS_BOX_H = 96      # 约 5 行 9pt (帧号/耗时/本次重算/贴图或错误, 长句会折行)
LOG_BOX_H = 96        # 约 6 行 9pt (日志是追加的, 留高一点看得见历史)
LOG_MAX_LINES = 200    # 超出就从头删, 防止长时间运行把内存吃满

FONT = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 12, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_MONO = ("Cascadia Mono", 9)

# ---- 深色主题 (VS Code Dark+ 近似色) ----
BG = "#1e1e1e"          # 窗口底
BG_BOX = "#252526"      # 输入框 / 文本框底 (比窗口略亮一层)
BG_BTN = "#333337"
BG_BTN_HOVER = "#3f3f46"
FG = "#d4d4d4"          # 主文字
FG_DIM = "#8f8f8f"      # 次要说明
FG_TITLE = "#ffffff"
ACCENT = "#0e639c"      # 主按钮
ACCENT_HOVER = "#1177bb"
BORDER = "#3c3c3c"
OK_GREEN = "#6a9955"    # 成功提示 (暗色下的绿)
ERR_RED = "#f48771"     # 错误提示


@dataclass(frozen=True)
class Item:
    """一个可勾选的显示项.

    Attributes:
        key: 内部标识 (设置 dict 的键).
        label: 界面上显示的中文名.
        show_key: 传给 `debug_overlay.draw_frame(show=...)` 的键.
        recog: 归属的识别项 (多个显示项可以共用一个识别项, 如棋盘框/备战席框都是 units).
        interval: 默认刷新间隔 (秒).
        min_interval: 间隔下限 (秒), 按实测识别耗时定.
        tip: 鼠标悬停提示.
        group: "visual" (图色/YOLO) 或 "ocr" (OCR 读数字), 只影响界面上怎么分段.
    """

    key: str
    label: str
    show_key: str
    recog: str
    interval: float
    min_interval: float
    tip: str = ""
    group: str = "visual"


ITEMS: tuple[Item, ...] = (
    Item("units_board", "棋子框 (棋盘)", "OVERLAY_SHOW_UNITS", "units",
         0.1, 0.05, "YOLO 棋子 + 名字 + 星级, 以及血条左侧那枚星级徽记锚点"),
    Item("units_bench", "备战席框", "OVERLAY_SHOW_BENCH_UNITS", "units",
         0.1, 0.05, "备战席格子里的棋子 (与棋盘共用一次 YOLO 推理)"),
    Item("shop", "商店 (识别 + 区域框)", "OVERLAY_SHOW_SHOP", "shop",
         1.5, 1.0, "5 张卡的卡面 OCR; 勾上才会去跑 OCR, 不勾可省 ~0.6s/次"),
    Item("board_equips", "棋子身上装备", "OVERLAY_SHOW_BOARD_EQUIPS", "board_equips",
         2.0, 1.0, "血条上方那几枚装备图标 (依赖血条定位, 最慢的一项)"),
    Item("equip_bar", "装备栏装备", "OVERLAY_SHOW_EQUIP_BAR", "equip_bar",
         1.0, 0.5, "左侧库存面板的格子与件数"),
    # ---- OCR 信息 (同一次 OCR 读出, 分两段画到 overlay 顶栏) ----
    Item("game_time", "时间信息 (回合/阶段/倒计时)", "OVERLAY_SHOW_GAME_TIME", "game",
         1.0, 0.5, "OCR 读顶栏: 3-6 / 准备 / 剩余20s; 画在 overlay 最上面那行", group="ocr"),
    Item("game_econ", "经济信息 (等级/经验/金币/刷新)", "OVERLAY_SHOW_GAME_ECON", "game",
         1.0, 0.5, "OCR 读: Lv5 经验10/18 金币103 免费刷新10 买经验4", group="ocr"),
)

# 识别项顺序: 棋子/血条 先跑 (后面的棋子携带装备要拿血条框定位); prompt 最慢, 放最后
RECOG_ORDER = ("units", "bars", "game", "shop", "equip_bar", "board_equips", "prompt")

# 结构化文本预览: 面板尺寸 (放在选项下方, 展开时向下延伸) + 生成节拍
PROMPT_W = 640
PROMPT_H = 560

# 两个阈值的合法区间 (UI 里填超了就钳到边界); 检出置信度下限 2026-11-21 用户指定 0.01
CONF_RANGE = (0.01, 0.95)
MIN_SCORE_RANGE = (0.05, 0.99)


def default_thresholds() -> dict:
    """识别阈值的初值 (UI 上可改, 改完点「应用」立即生效).

    * `conf`: 检出置信度 —— 低于它的框**根本不检出**; 默认取 `config.YOLO_UNITS_CONF`
      (0.35, 与父项目一致), 界面上可调, 下限 0.01 (2026-11-21 用户指定).
    * `min_score`: 未知阈值 —— 检出框的分数低于它**不写名字**, 标成 unknown 走点击确认兜底;
      默认取 `config.YOLO_UNITS_MIN_SCORE` (0.45), 与 config 默认值一致.
    """
    return {
        "conf": float(getattr(default_config, "YOLO_UNITS_CONF", 0.35)),
        "min_score": float(getattr(default_config, "YOLO_UNITS_MIN_SCORE", 0.45)),
    }


# ---- 客户端参数持久化 (config/overlay_client.yaml) ----
PREFS_FILENAME = "overlay_client.yaml"


def _prefs_path() -> Path:
    """yaml 路径; 自检/测试用环境变量 JCC_CLIENT_PREFS 指到临时文件."""
    env = os.environ.get("JCC_CLIENT_PREFS")
    if env:
        return Path(env)
    return Path(default_config.PROJECT_ROOT) / "config" / PREFS_FILENAME


def _prefs_show() -> str:
    """给日志用的相对路径 (绝对路径 430px 宽会折三行)."""
    p = _prefs_path()
    try:
        return str(p.relative_to(Path(default_config.PROJECT_ROOT)))
    except ValueError:
        return str(p)


def _yaml():
    """延迟导入 PyYAML; 没装就返回 None (客户端照常可用, 只是不持久化)."""
    try:
        import yaml
        return yaml
    except Exception:                               # noqa: BLE001
        return None


def _code_defaults() -> dict:
    """代码里的出厂默认 (yaml 缺失/字段缺失时的兜底, 值来自 config.py 与 ITEMS)."""
    return {
        "show": {it.key: True for it in ITEMS},
        "intervals": {it.key: it.interval for it in ITEMS},
        "thresholds": default_thresholds(),
        "draw_overlay": True,     # 总开关: 是否往游戏窗口上贴透明层
        "preview": True,          # 结构化文本面板默认展开
    }


def _clamp_section(sec) -> dict:
    """把 yaml 里的一组值钳进合法区间 (手改 yaml 可能写出越界值/错类型)."""
    out = _code_defaults()
    if not isinstance(sec, dict):
        return out
    s = sec.get("show")
    if isinstance(s, dict):
        for it in ITEMS:
            if it.key in s:
                out["show"][it.key] = bool(s[it.key])
    iv = sec.get("intervals")
    if isinstance(iv, dict):
        for it in ITEMS:
            try:
                v = float(iv.get(it.key))
            except (TypeError, ValueError):
                continue
            out["intervals"][it.key] = max(it.min_interval, v)
    th = sec.get("thresholds")
    if isinstance(th, dict):
        for key, rng in (("conf", CONF_RANGE), ("min_score", MIN_SCORE_RANGE)):
            try:
                v = float(th.get(key))
            except (TypeError, ValueError):
                continue
            out["thresholds"][key] = min(max(v, rng[0]), rng[1])
    for key in ("draw_overlay", "preview"):
        if isinstance(sec.get(key), bool):
            out[key] = sec[key]
    return out


def load_prefs() -> dict:
    """读 ``config/overlay_client.yaml``: ``{"defaults": {...}, "user": {...}|None}``.

    * ``defaults``: 点「存为默认」写进去的那一组, 也是「恢复默认」读的那一组;
    * ``user``: 每次点「应用」自动记下来的上一组, 启动时优先用它。

    文件不存在 / 被改坏 / 字段缺失 一律回落到代码默认 —— 绝不因为 yaml 写错让客户端起不来。
    """
    raw = {}
    mod = _yaml()
    try:
        p = _prefs_path()
        if mod is not None and p.is_file():
            raw = mod.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:                               # noqa: BLE001
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    defaults = _clamp_section(raw.get("defaults"))
    user = _clamp_section(raw["user"]) if isinstance(raw.get("user"), dict) else None
    win = raw.get("window") if isinstance(raw.get("window"), dict) else None
    return {"defaults": defaults, "user": user, "window": win}


def save_prefs(user: dict | None = None, defaults: dict | None = None,
               window: dict | None = None) -> None:
    """写回 yaml.

    传 ``None`` 的段保持文件里原样: ``save_prefs(window={...})`` 只更新窗口位置,
    不动 ``user`` / ``defaults`` (关窗口时用)。
    """
    mod = _yaml()
    if mod is None:
        raise RuntimeError("没装 PyYAML (pip install pyyaml), 参数无法持久化")
    p = _prefs_path()
    old = {}
    try:
        if p.is_file():
            old = mod.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:                               # noqa: BLE001
        old = {}
    if not isinstance(old, dict):
        old = {}
    data = {
        "defaults": old.get("defaults") if defaults is None else defaults,
        "user": old.get("user") if user is None else user,
        "window": old.get("window") if window is None else window,
    }
    if not data["defaults"]:
        data["defaults"] = _code_defaults()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(mod.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8", newline="\n")


PHASE_CN = {"prep": "准备", "combat": "战斗", "augment": "海克斯", "draft": "选秀"}


def _phase_cn(phase) -> str:
    s = str(phase or "")
    if not s:
        return "?"
    for k, v in PHASE_CN.items():
        if k in s:
            return v
    return s


def _time_text(g: dict) -> str:
    """时间信息一行: 「3-6 准备 剩余20s」."""
    stage = g.get("stage") or "?-?"
    rt = g.get("round_time")
    rt_txt = f"剩余{rt}s" if rt is not None else "剩余?s"
    return f"{stage} {_phase_cn(g.get('phase'))} {rt_txt}"


def _econ_text(g: dict) -> str:
    """经济信息一行: 「Lv5 经验10/18 金币103 免费刷新10 买经验4」."""
    xp = g.get("xp")
    xp_txt = (f"{xp[0]}/{xp[1]}" if isinstance(xp, (list, tuple)) and len(xp) >= 2
              else "?/?")
    lv = g.get("level")
    gold = g.get("gold")
    parts = [f"Lv{lv if lv is not None else '?'}", f"经验{xp_txt}",
             f"金币{gold if gold is not None else '?'}"]
    if g.get("free_refresh") is not None:
        parts.append(f"免费刷新{g['free_refresh']}")
    if g.get("xp_buy_cost") is not None:
        parts.append(f"买经验{g['xp_buy_cost']}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 设置 (UI 线程写, 绘制线程读)
# --------------------------------------------------------------------------- #
class Settings:
    """当前生效的显示开关 + 间隔; 带版本号, 绘制线程据此判断"要不要强制重算一轮"."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        prefs = load_prefs()
        active = prefs["user"] or prefs["defaults"]
        self.show = {it.show_key: bool(active["show"].get(it.key, True))
                     for it in ITEMS}
        self.intervals = {it.key: float(active["intervals"].get(it.key, it.interval))
                          for it in ITEMS}
        self._thr = dict(active["thresholds"])
        self.draw_overlay = bool(active.get("draw_overlay", True))
        self.preview = bool(active.get("preview", True))
        self.version = 0

    def snapshot(self) -> tuple[dict, dict, int]:
        with self._lock:
            return dict(self.show), dict(self.intervals), self.version

    def thresholds(self) -> dict:
        with self._lock:
            return dict(self._thr)

    def preview_on(self) -> bool:
        with self._lock:
            return bool(self.preview)

    def draw_overlay_on(self) -> bool:
        with self._lock:
            return bool(self.draw_overlay)

    def apply(self, show: dict, intervals: dict,
              thresholds: dict | None = None, preview: bool | None = None,
              draw_overlay: bool | None = None) -> int:
        with self._lock:
            self.show = dict(show)
            self.intervals = dict(intervals)
            if thresholds:
                self._thr = {**self._thr, **dict(thresholds)}
            if preview is not None:
                self.preview = bool(preview)
            if draw_overlay is not None:
                self.draw_overlay = bool(draw_overlay)
            self.version += 1
            return self.version

    def plan(self) -> dict[str, float]:
        """这一版配置下, 该跑哪些识别项 / 各自的间隔 (秒).

        多个显示项共用一个识别项时取**最小**间隔 (谁要得快就按谁跑); 全部关掉则不跑。
        """
        show, intervals, _ver = self.snapshot()
        plan: dict[str, float] = {}
        for it in ITEMS:
            if not show.get(it.show_key):
                continue
            iv = float(intervals.get(it.key, it.interval))
            old = plan.get(it.recog)
            plan[it.recog] = iv if old is None else min(old, iv)
        # 星级来自血条: 只要画棋子, 血条就得跟着同一节拍算 (不画血条框, 只取星数)
        if "units" in plan:
            plan["bars"] = plan["units"]
        # 结构化文本不再按节拍抽 (用户 2026-11-21: 别实时变动) —— 只在点「抽取」时算一次
        return plan


# --------------------------------------------------------------------------- #
# 绘制线程
# --------------------------------------------------------------------------- #
class Worker(threading.Thread):
    """后台循环: 主线程抓帧 + 重画; 每个识别项一条线程, 各按自己的间隔跑."""

    def __init__(self, settings: Settings, frame_source, *, use_overlay: bool = True) -> None:
        super().__init__(name="overlay-client", daemon=True)
        self.settings = settings
        self.frame_source = frame_source
        self.use_overlay = use_overlay
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._status = {
            "frame": 0,
            "cost": 0.0,
            "fresh": [],
            "counts": {},
            "errors": {},
            "in_game": True,
            "shown": False,
            "last_error": "",
        }
        self._state: dict = {
            "units": None, "bars": None, "shop": None,
            "equip_bar": None, "board_equips": None, "phase": None,
            "game": None, "prompt": None, "prompt_ver": 0,
            "ts": {},
        }
        # 离线预览用: 最近一帧原图 + 最近一帧渲染层
        self._layer: Image.Image | None = None
        self._frame_img: Image.Image | None = None
        self._layer_no = 0
        # 多线程共享: 最新一帧 / 各识别项自己的节拍线程
        self._latest: Image.Image | None = None
        self._latest_lock = threading.Lock()
        self._items: dict[str, threading.Thread] = {}
        self._counts: dict[str, int] = {}
        self._fresh: list[str] = []
        self._in_game_ok = True

    def set_prompt(self, text: str) -> int:
        """手动抽取的结果塞进来 (面板按版本号感知变化)."""
        with self._lock:
            self._state["prompt"] = text
            self._state["prompt_ver"] = int(self._state.get("prompt_ver") or 0) + 1
            return int(self._state["prompt_ver"])

    def last_frame(self) -> tuple[Image.Image | None, int]:
        """最近一帧原始画面 (未经绘制) + 帧号; 「保存帧」按钮用."""
        with self._lock:
            return (self._frame_img.copy() if self._frame_img is not None else None,
                    int(self._status.get("frame") or 0))

    def layer(self) -> tuple[Image.Image | None, Image.Image | None, int]:
        with self._lock:
            return self._layer, self._frame_img, self._layer_no

    def _set_layer(self, layer: Image.Image | None, frame: Image.Image | None) -> None:
        with self._lock:
            self._layer = layer
            self._frame_img = frame
            self._layer_no += 1

    # ---- 状态 (UI 轮询读) ----
    def status(self) -> dict:
        with self._lock:
            d = dict(self._status)
            d["counts"] = dict(self._counts)
            return d

    def _set_status(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    def _take_fresh(self) -> list[str]:
        with self._lock:
            fresh = list(self._fresh)
            self._fresh.clear()
            return fresh

    def stop(self, wait: float = 3.0) -> None:
        self._halt.set()
        for th in list(self._items.values()):
            if th.is_alive():
                th.join(timeout=0.5)
        if self.is_alive():
            self.join(timeout=wait)

    # ---- 主循环: 抓帧 + 重画 (识别在各自线程里) ----
    def run(self) -> None:
        for recog in RECOG_ORDER:
            if recog == "prompt":
                continue          # 结构化文本只在点「抽取」时算
            th = threading.Thread(target=self._recog_loop, args=(recog,),
                                  name=f"recog-{recog}", daemon=True)
            th.start()
            self._items[recog] = th

        last_grab = 0.0
        while not self._halt.is_set():
            now = time.monotonic()
            if now - last_grab < GRAB_INTERVAL:
                time.sleep(0.01)
                continue
            last_grab = now

            plan = self.settings.plan()
            img = self._grab()
            if img is None:
                time.sleep(0.5)
                continue
            with self._latest_lock:
                self._latest = img

            t0 = time.monotonic()
            if not plan:
                # 全不勾: 清空 overlay, 别留着上一帧的框
                blank = Image.new("RGBA", img.size, (0, 0, 0, 0))
                if self.use_overlay:
                    debug_overlay.set_frame(blank)
                self._set_layer(blank, img)
                self._set_status(cost=round(time.monotonic() - t0, 3), fresh=[],
                                 in_game=True, shown=False)
                continue

            if not self._in_game(img):
                # 非对局画面: 不跑识别项, 也不留上一帧的假框
                self._in_game_ok = False
                blank = Image.new("RGBA", img.size, (0, 0, 0, 0))
                if self.use_overlay:
                    debug_overlay.set_frame(blank)
                self._set_layer(blank, img)
                self._set_status(cost=round(time.monotonic() - t0, 3), fresh=[],
                                 in_game=False, shown=False)
                continue
            self._in_game_ok = True

            shown = False
            layer = None
            errors: dict = {}
            try:
                layer = self._render(img.size)
            except Exception as exc:  # noqa: BLE001
                errors["draw"] = f"{type(exc).__name__}: {exc}"
            if layer is not None:
                self._set_layer(layer, img)
                if self.use_overlay and self.settings.draw_overlay_on():
                    try:
                        shown = debug_overlay.set_frame(layer)
                    except Exception as exc:  # noqa: BLE001
                        errors["show"] = f"{type(exc).__name__}: {exc}"
            frame_no = int(self.status()["frame"]) + 1
            self._set_status(frame=frame_no, cost=round(time.monotonic() - t0, 3),
                             fresh=self._take_fresh(),
                             errors=errors, in_game=True, shown=shown)

    # ---- 单项节拍线程 ----
    def _recog_loop(self, recog: str) -> None:
        while not self._halt.is_set():
            plan = self.settings.plan()
            iv = plan.get(recog)
            if iv is None or not self._in_game_ok:
                time.sleep(0.1)
                continue
            with self._latest_lock:
                img = self._latest
            if img is None:
                time.sleep(0.05)
                continue
            t0 = time.monotonic()
            try:
                val = self._compute(recog, img)
            except Exception as exc:  # noqa: BLE001 - 单项失败沿用上一帧缓存
                with self._lock:
                    self._status["errors"] = {
                        **(self._status.get("errors") or {}),
                        recog: f"{type(exc).__name__}: {exc}",
                    }
                time.sleep(max(0.05, float(iv)))
                continue
            now = time.monotonic()
            with self._lock:
                self._state[recog] = val
                self._state["ts"][recog] = now
                self._counts[recog] = self._counts.get(recog, 0) + 1
                self._fresh.append(recog)
            time.sleep(max(0.005, float(iv) - (now - t0)))

    # ---- 分项识别 ----
    def _run(self, recog: str, img: Image.Image) -> None:
        """同步跑一项并写回 state (selftest / 手动触发用)."""
        val = self._compute(recog, img)
        with self._lock:
            self._state[recog] = val
            self._state["ts"][recog] = time.monotonic()

    def _compute(self, recog: str, img: Image.Image):
        if recog == "units":
            from recognize import yolo_units
            thr = self.settings.thresholds()
            return yolo_units.extract_units(
                img, conf=thr["conf"], min_score=thr["min_score"])
        if recog == "bars":
            # match_cells=set(): 跳过全棋子库多尺度头像匹配 (单只 145ms, 9 只 1.3s);
            # 名字靠 YOLO 给, 这里只要血条框/星数/徽记锚点。
            from recognize import board
            return board.extract_units(img, include_legend=False, match_cells=set())
        if recog == "game":
            # 时间信息 + 经济信息来自同一次 OCR (InfoTypes.Game 是扁平 dict)
            from recognize import extract as rex
            from config.config import InfoTypes
            return rex.extract_info(img, [InfoTypes.Game]) or {}
        if recog == "prompt":
            from extract_info import prompt as _prompt
            return _prompt.to_markdown(_prompt.snapshot(img))
        if recog == "shop":
            from recognize import extract as rex
            from config.config import InfoTypes
            return rex.extract_info(img, [InfoTypes.Shop]).get("shop") or []
        if recog == "equip_bar":
            from recognize import equipment
            return equipment.extract_equipment(img) or []
        if recog == "board_equips":
            from recognize import equipment
            # 口径: extract_board_equips 的 units 要 board.extract_units 的结果 (含 bar/rect),
            # 传 YOLO 的 box 会静默返回空列表。
            with self._lock:
                bars = self._state.get("bars")
            return equipment.extract_board_equips(img, units=bars) or []
        raise KeyError(recog)

    # ---- 取帧 / 阶段闸 ----
    def _grab(self) -> Image.Image | None:
        try:
            img = self.frame_source()
        except Exception as exc:  # noqa: BLE001 - 窗口没了/被挡 都走这里
            self._set_status(last_error=f"{type(exc).__name__}: {exc}")
            return None
        if img is None:
            self._set_status(last_error="取帧返回空 (窗口最小化/被遮挡?)")
            return None
        if img.size != TARGET_SIZE:
            img = img.resize(TARGET_SIZE, Image.LANCZOS)
        self._set_status(last_error="")
        return img

    def _in_game(self, img: Image.Image) -> bool:
        st = self._state
        ts = st["ts"]
        now = time.monotonic()
        if st["phase"] is None or now - float(ts.get("phase", -1e9)) >= PHASE_INTERVAL:
            try:
                from recognize import phase
                st["phase"] = phase.detect(img)
            except Exception:  # noqa: BLE001 - 判不出阶段就按"对局"走, 别把画面卡黑
                st["phase"] = {}
            ts["phase"] = now
        ph = (st["phase"] or {}).get("phase")
        return ph is None or str(ph) in IN_GAME_PHASES

    # ---- 画一帧 ----
    def _render(self, size) -> Image.Image:
        with self._lock:
            units = self._state.get("units")
            bars = self._state.get("bars")
            shop = self._state.get("shop")
            equip_bar = self._state.get("equip_bar")
            board_equips = self._state.get("board_equips")
            game = self._state.get("game")
        show, _ivs, _ver = self.settings.snapshot()
        # 只画用户勾的那几类: 其余显式关掉 (config 里的默认值可能是开的, 不能让它漏出来)
        draw_show = {
            "OVERLAY_SHOW_UNITS": bool(show.get("OVERLAY_SHOW_UNITS")),
            "OVERLAY_SHOW_BENCH_UNITS": bool(show.get("OVERLAY_SHOW_BENCH_UNITS")),
            "OVERLAY_SHOW_SHOP": bool(show.get("OVERLAY_SHOW_SHOP")),
            "OVERLAY_SHOW_BOARD_EQUIPS": bool(show.get("OVERLAY_SHOW_BOARD_EQUIPS")),
            "OVERLAY_SHOW_EQUIP_BAR": bool(show.get("OVERLAY_SHOW_EQUIP_BAR")),
            "OVERLAY_SHOW_BLOOD_BAR": False,   # 血条只用来取星数, 不画框 (会压住棋子)
            "OVERLAY_SHOW_BALLS_YOLO": False,
            "OVERLAY_SHOW_BALLS_COLOR": False,
            "OVERLAY_SHOW_HERO": False,
            "OVERLAY_SHOW_STAR_BADGE": True,
        }
        units = units or dict(EMPTY_UNITS)
        if bars:
            units = live_draw.merge_stars({"units": units, "bars": bars})
        return debug_overlay.draw_frame(
            size,
            units=units,
            bars=bars,
            legends=[],
            balls_yolo=[],
            balls_color=[],
            shop=shop or [],
            equip_bar=equip_bar or [],
            board_equips=board_equips or [],
            heroes=[],
            show=draw_show,
            header=self._header(show, game),
        )

    def _header(self, show: dict, game: dict | None = None) -> str | None:
        """overlay 顶栏: 时间信息 / 经济信息 按勾选拼成一行 (都没勾就 None)."""
        g = game if game is not None else (self._state.get("game") or {})
        parts: list[str] = []
        if show.get("OVERLAY_SHOW_GAME_TIME"):
            parts.append(_time_text(g))
        if show.get("OVERLAY_SHOW_GAME_ECON"):
            parts.append(_econ_text(g))
        return "  |  ".join(p for p in parts if p) or None

    # ---- 结构化文本 (UI 轮询读) ----
    def prompt_text(self) -> tuple[str, int]:
        with self._lock:
            st = self._state
            return str(st.get("prompt") or ""), int(st.get("prompt_ver") or 0)


class App(tk.Tk):
    """设置窗口: 勾选框 + 间隔输入 + 「应用」."""

    def __init__(self, settings: Settings, worker: Worker,
                 target: dict | None, *, offline: bool = False,
                 args=None) -> None:
        super().__init__()
        self.settings = settings
        self.worker = worker
        self.offline = offline
        self.args = args
        self._applied_at = "-"
        self._closed = False
        self._pv_no = -1          # 上次刷进面板的 prompt 版本号
        self._extracting = False  # 「抽取」正在跑 (防重复点)
        self._pending_extract = None   # 后台线程算完的结果, 由 _poll 取走

        self.title("对局 Overlay 客户端")
        self.resizable(False, False)
        self.configure(background=BG)
        self._apply_theme()
        self._dark_titlebar()

        self.vars: dict[str, tk.BooleanVar] = {}
        self.iv_vars: dict[str, tk.StringVar] = {}
        thr = self.settings.thresholds()
        self.thr_vars: dict[str, tk.StringVar] = {
            "conf": tk.StringVar(value=f"{thr['conf']:.2f}"),
            "min_score": tk.StringVar(value=f"{thr['min_score']:.2f}"),
        }
        self.pv_var = tk.BooleanVar(value=self.settings.preview_on())
        self.draw_var = tk.BooleanVar(value=self.settings.draw_overlay_on())

        self._build(target)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(UI_POLL_MS, self._poll)

    # ---- 主题 ----
    def _apply_theme(self) -> None:
        """深色主题.

        Tk 的 `vista` 主题不允许改控件底色 (会被系统主题覆盖), 所以换成 `clam`
        再逐个 style 配颜色 —— clam 是唯一"全部颜色都能改"的内置主题。
        """
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001 - 主题不可用就用默认
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=BG_BOX,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        troughcolor=BG_BOX, focuscolor=ACCENT)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Dim.TLabel", background=BG, foreground=FG_DIM)
        style.configure("Title.TLabel", background=BG, foreground=FG_TITLE,
                        font=FONT_TITLE)
        style.configure("Section.TLabel", background=BG, foreground=FG_DIM,
                        font=FONT_SMALL)
        style.configure("TCheckbutton", background=BG, foreground=FG, focuscolor=BG)
        style.map("TCheckbutton",
                  background=[("active", BG)],
                  foreground=[("disabled", FG_DIM)])
        style.configure("TEntry", fieldbackground=BG_BOX, foreground=FG,
                        insertcolor=FG, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER)
        style.map("TEntry",
                  fieldbackground=[("disabled", BG_BOX)],
                  foreground=[("disabled", FG_DIM)])
        style.configure("TButton", background=BG_BTN, foreground=FG,
                        bordercolor=BORDER, lightcolor=BG_BTN, darkcolor=BG_BTN,
                        focuscolor=BG_BTN, padding=(10, 4))
        style.map("TButton",
                  background=[("active", BG_BTN_HOVER), ("pressed", BG_BTN_HOVER)],
                  foreground=[("disabled", FG_DIM)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                        padding=(14, 4))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_HOVER), ("pressed", ACCENT_HOVER)],
                  foreground=[("disabled", FG_DIM)])
        style.configure("TSeparator", background=BORDER)
        style.configure("Vertical.TScrollbar", background=BG_BTN, troughcolor=BG_BOX,
                        bordercolor=BORDER, arrowcolor=FG_DIM)

    def _dark_titlebar(self) -> None:
        """把 Windows 标题栏也变深 (Win10 1809+ / Win11 的 DWM 属性 20).

        不做这一步, 深色内容配一条白色标题栏会很突兀。失败无所谓 (老系统没有这属性)。
        """
        try:
            import ctypes

            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            # 让窗口在任务栏出得来 (Toplevel 默认没有), 顺手设扩展样式
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_APPWINDOW)
            value = ctypes.c_int(1)
            for attr in (20, 19):        # 20=Win11/新版, 19=Win10 1903 的旧编号
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:  # noqa: BLE001 - 老系统/无 DWM 就算了
            pass

    # ---- 布局 ----
    def _build(self, target: dict | None) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="对局 Overlay 客户端", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Button(head, text="保存帧", command=self.on_save_frame).grid(
            row=0, column=1, rowspan=2, sticky="ne", padx=(12, 0))
        ttk.Button(head, text="重启", command=self.on_restart).grid(
            row=0, column=2, rowspan=2, sticky="ne", padx=(6, 0))

        if self.offline:
            win_txt = "离线模式: 用测试图跑, 不连模拟器"
        elif target:
            win_txt = (f"目标窗口: hwnd={target.get('render') or target.get('main')}  "
                       f"{target.get('w')}x{target.get('h')}")
        else:
            win_txt = "目标窗口: 未找到雷电模拟器 (可用 --img 离线看效果)"
        ttk.Label(head, text=win_txt, style="Dim.TLabel", font=FONT_SMALL).grid(
            row=1, column=0, sticky="w", pady=(2, 0))

        self._sep(outer, row=1, pady=(8, 8))

        root = ttk.Frame(outer)
        root.grid(row=2, column=0, sticky="nw")

        # 右列: 结构化文本模块 (左列放不下了, 用户 2026-11-21)
        side = ttk.Frame(outer)
        side.grid(row=0, column=1, rowspan=3, sticky="nw", padx=(18, 0))

        # 总开关: 关掉只跑识别, 不往游戏窗口上贴透明层
        r = 0
        ttk.Checkbutton(root, text="绘制 overlay（关掉只跑识别，不往游戏窗口贴图）",
                        variable=self.draw_var).grid(
            row=r, column=0, columnspan=4, sticky="w", pady=(0, 8))
        r += 1

        # 表头 (只有最上面一次, 两个分组共用)
        ttk.Label(root, text="显示项", font=FONT).grid(row=r, column=0, sticky="w")
        ttk.Label(root, text="刷新间隔 (秒)", font=FONT).grid(
            row=r, column=1, columnspan=2, sticky="w")
        ttk.Label(root, text="说明", style="Dim.TLabel", font=FONT_SMALL).grid(
            row=r, column=3, sticky="w")
        r += 1

        r = self._group(root, r, "图色 / YOLO", "visual")
        r = self._group(root, r, "OCR 信息 (同一次顶栏 OCR, 分两段画到 overlay 顶栏)", "ocr")
        r = self._threshold_rows(root, r)

        self.status_text = self._text_box(
            root, row=r, height=STATUS_BOX_H, color=FG_DIM, text="就绪")
        r += 1

        # 日志区: 标题 + 右上角两个按钮 + 只追加的文本框
        logbar = ttk.Frame(root)
        logbar.grid(row=r, column=0, columnspan=4, sticky="ew", pady=(2, 2))
        logbar.columnconfigure(0, weight=1)
        ttk.Label(logbar, text="日志", style="Dim.TLabel", font=FONT_SMALL).grid(
            row=0, column=0, sticky="w")
        ttk.Button(logbar, text="复制日志", width=8,
                   command=self.on_copy_log).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(logbar, text="清空日志", width=8,
                   command=self.on_clear_log).grid(row=0, column=2)
        r += 1

        self.log_text = self._text_box(
            root, row=r, height=LOG_BOX_H, color=FG, readonly=False)
        r += 1
        self._log("改完点右下角「应用」即时生效 (下一个节拍 <=0.5s)", FG_DIM)

        btns = ttk.Frame(root)
        btns.grid(row=r, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        btns.columnconfigure(0, weight=1)
        par = ttk.Frame(btns)
        par.grid(row=0, column=0, sticky="w")
        ttk.Button(par, text="恢复默认", width=9,
                   command=self.on_restore_defaults).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(par, text="存为默认", width=9,
                   command=self.on_save_defaults).grid(row=0, column=1)
        ttk.Button(btns, text="退出", command=self.on_close).grid(
            row=0, column=1, padx=(0, 8))
        ttk.Button(btns, text="应用", style="Accent.TButton",
                   command=self._apply).grid(row=0, column=2)
        r += 1

        self._sep(root, row=r, pady=(12, 8))
        r += 1

        # 结构化文本模块 (右列): 勾选框 -> 图片路径/抽取 -> 面板(标题行 + 文本框)
        self.pv_check = ttk.Checkbutton(
            side, text="结构化文本预览 (与 frame_prompt 同源)",
            variable=self.pv_var, command=self._toggle_preview)
        self.pv_check.grid(row=0, column=0, sticky="w", pady=(0, 4))

        prow = ttk.Frame(side)
        prow.grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Label(prow, text="图片路径:", font=FONT).grid(row=0, column=0, sticky="w")
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(prow, textvariable=self.path_var, width=30)
        self.path_entry.grid(row=0, column=1, sticky="w", padx=(6, 6))
        self.extract_btn = ttk.Button(prow, text="抽取", width=6,
                                      command=self.on_extract)
        self.extract_btn.grid(row=0, column=2, sticky="w")

        self.pv_frame = ttk.Frame(side, width=PROMPT_W, height=PROMPT_H)
        self.pv_frame.grid(row=2, column=0, sticky="nw")
        self.pv_frame.grid_propagate(False)

        pvhead = ttk.Frame(self.pv_frame)
        pvhead.place(x=0, y=0, width=PROMPT_W, height=24)
        pvhead.columnconfigure(0, weight=1)
        ttk.Label(pvhead, text="结构化文本", style="Dim.TLabel",
                  font=FONT_SMALL).grid(row=0, column=0, sticky="w")
        ttk.Button(pvhead, text="复制文本", width=8,
                   command=self.on_copy_prompt).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(pvhead, text="清空文本", width=8,
                   command=self.on_clear_prompt).grid(row=0, column=2)

        inner = ttk.Frame(self.pv_frame)
        inner.place(x=0, y=26, width=PROMPT_W, height=PROMPT_H - 26)
        self.pv_text = tk.Text(inner, font=FONT_MONO, background=BG_BOX,
                               foreground=FG, insertbackground=FG,
                               selectbackground=ACCENT, relief="flat", wrap="none",
                               borderwidth=0, highlightthickness=0, padx=8, pady=6)
        self.pv_text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.pv_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.pv_text.configure(yscrollcommand=sb.set, state="disabled")
        inner.grid_rowconfigure(0, weight=1)
        inner.grid_columnconfigure(0, weight=1)
        if not self.settings.preview_on():
            self.pv_frame.grid_remove()

        # 预览面板建好就 grid 过一次 (又立刻 grid_remove), 列宽被撑到 PROMPT_W;
        # 收起态必须重算一次几何, 不然窗口一直留着 700px 的宽度。
        self.update_idletasks()
        self.geometry(f"{self.winfo_reqwidth()}x{self.winfo_reqheight()}")
        self._place_window()
        self.bind("<Return>", lambda _e: self._apply())

    def _sep(self, parent, *, row: int, pady: tuple[int, int]) -> None:
        """1px 分隔线.

        用 tk.Frame 而不是 ttk.Separator: clam 主题下 Separator 的底色由 element
        内部画 (lightcolor/darkcolor), style.configure 改不动, 永远是浅色亮线。
        """
        tk.Frame(parent, height=1, background="#2f2f2f", borderwidth=0).grid(
            row=row, column=0, columnspan=4, sticky="ew", pady=pady)

    def _group(self, root, row: int, title: str, group: str) -> int:
        """画一个分组标题 + 该组的所有显示项, 末尾补一条分隔线, 返回下一行行号."""
        ttk.Label(root, text=title, style="Section.TLabel").grid(
            row=row, column=0, columnspan=4, sticky="w", pady=(4, 2))
        row += 1
        for it in ITEMS:
            if it.group != group:
                continue
            self._item_row(root, row, it)
            row += 1
        self._sep(root, row=row, pady=(6, 6))
        return row + 1

    def _item_row(self, root, row: int, it: Item) -> None:
        var = tk.BooleanVar(value=True)
        self.vars[it.key] = var
        cb = ttk.Checkbutton(root, text=it.label, variable=var)
        cb.grid(row=row, column=0, sticky="w", pady=2)
        _tip(cb, it.tip)
        iv = tk.StringVar(value=f"{it.interval:.1f}")
        self.iv_vars[it.key] = iv
        self._num_cell(root, row=row, var=iv, lo=it.min_interval, hi=None, step=0.1)
        ttk.Label(root, text=f"下限 {it.min_interval:g}s", style="Dim.TLabel",
                  font=FONT_SMALL).grid(row=row, column=3, sticky="w", padx=(10, 0))

    def _threshold_rows(self, root, row: int) -> int:
        ttk.Label(root, text="识别阈值", style="Section.TLabel").grid(
            row=row, column=0, columnspan=4, sticky="w", pady=(2, 2))
        row += 1
        rows = (
            ("conf", "YOLO 检出置信度", CONF_RANGE, "低于它的框根本不检出"),
            ("min_score", "未知 (unknown) 阈值", MIN_SCORE_RANGE,
             "检出框的分数低于它不写名字, 标 unknown"),
        )
        for key, label, rng, tip in rows:
            lab = ttk.Label(root, text=label, font=FONT)
            lab.grid(row=row, column=0, sticky="w", pady=2)
            _tip(lab, tip)
            self._num_cell(root, row=row, var=self.thr_vars[key], lo=rng[0],
                           hi=rng[1], step=0.05, width=6)
            ttk.Label(root, text=f"{rng[0]:g}~{rng[1]:g}", style="Dim.TLabel",
                      font=FONT_SMALL).grid(row=row, column=3, sticky="w",
                                            padx=(10, 0))
            row += 1
        self._sep(root, row=row, pady=(6, 6))
        return row + 1

    def _num_cell(self, root, *, row: int, var: tk.StringVar, lo: float,
                  hi: float | None = None, step: float = 0.1,
                  width: int = 7) -> None:
        """数值框 + `-`/`+` 两个按钮 (步长 step, 钳在 lo~hi)."""
        box = ttk.Frame(root)
        box.grid(row=row, column=1, sticky="w", padx=(24, 4))
        ttk.Entry(box, textvariable=var, width=width, justify="right").grid(
            row=0, column=0)
        ttk.Button(box, text="-", width=2, takefocus=False,
                   command=lambda: self._bump(var, -step, lo, hi)).grid(
            row=0, column=1, padx=(4, 0))
        ttk.Button(box, text="+", width=2, takefocus=False,
                   command=lambda: self._bump(var, step, lo, hi)).grid(
            row=0, column=2, padx=(2, 0))

    @staticmethod
    def _bump(var: tk.StringVar, delta: float, lo: float,
              hi: float | None) -> None:
        try:
            v = float((var.get() or "").strip())
        except ValueError:
            v = lo
        v = round(v + delta, 2)
        if hi is not None:
            v = min(v, hi)
        v = max(v, lo)
        var.set(f"{v:g}")

    def _toggle_preview(self) -> None:
        """勾选只控面板显隐; 真正去抽结构化文本要等「应用」(避免勾一下就卡 2s)."""
        if self.pv_var.get():
            self.pv_frame.grid()
        else:
            self.pv_frame.grid_remove()
        # resizable(False, False) 会把 geometry 钉死, 内容变宽/变高必须手动重算。
        # 光调 geometry("") 不一定跟着内容走 (实测展开面板后窗口没变宽), 直接按
        # 请求尺寸写一遍最稳。
        self.update_idletasks()
        self.geometry(f"{self.winfo_reqwidth()}x{self.winfo_reqheight()}")

    def _text_box(self, parent, *, row: int, height: int, color: str = FG,
                  text: str = "", readonly: bool = True) -> tk.Text:
        """固定像素大小的只读文本框.

        状态行/提示行的文字每 200ms 变一次、长度也不固定, 用会自己换行的 Label
        会让整个窗口高度跟着跳 (用户 2026-11-21 抱怨的就是这个), 所以固定成
        TEXT_BOX_W x height 的像素盒, 装不下的直接裁掉。
        """
        box = ttk.Frame(parent, width=TEXT_BOX_W, height=height)
        box.grid(row=row, column=0, columnspan=4, sticky="w", pady=(2, 4))
        box.grid_propagate(False)
        t = tk.Text(box, font=FONT_SMALL, background=BG_BOX, foreground=color,
                    insertbackground=FG, relief="flat", wrap="word",
                    borderwidth=0, highlightthickness=0, padx=6, pady=4)
        t.place(x=0, y=0, width=TEXT_BOX_W, height=height)
        t.insert("1.0", text)
        # readonly=True 是状态/提示框; 日志框传 False, 因为 disabled 的 Text
        # 在 Windows 上没法用鼠标拖选 (用户 2026-11-21 要能选中复制)。
        if readonly:
            t.configure(state="disabled", cursor="arrow")
        else:
            t.configure(cursor="xterm", insertwidth=1, takefocus=True)
        return t

    @staticmethod
    def _set_text(widget: tk.Text, text: str, color: str = FG) -> None:
        widget.configure(state="normal", foreground=color)
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    # ---- 结构化文本 ----
    def on_extract(self) -> None:
        """按「图片路径」抽结构化文本; 路径留空就现截一帧 (存盘) 再抽.

        抽取是全量识别 (OCR + 棋子 + 装备 + 商店…), 要 2~3s, 所以放后台线程,
        算完通过 `Worker.set_prompt` 交给面板, 不卡界面。
        """
        if getattr(self, "_extracting", False):
            self._log("[!] 上一次抽取还没跑完, 等一下", FG_DIM)
            return
        self._extracting = True
        self.extract_btn.configure(state="disabled")
        raw = self.path_var.get().strip().strip('"')
        self._log(f"抽取中… ({'现截一帧' if not raw else raw})", FG_DIM)

        def work() -> None:
            saved = None
            try:
                if raw:
                    p = Path(raw)
                    if not p.is_absolute():
                        p = Path(default_config.PROJECT_ROOT) / p
                    if not p.exists():
                        raise FileNotFoundError(f"图片不存在: {p}")
                    img = Image.open(p).convert("RGB")
                else:
                    img, no = self.worker.last_frame()
                    if img is None:
                        raise RuntimeError("还没有帧可截, 等第一帧出来再点")
                    out = Path(default_config.PROJECT_ROOT) / "tempdir" / "frames"
                    out.mkdir(parents=True, exist_ok=True)
                    p = out / f"frame_{no:06d}_{time.strftime('%Y%m%d_%H%M%S')}.png"
                    img.save(p)
                    saved = p
                if img.size != TARGET_SIZE:
                    img = img.resize(TARGET_SIZE, Image.LANCZOS)
                from extract_info import prompt as _prompt
                text = _prompt.to_markdown(_prompt.snapshot(img))
            except Exception as exc:  # noqa: BLE001 - 任何失败都要回到界面
                self._pending_extract = (None, str(exc), None)
                return
            self._pending_extract = (text, None, saved)

        threading.Thread(target=work, name="prompt-extract", daemon=True).start()

    def _extract_done(self, text: str | None, err: str | None, saved) -> None:
        self._extracting = False
        self.extract_btn.configure(state="normal")
        if err is not None:
            self._log(f"[X] 抽取失败: {err}", ERR_RED)
            return
        if not self.pv_var.get():            # 抽出来了就把面板摊开
            self.pv_var.set(True)
            self._toggle_preview()
        ver = self.worker.set_prompt(text or "")
        self._refresh_prompt()
        msg = "结构化文本已更新"
        if saved is not None:
            try:
                msg += f" (帧已存至 {saved.relative_to(Path(default_config.PROJECT_ROOT))})"
            except ValueError:
                msg += f" (帧已存至 {saved})"
        self._log(f"{msg} · v{ver}", OK_GREEN)

    def on_copy_prompt(self) -> None:
        text = self.pv_text.get("1.0", "end").strip()
        if not text:
            self._log("结构化文本是空的, 先点「抽取」", FG_DIM)
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            self._log(f"已复制结构化文本 ({len(text.splitlines())} 行) 到剪贴板", OK_GREEN)
        except tk.TclError as exc:
            self._log(f"[X] 复制失败: {exc}", ERR_RED)

    def on_clear_prompt(self) -> None:
        self.pv_text.configure(state="normal")
        self.pv_text.delete("1.0", "end")
        self.pv_text.configure(state="disabled")
        self._pv_no = -1
        self._log("结构化文本已清空", FG_DIM)

    # ---- 日志 ----
    def _log(self, msg: str, color: str = FG) -> None:
        """往日志框追加一行 (带时分秒), 自动滚到底, 只留最近 LOG_MAX_LINES 行."""
        try:
            tag = f"c{color.strip('#')}"
            self.log_text.tag_configure(tag, foreground=color)
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n", tag)
            total = int(self.log_text.index("end-1c").split(".")[0])
            if total > LOG_MAX_LINES:
                self.log_text.delete("1.0", f"{total - LOG_MAX_LINES}.0")
            self.log_text.see("end")
        except tk.TclError:
            pass

    def on_copy_log(self) -> None:
        """把日志框里的全部文本放进剪贴板."""
        text = self.log_text.get("1.0", "end").strip()
        if not text:
            self._log("日志是空的, 没有可复制的", FG_DIM)
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()       # Windows 上不 update 剪贴板可能丢
            self._log(f"已复制日志 ({len(text.splitlines())} 行) 到剪贴板", OK_GREEN)
        except tk.TclError as exc:
            self._log(f"[X] 复制失败: {exc}", ERR_RED)

    def on_clear_log(self) -> None:
        """清空日志框."""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._log("日志已清空", FG_DIM)

    # ---- 应用 ----
    def _collect(self):
        """解析界面上的 7 个勾选/间隔 + 2 个阈值.

        返回 ``(show, intervals, thresholds, notes)``, ``show`` 以 ``Item.key`` 为键;
        有非法输入时写日志并返回 ``None``。
        """
        show: dict = {}
        intervals: dict = {}
        notes: list[str] = []
        bad: list[str] = []
        for it in ITEMS:
            show[it.key] = bool(self.vars[it.key].get())
            raw = (self.iv_vars[it.key].get() or "").strip()
            try:
                v = float(raw)
            except ValueError:
                bad.append(f"{it.label}: 间隔 '{raw}' 不是数字")
                continue
            if v <= 0:
                bad.append(f"{it.label}: 间隔要大于 0")
                continue
            if v < it.min_interval:
                v = it.min_interval
                self.iv_vars[it.key].set(f"{it.min_interval:g}")
                notes.append(f"{it.label}: 已钳到下限 {it.min_interval:g}s")
            intervals[it.key] = v

        thr: dict = {}
        for key, label, rng in (("conf", "YOLO 检出置信度", CONF_RANGE),
                                ("min_score", "未知阈值", MIN_SCORE_RANGE)):
            raw = (self.thr_vars[key].get() or "").strip()
            try:
                v = float(raw)
            except ValueError:
                bad.append(f"{label}: '{raw}' 不是数字")
                continue
            if v < rng[0]:
                v = rng[0]
                self.thr_vars[key].set(f"{v:.2f}")
                notes.append(f"{label}: 已钳到下限 {rng[0]:g}")
            elif v > rng[1]:
                v = rng[1]
                self.thr_vars[key].set(f"{v:.2f}")
                notes.append(f"{label}: 已钳到上限 {rng[1]:g}")
            thr[key] = v

        if bad:
            self._log("[X] 没有应用: " + "; ".join(bad), ERR_RED)
            return None
        if len(intervals) != len(ITEMS) or len(thr) != 2:
            self._log("[X] 没有应用: 有间隔或阈值没填", ERR_RED)
            return None
        if thr["min_score"] < thr["conf"]:
            notes.append("未知阈值 < 检出置信度: 检出的框都会直接命名")
        extras = {"draw_overlay": bool(self.draw_var.get()),
                  "preview": bool(self.pv_var.get())}
        return show, intervals, thr, notes, extras

    def on_restore_defaults(self) -> None:
        """把 yaml 里 ``defaults:`` 段的参数填回界面 (不自动应用)."""
        d = load_prefs()["defaults"]
        for it in ITEMS:
            self.vars[it.key].set(bool(d["show"].get(it.key, True)))
            self.iv_vars[it.key].set(f"{d['intervals'].get(it.key, it.interval):g}")
        for key in ("conf", "min_score"):
            self.thr_vars[key].set(f"{d['thresholds'].get(key, 0.0):.2f}")
        self.draw_var.set(bool(d.get("draw_overlay", True)))
        self.pv_var.set(bool(d.get("preview", True)))
        self._toggle_preview()
        self._log(f"[OK] 已恢复 {_prefs_show()} 里的默认参数, 点「应用」生效", OK_GREEN)

    def on_save_defaults(self) -> None:
        """把当前界面参数写进 yaml 的 ``defaults:`` 段 (以后「恢复默认」回到这里)."""
        got = self._collect()
        if got is None:
            return
        show, intervals, thr, _notes, ext = got
        group = {"show": show, "intervals": intervals, "thresholds": thr, **ext}
        try:
            save_prefs(group, defaults=group)
        except Exception as e:                      # noqa: BLE001
            self._log(f"[X] 存为默认失败: {e}", ERR_RED)
            return
        self._log(f"[OK] 已存为默认 ({_prefs_show()}), 下次启动与「恢复默认」都用这组",
                  OK_GREEN)

    def _apply(self) -> None:
        got = self._collect()
        if got is None:
            return
        show, intervals, thr, notes, ext = got
        on = [it.label for it in ITEMS if show[it.key]]
        if not on:
            notes.append("全部关闭: overlay 会清空, 但仍保持对局检测")
        if not ext["draw_overlay"]:
            notes.append("绘制 overlay 已关: 只跑识别, 不往游戏上贴图")
        ver = self.settings.apply(
            {it.show_key: show[it.key] for it in ITEMS}, intervals,
            thresholds=thr, preview=ext["preview"],
            draw_overlay=ext["draw_overlay"])
        self._applied_at = time.strftime("%H:%M:%S")
        msg = f"[OK] 已应用 (v{ver} {self._applied_at}): " + \
              (", ".join(on) if on else "无显示项")
        if notes:
            msg += " | " + "; ".join(notes)
        if self.pv_var.get():
            msg += " | 结构化文本改为手动抽取 (点「抽取」)"
        try:
            save_prefs({"show": show, "intervals": intervals, "thresholds": thr,
                        **ext})
            msg += " | 已记住, 下次启动沿用"
        except Exception as e:                      # noqa: BLE001
            msg += f" | 偏好保存失败: {e}"
        self._log(msg, OK_GREEN)

    def _refresh_prompt(self) -> None:
        if self._closed or not self.pv_var.get():
            return
        text, no = self.worker.prompt_text()
        if no == self._pv_no:
            return
        self._pv_no = no
        if not text:
            text = "点下方「抽取」生成结构化文本\n(路径留空 = 现截一帧; 填了路径 = 抽那张图)"
        self.pv_text.configure(state="normal")
        self.pv_text.delete("1.0", "end")
        self.pv_text.insert("1.0", text)
        self.pv_text.configure(state="disabled")

    # ---- 状态轮询 ----
    def _poll(self) -> None:
        if self._closed:
            return
        pend = self._pending_extract
        if pend is not None:
            self._pending_extract = None
            self._extract_done(*pend)
        st = self.worker.status()
        parts = [f"帧 {st['frame']}"]
        if st["frame"]:
            parts.append(f"耗时 {st['cost']:.2f}s")
        if st["fresh"]:
            parts.append("本次重算 " + ",".join(st["fresh"]))
        if not st["in_game"]:
            parts.append("非对局画面, 已暂停识别")
        if self.worker.use_overlay and st["frame"] and not st["shown"]:
            parts.append("贴图失败 (窗口已关?)")
        if st["errors"]:
            parts.append("错误 " + "; ".join(f"{k}={v}" for k, v in st["errors"].items()))
        if st["last_error"]:
            parts.append(st["last_error"])
        if self._applied_at != "-":
            parts.append(f"上次应用 {self._applied_at}")
        self._set_text(self.status_text, " | ".join(parts), FG_DIM)
        self._refresh_prompt()
        self.after(UI_POLL_MS, self._poll)

    # ---- 退出 ----
    def _place_window(self) -> None:
        """按 yaml 里记的位置摆窗口; 记的位置已经不在屏幕内(换显示器/分辨率变了)就居中."""
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x, y = _pick_pos(w, h, load_prefs().get("window"),
                         self.winfo_screenwidth(), self.winfo_screenheight())
        self.geometry(f"{w}x{h}+{x}+{y}")

    def on_save_frame(self) -> None:
        """把最近一帧原始画面存成 png, 路径打到日志框.

        存的是**未绘制**的截图 (游戏原画面), 不是叠了框的那层 —— 拿来调参/对坐标/
        存样本都更合适; 要带框的图用 overlay_info 的离线输出。
        """
        img, no = self.worker.last_frame()
        if img is None:
            self._log("[!] 还没有帧可存, 等第一帧出来再点", ERR_RED)
            return
        out = Path(default_config.PROJECT_ROOT) / "tempdir" / "frames"
        try:
            out.mkdir(parents=True, exist_ok=True)
            name = f"frame_{no:06d}_{time.strftime('%Y%m%d_%H%M%S')}.png"
            path = out / name
            img.save(path)
        except OSError as exc:
            self._log(f"[X] 保存失败: {exc}", ERR_RED)
            print(f"[X] 保存失败: {exc}")
            return
        # 日志框只有 430px 宽, 绝对路径会折三行; 显示相对项目根的路径, 一行放得下
        try:
            shown = path.relative_to(Path(default_config.PROJECT_ROOT))
        except ValueError:
            shown = path
        self._log(f"已将当前帧保存至: {shown}", OK_GREEN)
        print(f"已将当前帧保存至: {path}")

    def on_restart(self) -> None:
        """重启识别: 重新探测窗口 -> 重建 Worker (重新载入模型) -> 计数清零.

        不用关窗口、不用重开进程。注意: 改 `config.py` 里的值**不会**因此生效 ——
        那是 python 进程级的事 (模块常量已经载入), 这里的"重启"只针对识别这一层:
        重新探窗口 (模拟器重启过/换了个模拟器)、重新建 ORT 会话、把帧号和计数清掉。
        """
        print("[i] 重启识别…")
        self._log("正在重启…", FG_DIM)
        self.update_idletasks()

        old = self.worker
        try:
            old.stop()
        except Exception:  # noqa: BLE001 - 线程可能已经停了
            pass
        try:
            debug_overlay.stop()
        except Exception:  # noqa: BLE001
            pass

        args = self.args
        try:
            if args is None:                 # 自检/无参场景: 沿用原来的帧源
                src, offline = old.frame_source, self.offline
            else:
                src, offline = _make_source(args)
        except FileNotFoundError as exc:
            self._log(f"[X] 重启失败: {exc}", ERR_RED)
            return

        use_overlay = False
        if not offline and (args is None or not getattr(args, "no_overlay", False)):
            ld = getattr(args, "ld_index", None) if args is not None else None
            if debug_overlay.resolve_target(ld) is not None:
                use_overlay = debug_overlay.start(ld)

        self.offline = offline
        self.worker = Worker(self.settings, src, use_overlay=use_overlay)
        self.worker.start()
        self._pv_no = -1
        self._set_text(self.status_text, "已重启, 等第一帧…", FG_DIM)

        msg = "[OK] 识别已重启"
        if offline:
            msg += " (离线模式)"
        elif not use_overlay:
            msg += " (没找到模拟器窗口, 只跑识别)"
        self._log(msg, OK_GREEN)
        print(msg)

    def on_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            save_prefs(window={"x": self.winfo_x(), "y": self.winfo_y()})
        except Exception:  # noqa: BLE001
            pass
        try:
            self.worker.stop()
        finally:
            try:
                debug_overlay.stop()
            except Exception:  # noqa: BLE001
                pass
            self.destroy()


def _pick_pos(w: int, h: int, pos, sw: int, sh: int) -> tuple[int, int]:
    """窗口左上角该放哪: 记住的位置还看得见就用它, 否则屏幕居中偏上.

    留 80px 可见边缘 —— 多显示器拔掉后记的坐标可能落在不存在的屏幕上。
    """
    x = (pos or {}).get("x")
    y = (pos or {}).get("y")
    if (isinstance(x, int) and isinstance(y, int)
            and -w + 80 <= x <= sw - 80 and -h + 80 <= y <= sh - 80):
        return x, y
    return (sw - w) // 2, max(0, (sh - h) // 2 - 20)


def _tip(widget, text: str) -> None:
    """给控件挂一个简单的悬停提示 (Tk 没有内建 tooltip)."""
    if not text:
        return
    tip = {"win": None}

    def show(_e=None) -> None:
        if tip["win"] is not None:
            return
        x = widget.winfo_rootx() + 16
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        tk.Label(win, text=text, font=FONT_SMALL, background="#2d2d30",
                 foreground=FG, relief="solid", borderwidth=1, justify="left",
                 wraplength=320).pack(ipadx=4, ipady=2)
        tip["win"] = win

    def hide(_e=None) -> None:
        win = tip["win"]
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
            tip["win"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)


# --------------------------------------------------------------------------- #
# 取帧来源
# --------------------------------------------------------------------------- #
def _make_source(args) -> tuple[object, bool]:
    """返回 (取帧函数, 是否离线)."""
    if args.img:
        p = Path(args.img)
        if not p.is_absolute():
            p = Path(default_config.PROJECT_ROOT) / p
        if not p.exists():
            raise FileNotFoundError(f"图片不存在: {p}")
        img = Image.open(p).convert("RGB")
        if img.size != TARGET_SIZE:
            img = img.resize(TARGET_SIZE, Image.LANCZOS)
        return (lambda: img), True
    from capture import screen as cs
    return cs.capture_game, False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.overlay_client",
        description="Overlay 客户端: 勾选显示项 + 设置刷新间隔, 点「应用」即时生效 (只画不点)")
    p.add_argument("--ld-index", type=int,
                   default=getattr(default_config, "OVERLAY_LD_INDEX", None),
                   help="雷电模拟器序号 (默认取 config.OVERLAY_LD_INDEX)")
    p.add_argument("--img", default=None,
                   help="离线模式: 用这张图循环跑 (不连模拟器, 用于调 UI/看效果)")
    p.add_argument("--no-overlay", action="store_true",
                   help="不建 overlay 窗口 (只显示设置界面与状态)")
    p.add_argument("--seconds", type=float, default=0.0,
                   help="运行多少秒后自动退出 (默认 0 = 一直跑)")
    p.add_argument("--selftest", action="store_true",
                   help="无头自检: 建界面 -> 改几组勾选与间隔 -> 应用 -> 打印结果 -> 退出")
    return p


def run_selftest(args) -> int:
    """自检: 不起 mainloop, 不建 overlay 窗口, 直接验证 Settings/UI 交互."""
    # 自检不能碰仓库里的 config/overlay_client.yaml, 指到一个空的临时文件
    _pref = Path(tempfile.mkdtemp(prefix="jcc_client_selftest_")) / PREFS_FILENAME
    os.environ["JCC_CLIENT_PREFS"] = str(_pref)
    settings = Settings()
    src, offline = _make_source(args)
    worker = Worker(settings, src, use_overlay=False)
    app = App(settings, worker, None, offline=True, args=args)
    app.withdraw()
    fails = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal fails
        print(f"  [{'OK' if ok else 'X'}] {name}{(' ' + extra) if extra else ''}")
        if not ok:
            fails += 1

    def say() -> str:
        return app.log_text.get("1.0", "end").strip()

    print("[1] 默认配置")
    show, ivs, _v = settings.snapshot()
    check("7 项全部默认勾选", all(show.values()), f"{len(show)} 项")
    check("默认间隔", [ivs[it.key] for it in ITEMS] == [it.interval for it in ITEMS],
          str([ivs[it.key] for it in ITEMS]))
    thr0 = settings.thresholds()
    check("默认阈值 conf=0.35 / unknown=0.45",
          (thr0["conf"], thr0["min_score"]) == (0.35, 0.45), str(thr0))
    plan = settings.plan()
    check("全勾时该跑 6 个识别项 (预览关着不跑 prompt)",
          set(plan) == {"units", "bars", "game", "shop", "equip_bar", "board_equips"},
          str(sorted(plan)))

    print("[2] 取消商店 + 只留备战席")
    app.vars["shop"].set(False)
    app.vars["units_board"].set(False)
    app._apply()
    plan = settings.plan()
    check("商店不跑了", "shop" not in plan, str(sorted(plan)))
    check("棋子/血条还在跑 (备战席要用)", "units" in plan, str(sorted(plan)))

    print("[3] 全关")
    for it in ITEMS:
        app.vars[it.key].set(False)
    app._apply()
    check("全关时不跑任何识别项", settings.plan() == {}, str(settings.plan()))

    print("[4] 间隔钳位与非法输入")
    for it in ITEMS:
        app.vars[it.key].set(True)
        app.iv_vars[it.key].set("0.01")
    app._apply()
    _show, ivs, _v = settings.snapshot()
    check("间隔被钳到下限",
          all(ivs[it.key] >= it.min_interval for it in ITEMS),
          str({k: ivs[k] for k in ivs}))
    app.iv_vars["shop"].set("abc")
    app._apply()
    _show2, ivs2, _v2 = settings.snapshot()
    check("非法输入不落库 (版本不变)", ivs2 == ivs, say())

    print("[5] 版本递增")
    app.iv_vars["shop"].set("1.5")
    app._apply()
    check("应用后版本 +1", settings.snapshot()[2] == _v + 1,
          f"v{settings.snapshot()[2]}")

    print("[6] 离线跑几帧")
    app.vars["shop"].set(False)          # OCR 慢, 自检不跑
    app.vars["game_time"].set(False)
    app.vars["game_econ"].set(False)
    for it in ITEMS:
        if it.key not in ("shop", "game_time", "game_econ"):
            app.vars[it.key].set(True)
    app._apply()
    # 预热: 先同步跑一遍把 ONNX/OCR 冷启动吃掉, 再清节拍表看线程自己的节拍
    probe = worker.frame_source()
    for _k in ("units", "bars", "equip_bar", "board_equips"):
        worker._run(_k, probe)
    worker._state["ts"].clear()
    worker.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10.0:
        app.update()
        time.sleep(0.05)
    st = worker.status()
    check("取帧成功", not st["last_error"], st["last_error"])
    check("至少跑了 2 帧", st["frame"] >= 2, f"frame={st['frame']}")
    check("有识别项完成", bool(st["fresh"]), str(st["fresh"]))
    check("各识别项都真跑过",
          all(st["counts"].get(k, 0) >= 1
              for k in ("units", "bars", "equip_bar", "board_equips")),
          str(st["counts"]))
    check("商店没跑 (已取消勾选)", st["counts"].get("shop", 0) == 0, str(st["counts"]))
    check("无识别错误", not st["errors"], str(st["errors"]))
    worker.stop()

    print("[7] 阈值钳位与回填")
    app.iv_vars["shop"].set("1.5")       # 先把上一段故意弄坏的值修好
    app.thr_vars["conf"].set("0.001")
    app._apply()
    t = settings.thresholds()
    check("conf 钳到下限 0.01", t["conf"] == 0.01, str(t))
    check("界面回填钳位后的值", app.thr_vars["conf"].get() == "0.01",
          app.thr_vars["conf"].get())
    app.thr_vars["min_score"].set("3")
    app._apply()
    check("unknown 钳到上限 0.99", settings.thresholds()["min_score"] == 0.99,
          str(settings.thresholds()))
    v_before = settings.snapshot()[2]
    app.thr_vars["conf"].set("abc")
    app._apply()
    check("非法阈值不落库", settings.snapshot()[2] == v_before, say())
    app.thr_vars["conf"].set("0.35")
    app.thr_vars["min_score"].set("0.45")
    app._apply()

    print("[8] 结构化文本: 手动抽取")
    app.pv_var.set(True)
    app._toggle_preview()
    app._apply()
    plan = settings.plan()
    check("prompt 不再进节拍表 (不实时)", "prompt" not in plan, str(sorted(plan)))
    check("面板已展开", bool(app.pv_frame.winfo_manager()),
          app.pv_frame.winfo_manager())
    # 路径空 = 现截一帧 + 存盘 + 抽取 (on_extract 是后台线程, 这里等它落地)
    app.path_var.set("")
    app.on_extract()
    for _ in range(200):
        app.update()
        if not app._extracting:
            break
        time.sleep(0.05)
    txt = app.pv_text.get("1.0", "end").strip()
    check("抽取出的文本是对局快照", "对局快照" in txt, f"{len(txt)} 字")
    check("抽取后不再受 plan 控制", "prompt" not in settings.plan(), str(sorted(settings.plan())))
    log_txt = app.log_text.get("1.0", "end")
    check("日志里记了抽取 + 存帧", "结构化文本已更新" in log_txt and "帧已存至" in log_txt,
          log_txt.strip().splitlines()[-1] if log_txt.strip() else "")
    # 复制 / 清空
    app.on_copy_prompt()
    clip = app.clipboard_get()
    check("复制文本进剪贴板", "对局快照" in clip, f"{len(clip)} 字")
    app.on_clear_prompt()
    check("清空文本后面板为空", app.pv_text.get("1.0", "end").strip() == "",
          repr(app.pv_text.get("1.0", "end").strip()[:20]))
    # 指定图片路径
    app.path_var.set("tests/images/board_ingame.png")
    app.on_extract()
    for _ in range(200):
        app.update()
        if not app._extracting:
            break
        time.sleep(0.05)
    txt2 = app.pv_text.get("1.0", "end").strip()
    check("指定路径也能抽出", "对局快照" in txt2, f"{len(txt2)} 字")
    # 不存在的路径要报错且不崩
    app.path_var.set("tests/images/__nope__.png")
    app.on_extract()
    for _ in range(200):
        app.update()
        if not app._extracting:
            break
        time.sleep(0.05)
    err_log = app.log_text.get("1.0", "end")
    check("坏路径报错但不崩", "抽取失败" in err_log, err_log.strip().splitlines()[-1])
    app.pv_var.set(False)
    app._toggle_preview()
    app._apply()
    check("取消后收起", not app.pv_frame.winfo_manager(), app.pv_frame.winfo_manager())

    print("[9] 加减按钮")
    app.iv_vars["shop"].set("1.5")
    App._bump(app.iv_vars["shop"], 0.1, 1.0, None)
    check("+0.1", app.iv_vars["shop"].get() == "1.6", app.iv_vars["shop"].get())
    App._bump(app.iv_vars["shop"], -0.1, 1.0, None)
    check("-0.1", app.iv_vars["shop"].get() == "1.5", app.iv_vars["shop"].get())
    App._bump(app.iv_vars["shop"], -1.0, 1.0, None)
    check("钳到下限", app.iv_vars["shop"].get() == "1", app.iv_vars["shop"].get())
    app.iv_vars["shop"].set("abc")
    App._bump(app.iv_vars["shop"], 0.1, 1.0, None)
    check("非法值按下限起步", app.iv_vars["shop"].get() == "1.1",
          app.iv_vars["shop"].get())

    print("[10] 重启按钮")
    old_worker = app.worker
    app.log_text.configure(state="normal")
    app.log_text.delete("1.0", "end")
    app.log_text.configure(state="disabled")
    app.on_restart()
    check("重启换了新 worker", app.worker is not old_worker)
    check("重启有提示", "重启" in say(), say())
    for _ in range(120):                 # 等新 worker 跑出第一帧 (最多约 6s)
        app.update()
        if app.worker.status()["frame"] >= 1:
            break
        time.sleep(0.05)
    st_re = app.worker.status()
    check("重启后出了帧", st_re["frame"] >= 1, str(st_re["frame"]))
    check("重启后没有错误", not st_re["errors"], str(st_re["errors"]))
    app.worker.stop()

    print("[11] 保存帧")
    app.log_text.configure(state="normal")
    app.log_text.delete("1.0", "end")
    app.log_text.configure(state="disabled")
    app.on_save_frame()
    txt = say()
    check("日志里有「已将当前帧保存至」", "已将当前帧保存至:" in txt, txt)
    saved = None
    if "保存至:" in txt:
        saved = Path(txt.split("保存至:", 1)[1].strip())
        check("文件真的存在", saved.exists(), str(saved))
        check("是 png", saved.suffix.lower() == ".png", str(saved.suffix))
        if saved.exists():
            from PIL import Image as _Im
            with _Im.open(saved) as im:
                check("尺寸是 1920x1080", im.size == (1920, 1080), str(im.size))
            saved.unlink(missing_ok=True)

    print("[12] 日志区: 复制 / 清空")
    app.on_clear_log()
    txt = app.log_text.get("1.0", "end").strip()
    check("清空后只剩「日志已清空」一行", txt.endswith("日志已清空") and
          len(txt.splitlines()) == 1, txt)
    app._log("测试行A")
    app._log("测试行B")
    app.on_copy_log()
    clip = app.clipboard_get()
    check("剪贴板拿到刚写的行", "测试行A" in clip and "测试行B" in clip, clip[:60])
    check("日志里记了「已复制日志」", "已复制日志" in
          app.log_text.get("1.0", "end"), app.log_text.get("1.0", "end")[-80:])

    print("[13] 参数持久化: 应用即记住 / 存为默认 / 恢复默认")
    app.vars["units_bench"].set(False)
    app.iv_vars["shop"].set("1.2")
    app.thr_vars["conf"].set("0.20")
    app._apply()
    check("应用后 yaml 文件已生成", _pref.is_file(), str(_pref))
    saved = load_prefs()
    check("user 段记下了 conf=0.20",
          abs((saved["user"] or {}).get("thresholds", {}).get("conf", 0) - 0.2) < 1e-9,
          str((saved["user"] or {}).get("thresholds")))
    s3 = Settings()
    check("重新构造 Settings 读回 conf=0.20",
          abs(s3.thresholds()["conf"] - 0.2) < 1e-9, str(s3.thresholds()))
    check("重新构造 Settings 读回 shop=1.2",
          abs(s3.snapshot()[1]["shop"] - 1.2) < 1e-9, str(s3.snapshot()[1]))
    check("重新构造 Settings 读回 units_bench=False",
          s3.snapshot()[0][ITEMS[1].show_key] is False, str(s3.snapshot()[0]))

    app.on_save_defaults()
    check("日志里记了「已存为默认」", "已存为默认" in app.log_text.get("1.0", "end"))
    d = load_prefs()["defaults"]
    check("defaults 段 conf=0.20", abs(d["thresholds"]["conf"] - 0.2) < 1e-9,
          str(d["thresholds"]))

    # 改乱 UI, 再「恢复默认」应回到刚存的那组
    app.vars["units_bench"].set(True)
    app.iv_vars["shop"].set("9.9")
    app.thr_vars["conf"].set("0.90")
    app.on_restore_defaults()
    check("恢复默认把 shop 填回 1.2", app.iv_vars["shop"].get() == "1.2",
          app.iv_vars["shop"].get())
    check("恢复默认把 conf 填回 0.20", app.thr_vars["conf"].get() == "0.20",
          app.thr_vars["conf"].get())
    check("恢复默认把 units_bench 填回未勾选", bool(app.vars["units_bench"].get()) is False)
    check("恢复默认不会自动应用 (日志提示点应用)",
          "点「应用」生效" in app.log_text.get("1.0", "end"))

    print("[14] 绘制总开关 / 预览默认勾选 / 窗口位置")
    # 「默认」要在没有 user 段的干净 prefs 下测 (前面几段改过 pv_var)
    _bak = _pref.read_bytes() if _pref.is_file() else None
    _pref.unlink(missing_ok=True)
    _clean = Settings()
    check("结构化文本预览默认勾选", _clean.preview_on() is True)
    check("绘制 overlay 默认勾选", _clean.draw_overlay_on() is True)
    if _bak is not None:
        _pref.write_bytes(_bak)

    app.pv_var.set(True)
    app.draw_var.set(False)
    app._apply()
    s4 = Settings()
    check("关掉总开关后重新读回 draw_overlay=False",
          s4.draw_overlay_on() is False)
    check("日志提示只跑识别不贴图",
          "只跑识别" in app.log_text.get("1.0", "end"),
          app.log_text.get("1.0", "end")[-120:])
    check("预览开关也进了 user 段",
          load_prefs()["user"].get("preview") is True,
          str((load_prefs()["user"] or {}).get("preview")))

    save_prefs(window={"x": 123, "y": 77})
    check("窗口位置写进 yaml", load_prefs()["window"] == {"x": 123, "y": 77},
          str(load_prefs()["window"]))
    check("记的位置直接采用", _pick_pos(800, 600, {"x": 123, "y": 77}, 1920, 1080)
          == (123, 77), str(_pick_pos(800, 600, {"x": 123, "y": 77}, 1920, 1080)))
    check("越界位置回落到屏幕内",
          _pick_pos(800, 600, {"x": -99999, "y": -99999}, 1920, 1080) == (560, 220),
          str(_pick_pos(800, 600, {"x": -99999, "y": -99999}, 1920, 1080)))
    check("没记过位置也居中", _pick_pos(800, 600, None, 1920, 1080) == (560, 220),
          str(_pick_pos(800, 600, None, 1920, 1080)))

    app.destroy()
    print(f"[{'OK' if fails == 0 else 'X'}] 自检{'通过' if fails == 0 else f'失败 {fails} 项'}")
    return 0 if fails == 0 else 1


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # 高 DPI 下 Tk 会糊, 显式声明 DPI 感知 (失败无所谓)
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass

    if args.selftest:
        return run_selftest(args)

    try:
        src, offline = _make_source(args)
    except FileNotFoundError as exc:
        print(f"[X] {exc}")
        return 2

    settings = Settings()
    use_overlay = not args.no_overlay
    target = None
    if not offline and use_overlay:
        target = debug_overlay.resolve_target(args.ld_index)
        if target is None:
            print("[!] 没找到雷电模拟器窗口: 界面照常可用, 但不会贴到游戏画面上")
            print("[!] 想先看效果可加 --img tests/images/board_ingame.png 走离线模式")
            use_overlay = False
        elif not debug_overlay.start(args.ld_index):
            print("[!] overlay 窗口启动失败, 只显示设置界面")
            use_overlay = False
    elif offline and use_overlay:
        print("[!] 离线模式不建 overlay 窗口 (只跑识别与设置界面)")

    worker = Worker(settings, src, use_overlay=use_overlay and not offline)
    app = App(settings, worker, target, offline=offline, args=args)
    app.after(200, worker.start)
    if args.seconds:
        app.after(int(args.seconds * 1000), app.on_close)
    print("[OK] Overlay 客户端已启动" + (" (离线)" if offline else ""))
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    print("[OK] 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())