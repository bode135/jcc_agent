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
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageTk

from config.config import default_config
from overlay import debug_overlay, live_draw

TARGET_SIZE = (1920, 1080)
EMPTY_UNITS = {"board": [], "bench": []}

# 只在"对局画面"里画: 大厅/登录/选英雄/结算 的界面美术会被当成球和棋子 (实测大厅帧 23 个假球)
IN_GAME_PHASES = {"prep", "combat", "augment", "draft"}
PHASE_INTERVAL = 2.0      # 阶段判定间隔 (phase.detect 25~48ms)
UI_POLL_MS = 200          # 状态栏刷新周期
PREVIEW_W, PREVIEW_H = 640, 360   # 离线预览缩略图 (16:9)

FONT = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 12, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)


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
    """

    key: str
    label: str
    show_key: str
    recog: str
    interval: float
    min_interval: float
    tip: str = ""


ITEMS: tuple[Item, ...] = (
    Item("units_board", "棋子框 (棋盘)", "OVERLAY_SHOW_UNITS", "units",
         0.5, 0.4, "YOLO 棋子 + 名字 + 星级, 以及血条左侧那枚星级徽记锚点"),
    Item("units_bench", "备战席框", "OVERLAY_SHOW_BENCH_UNITS", "units",
         0.5, 0.4, "备战席格子里的棋子 (与棋盘共用一次 YOLO 推理)"),
    Item("shop", "商店 (识别 + 区域框)", "OVERLAY_SHOW_SHOP", "shop",
         2.0, 1.0, "5 张卡的卡面 OCR; 勾上才会去跑 OCR, 不勾可省 ~0.6s/次"),
    Item("board_equips", "棋子身上装备", "OVERLAY_SHOW_BOARD_EQUIPS", "board_equips",
         3.0, 2.5, "血条上方那几枚装备图标 (依赖血条定位, 最慢的一项)"),
    Item("equip_bar", "装备栏装备", "OVERLAY_SHOW_EQUIP_BAR", "equip_bar",
         2.0, 1.5, "左侧库存面板的格子与件数"),
)

# 识别项顺序: 棋子/血条 先跑 (后面的棋子携带装备要拿血条框定位)
RECOG_ORDER = ("units", "bars", "shop", "equip_bar", "board_equips")


# --------------------------------------------------------------------------- #
# 设置 (UI 线程写, 绘制线程读)
# --------------------------------------------------------------------------- #
class Settings:
    """当前生效的显示开关 + 间隔; 带版本号, 绘制线程据此判断"要不要强制重算一轮"."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.show = {it.show_key: True for it in ITEMS}
        self.intervals = {it.key: it.interval for it in ITEMS}
        self.version = 0

    def snapshot(self) -> tuple[dict, dict, int]:
        with self._lock:
            return dict(self.show), dict(self.intervals), self.version

    def apply(self, show: dict, intervals: dict) -> int:
        with self._lock:
            self.show = dict(show)
            self.intervals = dict(intervals)
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
        return plan


# --------------------------------------------------------------------------- #
# 绘制线程
# --------------------------------------------------------------------------- #
class Worker(threading.Thread):
    """后台循环: 按当前配置取帧 -> 跑到点的识别项 -> 画 -> 贴到 overlay 窗口."""

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
            "ts": {},
        }
        # 离线预览用: 最近一帧原图 + 最近一帧渲染层
        self._layer: Image.Image | None = None
        self._frame_img: Image.Image | None = None
        self._layer_no = 0

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
            return dict(self._status)

    def _set_status(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    def stop(self, wait: float = 3.0) -> None:
        self._halt.set()
        if self.is_alive():
            self.join(timeout=wait)

    # ---- 主循环 ----
    def run(self) -> None:
        last_ver = -1
        while not self._halt.is_set():
            plan = self.settings.plan()
            if not plan:
                # 全不勾: 清空 overlay, 别留着上一帧的框
                blank = Image.new("RGBA", TARGET_SIZE, (0, 0, 0, 0))
                if self.use_overlay:
                    debug_overlay.set_frame(blank)
                self._set_layer(blank, self._frame_img)
                time.sleep(0.3)
                continue

            _show, _ivs, ver = self.settings.snapshot()
            force = ver != last_ver
            last_ver = ver
            now = time.monotonic()
            ts = self._state["ts"]
            due = [k for k in RECOG_ORDER
                   if k in plan and (force or now - float(ts.get(k, -1e9)) >= plan[k])]

            t0 = time.monotonic()
            img = self._grab()
            if img is None:
                time.sleep(1.0)
                continue

            if not self._in_game(img):
                # 非对局画面: 不跑识别项, 也不留上一帧的假框
                blank = Image.new("RGBA", img.size, (0, 0, 0, 0))
                if self.use_overlay:
                    debug_overlay.set_frame(blank)
                self._set_layer(blank, img)
                self._set_status(cost=round(time.monotonic() - t0, 3), fresh=[],
                                 in_game=False)
                time.sleep(max(0.2, min(plan.values())))
                continue

            fresh: list[str] = []
            errors: dict = {}
            counts = dict(self.status().get("counts") or {})
            for recog in due:
                try:
                    self._run(recog, img)
                    fresh.append(recog)
                    counts[recog] = counts.get(recog, 0) + 1
                    ts[recog] = now
                except Exception as exc:  # noqa: BLE001 - 单项失败沿用上一帧缓存
                    errors[recog] = f"{type(exc).__name__}: {exc}"

            shown = False
            layer = None
            try:
                layer = self._render(img.size)
            except Exception as exc:  # noqa: BLE001
                errors["draw"] = f"{type(exc).__name__}: {exc}"
            if layer is not None:
                self._set_layer(layer, img)
                if self.use_overlay:
                    try:
                        shown = debug_overlay.set_frame(layer)
                    except Exception as exc:  # noqa: BLE001
                        errors["show"] = f"{type(exc).__name__}: {exc}"

            frame_no = int(self.status()["frame"]) + 1
            self._set_status(frame=frame_no, cost=round(time.monotonic() - t0, 3),
                             fresh=fresh, counts=counts, errors=errors,
                             in_game=True, shown=shown)

            dt = time.monotonic() - t0
            time.sleep(max(0.05, min(plan.values()) - dt))

    # ---- 分项识别 ----
    def _run(self, recog: str, img: Image.Image) -> None:
        st = self._state
        if recog == "units":
            from recognize import yolo_units
            st["units"] = yolo_units.extract_units(img)
        elif recog == "bars":
            # match_cells=set(): 跳过全棋子库多尺度头像匹配 (单只 145ms, 9 只 1.3s);
            # 名字靠 YOLO 给, 这里只要血条框/星数/徽记锚点。
            from recognize import board
            st["bars"] = board.extract_units(img, include_legend=False, match_cells=set())
        elif recog == "shop":
            from recognize import extract as rex
            from config.config import InfoTypes
            st["shop"] = rex.extract_info(img, [InfoTypes.Shop]).get("shop") or []
        elif recog == "equip_bar":
            from recognize import equipment
            st["equip_bar"] = equipment.extract_equipment(img) or []
        elif recog == "board_equips":
            from recognize import equipment
            # 口径: extract_board_equips 的 units 要 board.extract_units 的结果 (含 bar/rect),
            # 传 YOLO 的 box 会静默返回空列表。
            st["board_equips"] = equipment.extract_board_equips(
                img, units=st.get("bars")) or []
        else:
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
        st = self._state
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
        units = st.get("units") or dict(EMPTY_UNITS)
        bars = st.get("bars")
        if bars:
            units = live_draw.merge_stars({"units": units, "bars": bars})
        return debug_overlay.draw_frame(
            size,
            units=units,
            bars=bars,
            legends=[],
            balls_yolo=[],
            balls_color=[],
            shop=st.get("shop") or [],
            equip_bar=st.get("equip_bar") or [],
            board_equips=st.get("board_equips") or [],
            heroes=[],
            show=draw_show,
            header=None,
        )


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    """设置窗口: 勾选框 + 间隔输入 + 「应用」."""

    def __init__(self, settings: Settings, worker: Worker,
                 target: dict | None, *, offline: bool = False) -> None:
        super().__init__()
        self.settings = settings
        self.worker = worker
        self.offline = offline
        self._applied_at = "-"
        self._closed = False

        self.title("对局 Overlay 客户端")
        self.resizable(False, False)
        try:
            ttk.Style(self).theme_use("vista")
        except Exception:  # noqa: BLE001 - 主题不可用就用默认
            pass

        self.vars: dict[str, tk.BooleanVar] = {}
        self.iv_vars: dict[str, tk.StringVar] = {}

        self._build(target)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(UI_POLL_MS, self._poll)

    # ---- 布局 ----
    def _build(self, target: dict | None) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        root = ttk.Frame(outer)
        root.grid(row=0, column=0, sticky="nw")

        ttk.Label(root, text="对局 Overlay 客户端", font=FONT_TITLE).grid(
            row=0, column=0, columnspan=4, sticky="w")

        if self.offline:
            win_txt = "离线模式: 用测试图跑, 不连模拟器"
        elif target:
            win_txt = (f"目标窗口: hwnd={target.get('render') or target.get('main')}  "
                       f"{target.get('w')}x{target.get('h')}")
        else:
            win_txt = "目标窗口: 未找到雷电模拟器 (可用 --img 离线看效果)"
        ttk.Label(root, text=win_txt, font=FONT_SMALL, foreground="#555555").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 10))

        ttk.Separator(root, orient="horizontal").grid(
            row=2, column=0, columnspan=4, sticky="ew", pady=(0, 8))

        ttk.Label(root, text="显示项", font=FONT).grid(row=3, column=0, sticky="w")
        ttk.Label(root, text="刷新间隔 (秒)", font=FONT).grid(row=3, column=1,
                                                        columnspan=2, sticky="w")
        ttk.Label(root, text="下限", font=FONT_SMALL, foreground="#777777").grid(
            row=3, column=3, sticky="w")

        for i, it in enumerate(ITEMS, start=4):
            var = tk.BooleanVar(value=True)
            self.vars[it.key] = var
            cb = ttk.Checkbutton(root, text=it.label, variable=var)
            cb.grid(row=i, column=0, sticky="w", pady=3)
            _tip(cb, it.tip)
            iv = tk.StringVar(value=f"{it.interval:.1f}")
            self.iv_vars[it.key] = iv
            entry = ttk.Entry(root, textvariable=iv, width=7, justify="right")
            entry.grid(row=i, column=1, sticky="w", padx=(24, 4))
            ttk.Label(root, text="秒", font=FONT).grid(row=i, column=2, sticky="w")
            ttk.Label(root, text=f"{it.min_interval:.1f}", font=FONT_SMALL,
                      foreground="#777777").grid(row=i, column=3, sticky="w", padx=(8, 0))

        ttk.Separator(root, orient="horizontal").grid(
            row=9, column=0, columnspan=4, sticky="ew", pady=(10, 8))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(root, textvariable=self.status_var, font=FONT_SMALL,
                  foreground="#333333", wraplength=430, justify="left").grid(
            row=10, column=0, columnspan=4, sticky="w")

        self.say_var = tk.StringVar(value="改完点右下角「应用」即时生效 (下一个节拍 <=0.5s)")
        self.say_label = ttk.Label(root, textvariable=self.say_var, font=FONT_SMALL,
                                   foreground="#1a6b1a", wraplength=430, justify="left")
        self.say_label.grid(row=11, column=0, columnspan=4, sticky="w",
                            pady=(4, 12))

        right = ttk.Frame(root)
        right.grid(row=12, column=0, columnspan=4, sticky="e")
        ttk.Button(right, text="退出", command=self.on_close).grid(row=0, column=0,
                                                                  padx=(0, 8))
        apply_btn = ttk.Button(right, text="应用", command=self._apply)
        apply_btn.grid(row=0, column=1)
        self._apply_btn = apply_btn

        # 离线模式: 右边多一列预览, 把测试图 + 当前勾选的框实时画出来,
        # 这样不连模拟器也能直观看到「应用」到底改了什么。
        self.preview = None
        self._photo = None
        self._pv_no = -1
        if self.offline:
            pv = ttk.Frame(outer)
            pv.grid(row=0, column=1, sticky="nw", padx=(16, 0))
            ttk.Label(pv, text="预览 (测试图 + 当前勾选的框)", font=FONT_SMALL,
                      foreground="#555555").grid(row=0, column=0, sticky="w",
                                                 pady=(0, 6))
            self.preview = ttk.Label(pv, text="等第一帧...", font=FONT_SMALL,
                                     anchor="center", background="#111111",
                                     foreground="#CCCCCC")
            self.preview.grid(row=1, column=0, sticky="nw")

        self.bind("<Return>", lambda _e: self._apply())

    # ---- 预览 ----
    def _update_preview(self) -> None:
        if self.preview is None:
            return
        layer, frame, no = self.worker.layer()
        if frame is None or no == self._pv_no:
            return
        self._pv_no = no
        base = frame.convert("RGBA")
        if layer is not None:
            base = Image.alpha_composite(base, layer)
        thumb = base.convert("RGB").resize((PREVIEW_W, PREVIEW_H), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(thumb)
        self.preview.configure(image=self._photo, text="")

    # ---- 应用 ----
    def _apply(self) -> None:
        show: dict = {}
        intervals: dict = {}
        notes: list[str] = []
        bad: list[str] = []
        for it in ITEMS:
            show[it.show_key] = bool(self.vars[it.key].get())
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
                self.iv_vars[it.key].set(f"{it.min_interval:.1f}")
                notes.append(f"{it.label}: 已钳到下限 {it.min_interval:.1f}s")
            intervals[it.key] = v

        if bad:
            self.say_var.set("[X] 没有应用: " + "; ".join(bad))
            self.say_label.configure(foreground="#b00000")
            return
        if len(intervals) != len(ITEMS):
            self.say_var.set("[X] 没有应用: 有间隔没填")
            self.say_label.configure(foreground="#b00000")
            return

        on = [it.label for it in ITEMS if show[it.show_key]]
        if not on:
            notes.append("全部关闭: overlay 会清空, 但仍保持对局检测")
        ver = self.settings.apply(show, intervals)
        self._applied_at = time.strftime("%H:%M:%S")
        msg = f"[OK] 已应用 (v{ver} {self._applied_at}): " + \
              (", ".join(on) if on else "无显示项")
        if notes:
            msg += " | " + "; ".join(notes)
        self.say_var.set(msg)
        self.say_label.configure(foreground="#1a6b1a")

    # ---- 状态轮询 ----
    def _poll(self) -> None:
        if self._closed:
            return
        st = self.worker.status()
        parts = [f"帧 {st['frame']}"]
        if st["frame"]:
            parts.append(f"耗时 {st['cost']:.2f}s")
        if st["fresh"]:
            parts.append("本次重算 " + ",".join(st["fresh"]))
        if not st["in_game"]:
            parts.append("非对局画面, 已暂停识别")
        if not self.worker.use_overlay:
            parts.append("未连 overlay 窗口")
        elif st["frame"] and not st["shown"]:
            parts.append("贴图失败 (窗口已关?)")
        if st["errors"]:
            parts.append("错误 " + "; ".join(f"{k}={v}" for k, v in st["errors"].items()))
        if st["last_error"]:
            parts.append(st["last_error"])
        if self._applied_at != "-":
            parts.append(f"上次应用 {self._applied_at}")
        self.status_var.set(" | ".join(parts))
        self._update_preview()
        self.after(UI_POLL_MS, self._poll)

    # ---- 退出 ----
    def on_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.worker.stop()
        finally:
            try:
                debug_overlay.stop()
            except Exception:  # noqa: BLE001
                pass
            self.destroy()


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
        tk.Label(win, text=text, font=FONT_SMALL, background="#ffffe0",
                 relief="solid", borderwidth=1, justify="left",
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
    settings = Settings()
    src, offline = _make_source(args)
    worker = Worker(settings, src, use_overlay=False)
    app = App(settings, worker, None, offline=True)
    app.withdraw()
    fails = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal fails
        print(f"  [{'OK' if ok else 'X'}] {name}{(' ' + extra) if extra else ''}")
        if not ok:
            fails += 1

    print("[1] 默认配置")
    show, ivs, _v = settings.snapshot()
    check("5 项全部默认勾选", all(show.values()), f"{len(show)} 项")
    check("默认间隔", [ivs[it.key] for it in ITEMS] == [it.interval for it in ITEMS],
          str([ivs[it.key] for it in ITEMS]))
    plan = settings.plan()
    check("全勾时该跑 5 个识别项", set(plan) == {"units", "bars", "shop",
                                                "equip_bar", "board_equips"},
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
    check("非法输入不落库 (版本不变)", ivs2 == ivs, app.say_var.get())

    print("[5] 版本递增")
    app.iv_vars["shop"].set("1.5")
    app._apply()
    check("应用后版本 +1", settings.snapshot()[2] == _v + 1,
          f"v{settings.snapshot()[2]}")

    print("[6] 离线跑几帧")
    app.vars["shop"].set(False)          # OCR 慢, 自检不跑
    for it in ITEMS:
        if it.key != "shop":
            app.vars[it.key].set(True)
    app._apply()
    worker.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 6.0:
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
    app = App(settings, worker, target, offline=offline)
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