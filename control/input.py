"""通用键鼠输入层: 截图坐标系 (default_config.DEFAULT_RESOLUTION) → 雷电模拟器.

统一 API (前台/后台由 default_config.BACKGROUND_INPUT 控制, 运行时改属性即时生效):
    click_left(x, y)                  左键单击
    click_right(x, y)                 右键单击
    drag(x1, y1, x2, y2, ...)         左键拖拽 (按住分步移动再松开)
    press_key(vk) / release_key(vk)   键盘按下/释放 (VK 码, 如 ord('D'))
    tap_key(vk, duration=0.05)        组合: 按下 → 停顿 → 释放

两种实现:
    前台: SetCursorPos + mouse_event / keybd_event 真实输入 (窗口置前, 占用真实鼠标)
    后台: PostMessage WM_LBUTTON*/WM_KEY* 到 RenderWindow 渲染子窗口
          (不抢前台, 不动真实光标; 雷电模拟器实测游戏可收到, 见 scripts/test_bg_mouse.py)
"""

import re
import subprocess
import time

import win32api
import win32con
import win32gui

from capture.window import (activate, find_ldplayer,
                            find_render_window, identify, release_foreground)
from config.config import default_config
from overlay import mouse_icon
# 子进程不弹控制台黑框 (2026-10-28): ldconsole 查询分辨率 / adb 后台点击都会闪黑框
from utils.no_window import NO_WINDOW


_ICON_CTX: dict = {"hwnd": 0, "ld_index": 0}  # 主窗口句柄 -> 雷电ID 的一次性解析缓存


def _window_pair() -> tuple[int, int, int]:
    """(主窗口 hwnd, RenderWindow 子窗口 hwnd, 雷电实例 ID) —— 一次枚举取全.

    后两个是后台点击用的; 主窗口句柄给指示图标判「点击位置是否被别的窗口盖住」,
    雷电实例 ID 给它挑本实例专属的卡通人物 (用户 m00064)。
    每进程通过 config.LDPLAYER_WINDOW 绑定自己的实例 (capture/window.py:311-326),
    所以 find_ldplayer()[0] 就是本进程服务的那个窗口。
    实例 ID 只在句柄变化时解析一次 (identify 可能要跑 ldconsole, 不能每次点击都问)。
    """
    wins = find_ldplayer()
    if not wins:
        raise RuntimeError("未找到雷电模拟器窗口, 请先启动模拟器")
    main = int(wins[0]["hwnd"])
    render = find_render_window(main)
    if render is None:
        raise RuntimeError("未找到 RenderWindow 子窗口")
    if _ICON_CTX["hwnd"] != main:  # 实例重启/换窗口才重新解析, 平时零成本
        try:  # 只用来挑哪套卡通人物, 识别失败按 0 号人物走, 不影响点击
            _ICON_CTX["ld_index"] = int((identify(wins[0]) or {}).get("ld_index") or 0)
        except Exception:  # noqa: BLE001
            _ICON_CTX["ld_index"] = 0
        _ICON_CTX["hwnd"] = main
    return main, render, _ICON_CTX["ld_index"]


def _render_window() -> int:
    """查找雷电模拟器渲染子窗口句柄 (只要子窗口句柄的调用走这里)."""
    return _window_pair()[1]


def _main_hwnd() -> int:
    wins = find_ldplayer()
    if not wins:
        raise RuntimeError("未找到雷电模拟器窗口, 请先启动模拟器")
    return wins[0]["hwnd"]


def _to_screen(x: float, y: float, render: int) -> tuple[int, int]:
    """截图坐标按渲染窗口实际尺寸等比换算为屏幕坐标 (前台用)."""
    left, top, right, bottom = win32gui.GetWindowRect(render)
    w, h = right - left, bottom - top
    res_w, res_h = default_config.DEFAULT_RESOLUTION
    return int(left + w * x / res_w), int(top + h * y / res_h)


def _to_client(x: float, y: float, render: int) -> tuple[int, int]:
    """截图坐标按渲染窗口客户区尺寸等比换算为客户区坐标 (后台 PostMessage 用)."""
    _l, _t, r, b = win32gui.GetClientRect(render)
    res_w, res_h = default_config.DEFAULT_RESOLUTION
    return int(r * x / res_w), int(b * y / res_h)


def _client_to_screen(cx: int, cy: int, render: int) -> tuple[int, int]:
    """渲染窗口客户区坐标 → 屏幕坐标 (后台指示图标定位用, 精确到客户区)."""
    return win32gui.ClientToScreen(render, (cx, cy))


# ---------- 后台: PostMessage 到渲染子窗口 ----------

def _bg_click(x: float, y: float, *, right: bool = False) -> None:
    main, render, ld_index = _window_pair()
    cx, cy = _to_client(x, y, render)
    # 后台指示图标: 本实例专属卡通人物; 该点被别的窗口盖住时不显示 (用户 m00064)
    mouse_icon.show(*_client_to_screen(cx, cy, render), "click",
                    main_hwnd=main, ld_index=ld_index)
    lp = win32api.MAKELONG(cx, cy)
    down = win32con.WM_RBUTTONDOWN if right else win32con.WM_LBUTTONDOWN
    up = win32con.WM_RBUTTONUP if right else win32con.WM_LBUTTONUP
    wParam = 0 if right else win32con.MK_LBUTTON
    win32api.PostMessage(render, win32con.WM_MOUSEMOVE, 0, lp)
    time.sleep(default_config.CLICK_MOVE_DELAY)
    win32api.PostMessage(render, down, wParam, lp)
    time.sleep(default_config.CLICK_DOWN_DELAY)
    win32api.PostMessage(render, up, 0, lp)
    time.sleep(default_config.CLICK_UP_DELAY)


def _bg_drag(x1: float, y1: float, x2: float, y2: float, *, steps: int,
             press_pause: float, step_interval: float, release_pause: float,
             progress: list[float] | None = None) -> None:
    main, render, ld_index = _window_pair()
    # 2026-10-29 (用户 m00072): 后台 PostMessage 拖拽也必须先做鼠标体检。
    # 真实鼠标若停在 LD 模拟器窗口内, 模拟器的"虚拟鼠标"会和注入的拖拽坐标抢位置 ——
    # 落点按真实光标算(或拖拽被系统改回去), 症状就是"装备离手(库存 -1)却落到别处",
    # 这正是 equip_to 低成功率的一个直接诱因。前台 _fg_drag 早在 2026-10-21 就有这步,
    # 后台路径一直漏掉, 而 BACKGROUND_INPUT 默认 True(默认就走这里) —— 全量补齐。
    # 只在"鼠标在窗口内/屏幕外"时才移动(零打扰), 落点选"屏幕内且窗口外", 绝不移出屏幕。
    _inside, _pos, _rect = pointer_inside_screen()
    _win = _ld_window_rect()
    _in_win = bool(_win is not None and _inside
                   and _win[0] <= _pos[0] < _win[2] and _win[1] <= _pos[1] < _win[3])
    # 再看一眼"矩形内"是不是真压在模拟器上 (用户 m00001): 被浏览器等压在底层时不算干扰,
    # 体检也不会动用户光标。判不出(None)按老口径当作"真压在窗口上", 保守优先。
    _over = _pointer_over_ld_window(_pos) if _in_win else None
    print(f"[input] 后台拖拽前鼠标位态: pos={_pos} in_window={_in_win} "
          f"压在模拟器上={_over} 窗口={_win}", flush=True)
    _ensure_pointer()   # 窗口内/屏幕外都归位到"屏幕内窗口外"; 后台不依赖真实光标, 不传 target
    cx1, cy1 = _to_client(x1, y1, render)
    cx2, cy2 = _to_client(x2, y2, render)
    sx1, sy1 = _client_to_screen(cx1, cy1, render)
    sx2, sy2 = _client_to_screen(cx2, cy2, render)
    # 后台指示图标: 显示在拖拽起点, 途中跟随; 用本实例专属卡通人物 (用户 m00064)
    mouse_icon.show(sx1, sy1, "drag", main_hwnd=main, ld_index=ld_index)
    win32api.PostMessage(render, win32con.WM_MOUSEMOVE, 0,
                         win32api.MAKELONG(cx1, cy1))
    time.sleep(default_config.DRAG_MOVE_DELAY)
    win32api.PostMessage(render, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON,
                         win32api.MAKELONG(cx1, cy1))
    time.sleep(press_pause)  # 按住片刻, 让游戏识别为拖拽而非点击
    # 进度序列: 缺省 = 线性 i/steps (历史行为); 装备档会传进来自带渐入渐出的剖面 (drag_progress)
    _prog = progress if progress else [i / steps for i in range(1, steps + 1)]
    for t in _prog:
        win32api.PostMessage(render, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON,
                             win32api.MAKELONG(int(cx1 + (cx2 - cx1) * t),
                                               int(cy1 + (cy2 - cy1) * t)))
        mouse_icon.move(sx1 + (sx2 - sx1) * t, sy1 + (sy2 - sy1) * t)
        time.sleep(step_interval)
    time.sleep(release_pause)  # 落点停留, 等游戏判定
    win32api.PostMessage(render, win32con.WM_LBUTTONUP, 0,
                         win32api.MAKELONG(cx2, cy2))
    time.sleep(default_config.DRAG_END_DELAY)


def _bg_key(vk: int, *, down: bool) -> None:
    render = _render_window()
    sc = win32api.MapVirtualKey(vk, 0)
    lp = 1 | (sc << 16)
    if not down:
        lp |= (1 << 30) | (1 << 31)  # KEYUP 惯例位: 前次状态+转换
    msg = win32con.WM_KEYDOWN if down else win32con.WM_KEYUP
    win32api.PostMessage(render, msg, vk, lp)


# ---------- 前台: 真实键鼠 ----------

def _fg_click(x: float, y: float, *, right: bool = False) -> None:
    render = _render_window()
    screen_x, screen_y = _to_screen(x, y, render)
    main_hwnd = _main_hwnd()
    activate(main_hwnd)  # 真实鼠标点击需要窗口在前台
    time.sleep(0.15)
    win32api.SetCursorPos((screen_x, screen_y))
    time.sleep(default_config.CLICK_MOVE_DELAY)
    down = win32con.MOUSEEVENTF_RIGHTDOWN if right else win32con.MOUSEEVENTF_LEFTDOWN
    up = win32con.MOUSEEVENTF_RIGHTUP if right else win32con.MOUSEEVENTF_LEFTUP
    win32api.mouse_event(down, 0, 0, 0, 0)
    time.sleep(default_config.CLICK_DOWN_DELAY)
    win32api.mouse_event(up, 0, 0, 0, 0)
    time.sleep(default_config.CLICK_UP_DELAY)
    release_foreground(main_hwnd)      # 撤置顶 + 释放前台锁, 让用户点其它窗口能覆盖 (用户 2026-10-17)


def _virtual_screen_rect() -> tuple[int, int, int, int]:
    """(虚拟)屏幕矩形 (左, 上, 右, 下), 支持多显示器时的负坐标."""
    vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    return int(vx), int(vy), int(vx + vw), int(vy + vh)


def pointer_inside_screen() -> tuple[bool, tuple[int, int], tuple[int, int, int, int]]:
    """检测真实鼠标是否落在(虚拟)屏幕范围内.

    用户 2026-10-21: LD 模拟器的虚拟鼠标与本机真实鼠标会互相干扰, 真实鼠标一旦
    跑到屏幕外(多屏/远程桌面/虚拟鼠标抢焦点), 前台拖拽的 SetCursorPos 会被系统
    改回去, 拖拽整条链路白跑. 所以每次拖拽前先查一次, 不在屏内就先归位.
    """
    x, y = win32api.GetCursorPos()
    rect = _virtual_screen_rect()
    inside = rect[0] <= x < rect[2] and rect[1] <= y < rect[3]
    return bool(inside), (int(x), int(y)), rect


def _ld_window_rect() -> tuple[int, int, int, int] | None:
    """当前绑定实例的主窗口矩形 (左, 上, 右, 下); 拿不到返回 None."""
    try:
        hwnd = _main_hwnd()
        if not hwnd:
            return None
        return tuple(int(v) for v in win32gui.GetWindowRect(hwnd))  # type: ignore[return-value]
    except Exception:
        return None


def _pointer_over_ld_window(pos: tuple[int, int]) -> bool | None:
    """光标那一点上, 最顶层窗口是不是本实例 (用户 m00001, 2026-10-31).

    要区分的两种情况:
      - 模拟器窗口**真的**在光标底下 (前台/可见, 用户就是在它上面操作) -> 该搬走光标;
      - 模拟器被浏览器等压在**下层**, 光标只是几何上落在它的矩形里 (用户操作的是上层窗口)
        -> 绝不能碰用户的光标。
    判据: `WindowFromPoint(光标点)` 取该点最顶层窗口, 再取顶层祖先 (`GA_ROOT`), 是不是
    本实例主窗口 —— 与 `overlay/mouse_icon.py::_occluded` 同源做法 (指示图标自身是
    WS_EX_TRANSPARENT 穿透窗口, WindowFromPoint 会跳过它, 不会自己遮自己)。

    返回 True=真压在模拟器上; False=那点被**别的**窗口盖着; None=判不出(异常/拿不到句柄)。
    """
    try:
        main = int(_main_hwnd() or 0)
    except Exception:  # noqa: BLE001 - 枚举不到窗口 = 判不出, 交给调用方兜底
        return None
    if not main:
        return None
    try:
        top = win32gui.WindowFromPoint((int(pos[0]), int(pos[1])))
        if not top:
            return None
        return win32gui.GetAncestor(top, win32con.GA_ROOT) == main
    except Exception:  # noqa: BLE001
        return None


def _park_point(screen: tuple[int, int, int, int],
                win: tuple[int, int, int, int] | None,
                near: tuple[int, int] | None = None) -> tuple[int, int]:
    """给真实鼠标找一个"屏幕内、模拟器窗口外"的落脚点; 窗口占满全屏才退到屏幕中心.

    用户 2026-10-21: 鼠标要移到 **LD 模拟器窗口外、屏幕里边** (它和虚拟鼠标抢位置),
    且**绝不移到屏幕外**; 只有窗口把整个屏幕都占满(全屏)时才没有"窗口外"的点, 只能放
    屏幕中心. `near` 给了就选**离当前鼠标最近**的那条带, 免得鼠标满屏乱飞.
    """
    cx = (screen[0] + screen[2]) // 2
    cy = (screen[1] + screen[3]) // 2
    if win is None:
        return cx, cy
    left, top, right, bottom = win
    # 候选: 窗口右侧条 / 左侧条 / 下方条 / 上方条 的中点, 只留"在屏幕内且不在窗口内"的
    cands = []
    if right < screen[2]:
        cands.append(((right + screen[2]) // 2, cy))
    if left > screen[0]:
        cands.append(((screen[0] + left) // 2, cy))
    if bottom < screen[3]:
        cands.append((cx, (bottom + screen[3]) // 2))
    if top > screen[1]:
        cands.append((cx, (screen[1] + top) // 2))
    valid = [p for p in cands
             if (p[0] != cx or p[1] != cy)
             and screen[0] <= p[0] < screen[2] and screen[1] <= p[1] < screen[3]]
    if not valid:
        return cx, cy          # 窗口占满全屏: 屏幕内没有窗口外的点, 只能退到中心
    if near is not None:
        return min(valid, key=lambda p: (p[0] - near[0]) ** 2 + (p[1] - near[1]) ** 2)
    return valid[0]


def _log_flush(msg: str) -> None:
    """`_ensure_pointer` 的默认日志口: 带 flush.

    AGENTS.md §3: 长任务重定向到文件时 stdout 是块缓冲, 不 flush 会长期 0 字节,
    用户/助手会误判"卡死"。改名 `log` 参数的调用方(测试)不受影响。
    """
    print(msg, flush=True)


def _ensure_pointer(*, target: tuple[int, int] | None = None, log=_log_flush) -> bool:
    """拖拽前的鼠标体检.

    1) 鼠标在屏幕外 -> 拉回屏幕中心;
    2) 鼠标落在 LD 模拟器窗口内 -> 移到"屏幕内但窗口外"的落脚点(没有就屏幕中心),
       因为 LD 的虚拟鼠标会和真实鼠标抢位置, 拖拽会被系统改回去;
       **但**仅当光标真的压在模拟器可见区域上才搬 (用户 m00001, 2026-10-31): 模拟器常被
       压在浏览器等窗口**下层**, 此时用户在上层操作, 光标只是几何上落在模拟器矩形里 ——
       老判据会把他正在用的鼠标搬走。判据见 `_pointer_over_ld_window`;
       总闸 `default_config.DRAG_PARK_WHEN_OCCLUDED=True` 可退回老行为。
    3) 最后把光标预置到 target 并读回校验.
    返回 True = 鼠标位置可靠; False = 归位/预置失败(调用方仍继续, 但日志会留证据).
    """
    inside, pos, rect = pointer_inside_screen()
    win = _ld_window_rect()
    need_park = (not inside)
    cover = None   # 光标那点的遮挡判定: True=压在模拟器上 / False=被别的窗口盖着 / None=判不出
    if inside and win is not None:
        in_rect = win[0] <= pos[0] < win[2] and win[1] <= pos[1] < win[3]
        if in_rect:
            cover = _pointer_over_ld_window(pos)
            # 被别的窗口盖住 -> 不动用户光标 (除非显式开回老行为); 判不出(None)时保守照搬
            need_park = (cover is not False) or bool(
                getattr(default_config, "DRAG_PARK_WHEN_OCCLUDED", False))
    if need_park:
        px, py = _park_point(rect, win, near=pos)
        why = "屏幕外" if not inside else "在模拟器窗口内"
        log(f"[input] 拖拽前检测: 鼠标{why} pos={pos} 窗口={win} -> 移到 ({px},{py})")
        win32api.SetCursorPos((px, py))
        time.sleep(0.05)
        inside, pos, _ = pointer_inside_screen()
        if not inside:
            log(f"[input] [!] 鼠标仍回不到屏幕内 pos={pos} -> 本次拖拽可能失败")
            return False
    elif cover is False:
        # 用户正在上层窗口操作, 我们**没碰**他的光标 —— 留痕, 便于统计"零打扰"覆盖了多少次
        log(f"[input] 拖拽前检测: 鼠标在模拟器矩形内但被其它窗口盖住 pos={pos} "
            f"窗口={win} -> 不动用户鼠标")
    if target is None:
        return True
    for attempt in (1, 2):
        win32api.SetCursorPos(target)
        time.sleep(0.02)
        got = win32api.GetCursorPos()
        if abs(got[0] - target[0]) <= 2 and abs(got[1] - target[1]) <= 2:
            return True
        log(f"[input] 拖拽前定位第 {attempt} 次不符: 期望={target} 实际={got}")
    return False


def _fg_drag(x1: float, y1: float, x2: float, y2: float, *, steps: int,
             press_pause: float, step_interval: float, release_pause: float,
             progress: list[float] | None = None) -> None:
    render = _render_window()
    sx1, sy1 = _to_screen(x1, y1, render)
    sx2, sy2 = _to_screen(x2, y2, render)
    main_hwnd = _main_hwnd()
    activate(main_hwnd)
    time.sleep(0.15)
    # 拖拽前确认鼠标在屏幕内 + 预置起点可读回 (用户 2026-10-21: 虚拟鼠标与真实鼠标抢位置会导致拖拽失败)
    _ensure_pointer(target=(sx1, sy1))
    time.sleep(default_config.DRAG_MOVE_DELAY)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(press_pause)  # 按住片刻, 让游戏识别为拖拽而非点击
    _prog = progress if progress else [i / steps for i in range(1, steps + 1)]
    for t in _prog:
        win32api.SetCursorPos((int(sx1 + (sx2 - sx1) * t), int(sy1 + (sy2 - sy1) * t)))
        time.sleep(step_interval)
    time.sleep(release_pause)  # 落点停留, 等游戏判定
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(default_config.DRAG_END_DELAY)
    release_foreground(main_hwnd)      # 撤置顶 + 释放前台锁, 让用户点其它窗口能覆盖 (用户 2026-10-17)


def _fg_key(vk: int, *, down: bool) -> None:
    flags = 0 if down else win32con.KEYEVENTF_KEYUP
    win32api.keybd_event(vk, 0, flags, 0)


# ---------- 真后台: 雷电 adb 注入 (不碰宿主键鼠, 对 WebView 也有效) ----------

_ADB_SIZE: dict = {"t": 0.0, "size": None, "index": None}


def _ldconsole_exe() -> str | None:
    """雷电自带 ldconsole.exe 的路径 (复用 capture.window 的自动定位)."""
    from capture.window import _ldconsole_path
    return _ldconsole_path()


def target_ld_index() -> int:
    """当前绑定实例的**雷电 ID** (find_ldplayer 已把要操作的那个排在第 0 位)."""
    from capture.window import identify
    wins = find_ldplayer()
    if not wins:
        raise RuntimeError("未找到雷电模拟器窗口, 请先启动模拟器")
    return int(identify(wins[0]).get("ld_index") or 0)


def _android_size(ld_index: int, ttl: float = 600.0) -> tuple[int, int]:
    """Android 内部物理分辨率 (`adb shell wm size`), 带缓存; 取不到就假定与截图基准一致.

    实测 (雷电 9 / 实例 1): `Physical size: 1920x1080` —— 与 `DEFAULT_RESOLUTION` 1:1,
    所以坐标可以直接用; 这里仍然动态读一次, 免得用户改过模拟器分辨率后点偏。
    """
    now = time.time()
    if (_ADB_SIZE["size"] and _ADB_SIZE["index"] == ld_index
            and now - _ADB_SIZE["t"] < ttl):
        return _ADB_SIZE["size"]
    res = tuple(default_config.DEFAULT_RESOLUTION)
    exe = _ldconsole_exe()
    if exe:
        try:
            out = subprocess.run(
                [exe, "adb", "--index", str(ld_index), "--command", "shell wm size"],
                capture_output=True, timeout=15,
                creationflags=NO_WINDOW).stdout.decode("utf-8", "replace")
            m = re.search(r"(\d+)\s*x\s*(\d+)", out)
            if m:
                res = (int(m.group(1)), int(m.group(2)))
        except Exception:  # noqa: BLE001 - 读不到就按基准分辨率走, 不能因此中断采集
            pass
    _ADB_SIZE.update(t=now, size=res, index=ld_index)
    return res


def adb_tap(x: float, y: float, *, ld_index: int | None = None) -> None:
    """**真后台**点击: 经雷电 adb 在 Android 内部注入 tap —— 宿主键鼠全程不动, 不抢焦点.

    为什么需要 (用户 m05001/m05003): 游戏里点不动的内嵌 WebView (月卡/支付/活动页的关闭 X)
    以前只能靠**前台真实鼠标**, 那会抢走用户正在用的键鼠。adb 注入发生在 Android 内部,
    WebView 照样收得到, 而 Windows 这边一个鼠标事件都不发、前台窗口不变。
    """
    idx = target_ld_index() if ld_index is None else int(ld_index)
    exe = _ldconsole_exe()
    if not exe:
        raise RuntimeError("未找到 ldconsole.exe (雷电自带的命令行工具), 无法用 adb 后台点击")
    aw, ah = _android_size(idx)
    res_w, res_h = default_config.DEFAULT_RESOLUTION
    px, py = int(aw * x / res_w), int(ah * y / res_h)
    try:  # 指示图标: 只是画一个置顶穿透窗口, 带 WS_EX_NOACTIVATE, 不会抢焦点
        main, render, _ld_unused = _window_pair()
        mouse_icon.show(*_to_screen(x, y, render), "click",
                        main_hwnd=main, ld_index=idx)
    except Exception:  # noqa: BLE001 - 指示图标失败不影响点击
        pass
    subprocess.run([exe, "adb", "--index", str(idx),
                    "--command", f"shell input tap {px} {py}"],
                   capture_output=True, timeout=15, creationflags=NO_WINDOW)


def click_left_adb(x: float, y: float) -> None:
    """左键单击 (adb 后台注入) —— 需要点内嵌 WebView 时的**首选**, 替代前台真实鼠标."""
    adb_tap(x, y)


# ---------- 统一 API (按 BACKGROUND_INPUT 分发) ----------

def click_left(x: float, y: float) -> None:
    """左键单击截图坐标系下的点 (后台发送消息 / 前台真实点击)."""
    if default_config.BACKGROUND_INPUT:
        _bg_click(x, y)
    else:
        _fg_click(x, y)


def click_left_background(x: float, y: float) -> None:
    """左键单击 —— **强制后台 PostMessage 通道** (不管 BACKGROUND_INPUT 怎么配).

    为什么单独开这个入口 (control/fallback.py 需要): 兜底点击要按通道名逐个试, "bg"
    通道必须真的是后台 PostMessage 语义 —— 若复用 `click_left`, 一旦用户把
    BACKGROUND_INPUT 改成 False, "bg" 通道就会偷偷变成前台真实鼠标, 通道名与实际
    行为不符, 排障时会被日志误导。这里固定走 `_bg_click`, 通道语义唯一。
    """
    _bg_click(x, y)


def click_left_foreground(x: float, y: float) -> None:
    """左键单击 —— **默认走 adb 后台注入**, 不再抢用户键鼠 (用户 m05001/m05003)。

    历史 (用户 m03733 现场): 游戏里的月卡/支付/活动页是**内嵌 WebView**, 后台 `PostMessage`
    点它的关闭 X **连续 27 次全部无效** —— 采集卡死在同一坐标, 最后
    `TimeoutError: 未能在 180 秒内离开结算页`。当时的解法是退回**前台真实鼠标**
    (`activate()` + `SetCursorPos` + `mouse_event`), 但那条路会**抢走用户正在用的键鼠**
    (用户 m05001: "你丫的现在怎么占用我前台键鼠了? 不是后台么? 全部要用后台键鼠方案!";
    m05003 定位到就是在**退出游戏**点 UI 那一下)。

    现在改走 `adb_tap` (雷电 adb 在 Android 内部注入 tap): 对 WebView 一样有效, 且宿主
    键鼠全程不动。真要临时退回前台真实鼠标 (调试用), 把
    `default_config.ALLOW_FOREGROUND_FALLBACK` 置 True 即可。
    """
    if getattr(default_config, "ALLOW_FOREGROUND_FALLBACK", False):
        _fg_click(x, y)
    else:
        adb_tap(x, y)


def click_right(x: float, y: float) -> None:
    """右键单击截图坐标系下的点."""
    if default_config.BACKGROUND_INPUT:
        _bg_click(x, y, right=True)
    else:
        _fg_click(x, y, right=True)


def drag_steps(x1: float, y1: float, x2: float, y2: float, cfg=None, *,
               base: int | None = None, cap: float | None = None,
               top: int | None = None) -> int:
    """**按距离定步数** (P0-26 定案, 用户 m00821 实机 2026-11-02 02:18: 「位置远, 而我们的 steps 是固定的,
    导致拖拽速度过快」) —— 固定的应该是**每步跨度上限** `DRAG_MAX_STEP_PX`, 不是步数。

    为什么要它: `DRAG_STEPS=14` 时近格 690px = 49px/步 (离线 0/37 从不失败)、
    远角 己方(4,7) 1390px = **99px/步** (>=1200px 档真失败 74%)，每步间隔又固定 0.012s
    → 等效指针速度 ~8000px/s → 游戏不把这串 MOVE 当拖拽 → 装备压根没离手。

    规则: `steps = clamp(ceil(距离 / DRAG_MAX_STEP_PX), DRAG_STEPS, DRAG_STEPS_MAX)`，
    下限取原 `DRAG_STEPS` → **近处行为与今天一字不差**（距离 <= DRAG_STEPS×DRAG_MAX_STEP_PX=840px
    一律还是 14 步），只有远距才加步数把跨度压回上限以内。
    `cap <= 0` = 关掉自适应，完全退回旧行为（留的开关，实机万一有反证可一键回退）。
    `base/cap/top` 显式传入时覆盖 cfg 里的同名常量 —— 给"某个动作单独一套时序"用
    (见 `_drag_timing`，equip_to 走 `EQUIP_DRAG_*` 那一套)。
    """
    cfg = cfg or default_config
    base = int(cfg.DRAG_STEPS if base is None else base)
    cap = float(getattr(cfg, "DRAG_MAX_STEP_PX", 0.0) or 0.0) if cap is None else float(cap)
    if cap <= 0:
        return base
    dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    need = int(-(-dist // cap))                       # ceil，不用 math.ceil 以便保持纯核心类型
    top = int(getattr(cfg, "DRAG_STEPS_MAX", base) or base) if top is None else int(top)
    return max(base, min(need, max(top, base)))


def drag_progress(dist_px: float, *, steps: int, ease_head=(), ease_tail=(),
                  max_step_px: float = 0.0, ease_min_dist: float = 0.0,
                  steps_max: int = 0) -> list[float]:
    """一次拖拽的**归一化进度序列** `t` (递增、末位恒 = 1.0)，替代线性 `i/steps`。

    - `ease_head` / `ease_tail` 都空 → 返回 `[i/steps]`，与历史线性行为**逐比特一致**；
    - 有剖面 → 头几步按 `ease_head` 的**每步像素**渐入、中段按 `max_step_px` 匀速铺满、
      尾几步按 `ease_tail` 渐出 (最后一步只有 1px)。用户 m01024 原话:「保证最后几个像素是
      [1~3px 挪过去] 的效果, 让游戏反应过来」—— 猜的是游戏按**末段指针速度**判定落点。
    - 距离太短 (铺不满 head+tail+一个匀速段, 或 < `ease_min_dist`) → 退回线性。
    - `steps_max > 0` → 总 MOVE 数不许超过它 (宁可调大中段跨度, 也不让单次拖拽时长失控)。
    """
    dist = float(dist_px)
    if dist <= 0:
        return [1.0]
    head = [float(v) for v in (ease_head or ()) if float(v) > 0]
    tail = [float(v) for v in (ease_tail or ()) if float(v) > 0]
    linear = [i / steps for i in range(1, int(steps) + 1)] if steps > 0 else [1.0]
    if not head and not tail:
        return linear
    cap = float(max_step_px) if max_step_px and max_step_px > 0 else 0.0
    if dist < float(ease_min_dist) or dist <= sum(head) + sum(tail) + max(cap, 1.0):
        return linear                       # 剖面铺不开, 线性更划算
    mid = dist - sum(head) - sum(tail)
    n_mid = max(1, int(-(-mid // cap))) if cap > 0 else max(1, int(steps))
    if steps_max and int(steps_max) > 0:    # 总步数封顶: 牺牲中段跨度换时长有界
        n_mid = max(1, min(n_mid, int(steps_max) - len(head) - len(tail)))
    deltas = head + [mid / n_mid] * n_mid + tail
    out: list[float] = []
    cum = 0.0
    for d in deltas:
        cum += d
        out.append(1.0 if cum >= dist else cum / dist)
    out[-1] = 1.0
    return out


def _drag_timing(timing: str | None) -> dict:
    """取**一套**拖拽时序常量。缺省 = 全局 `DRAG_*`；`"equip"` = 装备专用 `EQUIP_DRAG_*` 那一套。

    用户 m01032 (2026-11-02 03:13): 「加入个 drag_ease_in_out 之类的参数, 只有 equip_to 应用.
    并且, equip_to 的 drag 速度, 单独设置一套.」→ 渐入渐出与更长的按住只进装备档,
    place/swap/sell/combine 继续走全局那套、行为一字不变。`EQUIP_DRAG_ENABLED=False` 时
    装备档整体退回全局档 (留的对照开关)。
    """
    cfg = default_config
    g = lambda k, d: getattr(cfg, k, d)  # noqa: E731 - 就地取常量, 不做成公共工具
    out = {
        "steps_base": int(cfg.DRAG_STEPS),
        "max_step_px": float(g("DRAG_MAX_STEP_PX", 0.0) or 0.0),
        "steps_max": int(g("DRAG_STEPS_MAX", cfg.DRAG_STEPS) or cfg.DRAG_STEPS),
        "press_pause": float(cfg.DRAG_PRESS_PAUSE),
        "step_interval": float(cfg.DRAG_STEP_INTERVAL),
        "release_pause": float(cfg.DRAG_RELEASE_PAUSE),
        "ease": bool(g("DRAG_EASE_IN_OUT", False)),
        "ease_head": tuple(g("DRAG_EASE_HEAD_PX", ()) or ()),
        "ease_tail": tuple(g("DRAG_EASE_TAIL_PX", ()) or ()),
        "ease_min_dist": float(g("DRAG_EASE_MIN_DIST_PX", 0.0) or 0.0),
    }
    if timing == "equip" and g("EQUIP_DRAG_ENABLED", True):
        out.update({
            "steps_base": int(g("EQUIP_DRAG_STEPS", out["steps_base"])),
            "max_step_px": float(g("EQUIP_DRAG_MAX_STEP_PX", out["max_step_px"]) or 0.0),
            "steps_max": int(g("EQUIP_DRAG_STEPS_MAX", out["steps_max"]) or out["steps_max"]),
            "press_pause": float(g("EQUIP_DRAG_PRESS_PAUSE", out["press_pause"])),
            "step_interval": float(g("EQUIP_DRAG_STEP_INTERVAL", out["step_interval"])),
            "release_pause": float(g("EQUIP_DRAG_RELEASE_PAUSE", out["release_pause"])),
            "ease": bool(g("EQUIP_DRAG_EASE_IN_OUT", True)),
            "ease_head": tuple(g("EQUIP_DRAG_EASE_HEAD_PX", out["ease_head"]) or ()),
            "ease_tail": tuple(g("EQUIP_DRAG_EASE_TAIL_PX", out["ease_tail"]) or ()),
            "ease_min_dist": float(g("EQUIP_DRAG_EASE_MIN_DIST_PX", out["ease_min_dist"]) or 0.0),
        })
    if timing == "board":
        # place/swap 首拖专用档 (用户 m00224, 2026-11-11): 首拖直接改用原"重拖放宽档"
        # (BOARD_REDRAG_*) 一样的慢时序 —— 用户原话:「就按重拖的那个慢一点的速度就很稳了…
        # 一旦拖动失败反而要花更多时间, 不如直接按重拖的慢速度」。重拖仍走显式 drag_kw,
        # 与本档独立; 这里只影响"首拖"(drag_kw=None 时 operations.units.swap 传 timing="board")。
        # BOARD_FIRST_DRAG_FAST=True 可一键回退到旧的全局 DRAG_*(便于实机 A/B 对照)。
        if not g("BOARD_FIRST_DRAG_FAST", False):
            out.update({
                "steps_base": int(g("BOARD_REDRAG_STEPS", out["steps_base"])),
                "press_pause": float(g("BOARD_REDRAG_PRESS_PAUSE", out["press_pause"])),
                "step_interval": float(g("BOARD_REDRAG_STEP_INTERVAL", out["step_interval"])),
                "release_pause": float(g("BOARD_REDRAG_RELEASE_PAUSE", out["release_pause"])),
            })
    if not out["ease"]:
        out["ease_head"], out["ease_tail"] = (), ()
    return out


def drag(x1: float, y1: float, x2: float, y2: float, *, steps: int | None = None,
         press_pause: float | None = None, step_interval: float | None = None,
         release_pause: float | None = None, timing: str | None = None,
         ease_in_out: bool | None = None) -> None:
    """按住左键从 (x1,y1) 拖到 (x2,y2) 后松开 (截图坐标系).

    时序参数缺省时读 config (DRAG_STEPS/DRAG_PRESS_PAUSE/DRAG_STEP_INTERVAL/
    DRAG_RELEASE_PAUSE), 运行时改 config 即时生效; 传参可单次覆盖.
    分小步移动而非瞬移: 游戏对瞬移拖拽的落点判定不稳定, 分步更可靠;
    按住片刻让游戏进入拖拽状态 (过短会被当作点击丢弃), 松开前停留等落点判定.
    **步数缺省按距离自适应** (见 `drag_steps`，P0-26 定案) —— 显式传 steps 的调用方
    (如 equip_to 的重拖放宽档) 覆盖这个默认.

    `timing="equip"` = 换用**装备专用那一套**时序 (更长按住 + 渐入渐出)，见 `_drag_timing`；
    `ease_in_out` 单次强制开/关渐入渐出 (None = 由该档的常量决定)。
    """
    tm = _drag_timing(timing)
    dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    _n_steps = (drag_steps(x1, y1, x2, y2, base=tm["steps_base"], cap=tm["max_step_px"],
                           top=tm["steps_max"]) if steps is None else int(steps))
    _ease = tm["ease"] if ease_in_out is None else bool(ease_in_out)
    head = tm["ease_head"] if _ease else ()
    tail = tm["ease_tail"] if _ease else ()
    prog = drag_progress(dist, steps=_n_steps, ease_head=head, ease_tail=tail,
                         max_step_px=tm["max_step_px"], ease_min_dist=tm["ease_min_dist"],
                         steps_max=tm["steps_max"])
    if (head or tail) and len(prog) >= 2:
        # 渐入渐出的步数由剖面决定, 与线性步数不同 -> 留一行可搜索的痕迹, 便于实机比对时序
        print(f"[input] 拖拽剖面 timing={timing or 'default'} dist={dist:.0f}px "
              f"步数 {_n_steps}->{len(prog)} 末步={(prog[-1] - prog[-2]) * dist:.1f}px "
              f"press={tm['press_pause']:.2f}s 轨迹≈{len(prog) * tm['step_interval']:.2f}s",
              flush=True)
    kw = dict(steps=_n_steps,
              press_pause=tm["press_pause"] if press_pause is None else press_pause,
              step_interval=tm["step_interval"] if step_interval is None else step_interval,
              release_pause=tm["release_pause"] if release_pause is None else release_pause,
              progress=prog)
    if default_config.BACKGROUND_INPUT:
        _bg_drag(x1, y1, x2, y2, **kw)
    else:
        _fg_drag(x1, y1, x2, y2, **kw)


def press_key(vk: int) -> None:
    """按下键盘键 (VK 虚拟键码)."""
    if default_config.BACKGROUND_INPUT:
        _bg_key(vk, down=True)
    else:
        _fg_key(vk, down=True)


def release_key(vk: int) -> None:
    """释放键盘键 (VK 虚拟键码)."""
    if default_config.BACKGROUND_INPUT:
        _bg_key(vk, down=False)
    else:
        _fg_key(vk, down=False)


def tap_key(vk: int, duration: float | None = None) -> None:
    """组合: 按下 → 停顿 → 释放 (duration 缺省读 config.KEY_TAP_DURATION)."""
    press_key(vk)
    time.sleep(default_config.KEY_TAP_DURATION if duration is None else duration)
    release_key(vk)
