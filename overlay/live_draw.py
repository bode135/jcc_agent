"""绘制节拍 / 取帧 / 分项汇总 (纯图色方案: 只靠截图 + 识别, 不读游戏内存).

分工:
    - `overlay/debug_overlay.py` 只管**窗口 + 贴图**, `draw_frame()` 是纯函数;
    - 本模块管:**这一拍该重算什么**(按 config.OVERLAY_* 的分项间隔) / 跑识别 / 汇总成画框数据 /
      取帧来源注入 / 可选的后台绘制线程。

要点:
    - 纯图色: 原先"内存对账 GT / 内存实时格位 / 选秀轮盘"这三条路径**已全部删除**
      (连同它们的 provider 注册接口), 本模块现在不再有任何内存读取或选秀依赖。
    - 取帧来源可注入: 实时默认 `capture.screen.capture_game()`, 离线/单测用 `set_frame_source(fn)`
      注入一个"返回 PIL Image 的函数", 这样同一套绘制/汇总代码离线也能验证。
    - 分项间隔: 单项实测耗时差异很大 (YOLO 棋子 ~10ms / 血条 ~60ms / 商店 ~230ms /
      装备栏 ~450ms / 棋子携带装备 ~800ms), 所以只让**到点**的项重算, 其余用上一帧缓存。
"""

from __future__ import annotations

import contextlib
import threading
import time

from PIL import Image

from config.config import InfoTypes, default_config
from overlay import debug_overlay

_MIN_TICK = 0.2  # 绘制/刷新节拍下限 (秒): 再快也没意义, 只会白烧 CPU

# 画框类项目 -> (config 里的开关键, config 里的间隔键)
_ITEMS = {
    "units":        ("OVERLAY_SHOW_UNITS",        "OVERLAY_UNITS_INTERVAL"),
    "bars":         ("OVERLAY_SHOW_BLOOD_BAR",    "OVERLAY_BLOOD_BAR_INTERVAL"),
    "balls_yolo":   ("OVERLAY_SHOW_BALLS_YOLO",   "OVERLAY_BALLS_YOLO_INTERVAL"),
    "balls_color":  ("OVERLAY_SHOW_BALLS_COLOR",  "OVERLAY_BALLS_COLOR_INTERVAL"),
    "shop":         ("OVERLAY_SHOW_SHOP",         "OVERLAY_SHOP_INTERVAL"),
    "equip_bar":    ("OVERLAY_SHOW_EQUIP_BAR",    "OVERLAY_EQUIP_BAR_INTERVAL"),
    "board_equips": ("OVERLAY_SHOW_BOARD_EQUIPS", "OVERLAY_BOARD_EQUIPS_INTERVAL"),
}
_ITEM_KEYS = tuple(_ITEMS)

# 单项"最短间隔"地板: 按实测耗时定的, 防止把间隔配得比识别本身还短 (白跑、抢帧)
_MIN_ITEM_INTERVAL = {
    "units": 0.4,
    "bars": 1.5,
    "balls_yolo": 0.4,
    "balls_color": 0.4,
    "shop": 1.0,
    "equip_bar": 1.5,
    "board_equips": 2.5,
}

_EMPTY_UNITS = {"board": [], "bench": []}

_frame_source = None          # 取帧来源 (None = 实时 capture.screen.capture_game)
_main_lock = threading.RLock()  # 主循环忙标记 (绘制线程礼让, 别跟决策循环抢 GIL)
_main_busy = [0]


# --------------------------------------------------------------------------- #
# 主循环礼让 (绘制/后台线程在决策循环活跃时少占 CPU)
# --------------------------------------------------------------------------- #
def begin_main_work() -> None:
    with _main_lock:
        _main_busy[0] += 1


def end_main_work() -> None:
    with _main_lock:
        _main_busy[0] = max(0, _main_busy[0] - 1)


@contextlib.contextmanager
def main_work():
    begin_main_work()
    try:
        yield
    finally:
        end_main_work()


def _is_main_busy() -> bool:
    with _main_lock:
        return _main_busy[0] > 0


# --------------------------------------------------------------------------- #
# 开关 / 间隔 / 到点判定
# --------------------------------------------------------------------------- #
def _as_bool(v) -> bool:
    return bool(v)


def item_enabled(key: str, show: dict | None = None) -> bool:
    """该项是否要算/要画 (show 覆盖优先, 否则读 config.OVERLAY_SHOW_*)."""
    show_key = _ITEMS[key][0]
    if show is not None and show_key in show:
        return _as_bool(show[show_key])
    return _as_bool(getattr(default_config, show_key, False))


def item_interval(key: str, show: dict | None = None) -> float:
    """该项刷新间隔 (秒), 带实测耗时地板."""
    raw = float(getattr(default_config, _ITEMS[key][1], 1.0))
    return max(_MIN_TICK, _MIN_ITEM_INTERVAL.get(key, _MIN_TICK), raw)


def loop_tick(show: dict | None = None) -> float:
    """当前启用项里最小的刷新间隔 (>= _MIN_TICK); 没启用任何项时返回 0."""
    ivs = [item_interval(k, show) for k in _ITEM_KEYS if item_enabled(k, show)]
    return max(_MIN_TICK, min(ivs)) if ivs else 0.0


def due_keys(last_ts: dict, now: float | None = None, show: dict | None = None) -> set[str]:
    """这一拍该重算的项 (last_ts: {item: 上次重算的时刻}), 首次全部到点."""
    now = time.monotonic() if now is None else now
    out = set()
    for k in _ITEM_KEYS:
        if not item_enabled(k, show):
            continue
        if now - float(last_ts.get(k, -1e9)) >= item_interval(k, show):
            out.add(k)
    return out


def _try(fn, default, what: str, errors: dict | None = None):
    """跑一个识别项; 出错时不抛 (记 errors[what] 并返回 default)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - 单项识别失败不该让整帧/整进程挂掉
        if errors is not None:
            errors[what] = f"{type(exc).__name__}: {exc}"
        return default


# --------------------------------------------------------------------------- #
# 取帧来源 (实时 capture_game / 离线注入)
# --------------------------------------------------------------------------- #
def set_frame_source(fn) -> None:
    """注入取帧函数 (无参, 返回 PIL Image 或 None); 传 None 恢复实时截图."""
    global _frame_source
    _frame_source = fn


def realtime_frame():
    """实时取帧: `capture.screen.capture_game()` (1920×1080); 失败抛 RuntimeError(中文)."""
    from capture import screen as cs
    return cs.capture_game()


def get_frame():
    """当前取帧来源的取帧函数."""
    return _frame_source if _frame_source is not None else realtime_frame


# --------------------------------------------------------------------------- #
# 分项识别汇总
# --------------------------------------------------------------------------- #
def collect(img: Image.Image, *, cache: dict | None = None, due: set[str] | None = None,
            force: bool = False, show: dict | None = None) -> dict:
    """按 due 跑识别项, 结果写进 cache, 返回画框/消息要用的汇总 dict.

    Args:
        img: 当前帧 (识别口径要求 1920×1080, 非该尺寸请调用方先归一化)。
        cache: 跨帧缓存 (首次传 {}), 内部用 `_ts` 记录各项上次重算时刻。
        due: 这一拍要重算的项 (见 `due_keys`); None/force=True 时全部重算。
        show: 开关覆盖 dict (同 config.OVERLAY_SHOW_* 键名); None 读 config。

    Returns:
        {"units","bars","balls_yolo","legends","balls_color","shop","equip_bar",
         "board_equips","errors","fresh"}
    """
    from recognize import board, equipment, extract as rex, reward_ball, yolo_units

    store = cache if cache is not None else {}
    ts = store.setdefault("_ts", {})
    errors = store.setdefault("_errors", {})
    force = force or due is None
    due_set = set(_ITEM_KEYS) if force else set(due)
    now = time.monotonic()

    def want(k: str) -> bool:
        return force or k in due_set

    # 棋子携带装备要按血条/身体框定位图标; 血条缓存没有时顺带先算一次
    if want("board_equips") and store.get("bars") is None and want("bars") is False:
        due_set.add("bars")

    new_errors: dict = {}

    if want("units"):
        err: dict = {}
        v = _try(lambda: yolo_units.extract_units(img), None, "棋子(YOLO)", err)
        if v is not None:
            store["units"] = v
        new_errors.update(err)

    if want("balls_yolo"):
        err = {}
        # 口径: yolo_units.predict() 的返回值里**不含** ball/legend 两类 (它们在 `_to_cells`
        # 被丢弃), 分到 `last_balls()` / `last_legends()`。旧实现从返回值里按名字筛是筛不到的
        # (实测 2026-11-14: 返回值 8 项全是棋子, 名字里没有 ball/小小英雄)。
        _pred = _try(lambda: yolo_units.predict(img, model=yolo_units.ball_onnx_path()),
                     None, "奖励球(YOLO)", err)
        if _pred is not None:
            store["balls_yolo"] = yolo_units.last_balls()
        _lpred = _try(lambda: yolo_units.predict(img, model=yolo_units.legend_onnx_path()),
                      None, "小小英雄(YOLO)", err)
        if _lpred is not None:
            store["legends"] = yolo_units.last_legends()
        new_errors.update(err)

    if want("balls_color"):
        err = {}
        v = _try(lambda: reward_ball.find_balls(img), None, "奖励球(颜色)", err)
        if v is not None:
            store["balls_color"] = v
        new_errors.update(err)

    if want("bars"):
        err = {}
        # 快速口径: `match_cells=set()` 跳过"全棋子库多尺度头像匹配"(单只 145ms, 9 只 1.3s;
        # 实测全量 1637ms -> 只算血条 51ms)。名字由 YOLO 那边给 (~7ms 全名), 这里只要
        # 血条框 / 星数 / 格位 / 小小英雄名牌 (include_legend=True 才能拿到 legend 单位)。
        v = _try(lambda: board.extract_units(img, include_legend=True, match_cells=set()),
                 None, "血条(board.extract_units)", err)
        if v is not None:
            store["bars"] = v
        new_errors.update(err)

    if want("shop"):
        err = {}
        v = _try(lambda: rex.extract_info(img, [InfoTypes.Shop]).get("shop"),
                 None, "商店", err)
        if v is not None:
            store["shop"] = v
        new_errors.update(err)

    if want("equip_bar"):
        err = {}
        v = _try(lambda: equipment.extract_equipment(img), None, "装备栏", err)
        if v is not None:
            store["equip_bar"] = v
        new_errors.update(err)

    if want("board_equips"):
        err = {}
        # 口径: extract_board_equips 的 units 参数要的是 board.extract_units 的结果 (含 bar/rect),
        # 不是 YOLO 的 box —— 传错会**静默返回空列表**(实测 2026-11-14, 旧实现就踩了这坑)。
        v = _try(lambda: equipment.extract_board_equips(img, units=store.get("bars")),
                 None, "棋子携带装备", err)
        if v is not None:
            store["board_equips"] = v
        new_errors.update(err)

    for k in due_set:
        ts[k] = now
    for k in list(store.get("_errors") or {}):
        if k not in new_errors and k in due_set:
            store["_errors"].pop(k, None)
    store["_errors"].update(new_errors)

    return {
        "units": store.get("units") or dict(_EMPTY_UNITS),
        "bars": store.get("bars") or dict(_EMPTY_UNITS),
        "balls_yolo": store.get("balls_yolo") or [],
        "legends": store.get("legends") or [],
        "balls_color": store.get("balls_color") or [],
        "shop": store.get("shop") or [],
        "equip_bar": store.get("equip_bar") or [],
        "board_equips": store.get("board_equips") or [],
        "errors": dict(store.get("_errors") or {}),
        "fresh": sorted(due_set),
    }


def merge_stars(data: dict) -> dict:
    """把血条口径给的星数 (bars) 按最近中心对齐到 YOLO 棋子 (units) 上, 便于画/打印星级.

    YOLO 只给名字, 星数只能来自血条条纹 (board.extract_units 的 unit["stars"]); 血条不可用时
    保持 None, 上层显示 "-" / 不画星, 绝不猜。
    """
    units = data.get("units") or _EMPTY_UNITS
    bars = data.get("bars") or _EMPTY_UNITS
    pool = []
    for area in ("board", "bench"):
        for b in bars.get(area) or []:
            c = b.get("center") or b.get("bar")
            st = b.get("stars")
            if not c or st is None:
                continue
            cx = float(c[0]) if len(c) > 2 else float(c[0])
            cy = float(c[1]) if len(c) > 2 else float(c[1])
            pool.append((area, cx, cy, int(st)))
    out = {"board": [], "bench": []}
    for area in ("board", "bench"):
        for u in units.get(area) or []:
            nd = dict(u)
            nd["stars"] = None
            c = nd.get("center") or nd.get("box")
            if c and len(c) >= 2:
                ux, uy = float(c[0]), float(c[1])
                best = None
                for (barea, bx, by, st) in pool:
                    if barea != area:
                        continue
                    d = ((bx - ux) ** 2 + (by - uy) ** 2) ** 0.5
                    if d <= 70.0 and (best is None or d < best[0]):
                        best = (d, st)
                if best is not None:
                    nd["stars"] = best[1]
            out[area].append(nd)
    return out


# --------------------------------------------------------------------------- #
# 画一帧 (汇总数据 -> 透明画布)
# --------------------------------------------------------------------------- #
def draw(data: dict, *, size=None, show: dict | None = None,
         header: str | None = None) -> Image.Image:
    """把 `collect` 的汇总数据画成一张透明 RGBA 画布 (尺寸默认 1920×1080)."""
    sz = tuple(size) if size else (1920, 1080)
    units = merge_stars(data) if data.get("bars") else (data.get("units") or _EMPTY_UNITS)
    return debug_overlay.draw_frame(
        sz,
        units=units,
        bars=data.get("bars"),
        legends=data.get("legends"),
        shop=data.get("shop"),
        equip_bar=data.get("equip_bar"),
        board_equips=data.get("board_equips"),
        balls_yolo=data.get("balls_yolo"),
        balls_color=data.get("balls_color"),
        show=show,
        header=header,
    )


# --------------------------------------------------------------------------- #
# 可选的后台绘制线程 (决策循环忙时礼让; 取帧来源可注入)
# --------------------------------------------------------------------------- #
class LiveDrawThread:
    """后台"取帧 + 识别 + 画到 overlay"线程 (不是决策循环的一部分, 只做观察绘制)."""

    def __init__(self, ld_index=None, show: dict | None = None) -> None:
        self.ld_index = ld_index
        self.show = show
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cache: dict = {}
        self._frames = 0

    # ---- 生命周期 ----
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        if not getattr(default_config, "OVERLAY_DRAW_ENABLED", False):
            print("[overlay] OVERLAY_DRAW_ENABLED=False, 绘制线程未启动")
            return False
        if not debug_overlay.start(self.ld_index):
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="live-draw", daemon=True)
        self._thread.start()
        return True

    def stop(self, wait: float = 3.0) -> None:
        self._stop.set()
        th = self._thread
        if th is not None:
            try:
                th.join(timeout=wait)
            except Exception:  # noqa: BLE001
                pass
        self._thread = None

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def describe(self) -> str:
        return f"[overlay] 绘制线程 {'在跑' if self.is_alive() else '已停'} 已画 {self._frames} 帧"

    # ---- 主循环 ----
    def _loop(self) -> None:
        last = {}
        while not self._stop.is_set():
            tick = loop_tick(self.show)
            if tick <= 0:
                time.sleep(0.5)
                continue
            if _is_main_busy():
                time.sleep(_MIN_TICK)
                continue
            if not debug_overlay.is_active():
                print("[overlay] 窗口已不可用, 绘制线程退出")
                break
            t0 = time.monotonic()
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                print(f"[overlay] 绘制帧失败: {type(exc).__name__}: {exc}")
                time.sleep(0.5)
            dt = time.monotonic() - t0
            time.sleep(max(0.0, tick - dt))

    def _tick(self) -> None:
        get = get_frame()
        img = get()
        if img is None:
            return
        due = due_keys(self._cache.setdefault("_ts", {}), show=self.show)
        data = collect(img, cache=self._cache, due=due, show=self.show)
        frame = draw(data, size=img.size, show=self.show)
        if debug_overlay.set_frame(frame):
            self._frames += 1


def start(ld_index=None, show: dict | None = None) -> LiveDrawThread | None:
    """启动后台绘制线程 (OVERLAY_DRAW_ENABLED=False 时返回 None)."""
    th = LiveDrawThread(ld_index, show=show)
    return th if th.start() else None
