"""后台鼠标动作指示图标: 在点击/拖拽对应的屏幕位置闪现提示图标.

背景: 后台输入 (PostMessage) 不动真实光标, 屏幕上看不到机器人点了哪里;
本模块创建一个置顶、鼠标穿透、无边框的分层窗口 (UpdateLayeredWindow 逐像素透明),
在动作位置显示一套"卡通小人"素材里的点击图 / 拖拽图.

设计:
    - 独立守护线程持有窗口并泵消息, control/input.py 只做 PostMessage 通知,
      不阻塞输入主流程; 任何异常静默降级为不显示, 绝不影响点击/拖拽本身
    - 多开时**一个模拟器窗口一个卡通人物** (用户 m00064 第 1 条): 按雷电实例 ID
      (ld_index) 对套数取模选一套, 位图按需加载 (每套两张图)。套来自模板库
      `config.MOUSE_ICON_DIR` 里的 `click_mouse_<i>.png` + `drag_mouse_<i>.png` (用户 m00303):
      第 <i> 套给 window_<i> 显示, 往目录里放一对同号文件就多一个人物, 不用改代码
    - "什么时候才画"由总闸 config.MOUSE_ICON_ONLY_FOREGROUND 统一决定 (用户 m00569
      「是否只能在前台展示」, 当前按要求设 False): False = 前台/遮挡都不判, 任何时候都画;
      True = 目标窗口不在前台就不画, 在前台再按 MOUSE_ICON_SKIP_OCCLUDED 判"点击位置
      是否被别的窗口盖住" (用户 m00064 第 2 条): 判据 = 该点最顶层窗口 (WindowFromPoint)
      的顶层祖先 (GA_ROOT) 是不是目标模拟器主窗口; 指示图标自身是 WS_EX_TRANSPARENT
      鼠标穿透窗口, WindowFromPoint 会跳过它, 不会自遮
    - 拖拽起点先显示点击图标 (按下瞬间), MOUSE_ICON_DRAG_CLICK_DELAY 后切换为
      拖拽图标并跟随路径移动 (move), 停留 MOUSE_ICON_DRAG_SECONDS 后自动隐藏
    - 定位: 图标左下角对齐点击中心, MOUSE_ICON_OFFSET 可再向右上偏移 (x, y);
      素材**等比缩放后底部居中**放进 MOUSE_ICON_SIZE 画布 (不拉伸, 人物不变形,
      脚底贴齐画布下沿 -> 手指指向点击点的相对位置不漂)
    - 开关/尺寸/偏移/时长/图标套集中在 config: MOUSE_ICON_*
    - 验证: python -m tests.capture.test_mouse_icon (实机目视, 不需要模拟器)
"""

import ctypes
import re
import threading
from ctypes import wintypes
from pathlib import Path

import numpy as np
import win32api
import win32con
import win32gui
from PIL import Image

from config.config import default_config

ctypes.windll.user32.SetProcessDPIAware()  # 物理像素, 与 capture.window 保持一致

# pywin32 未封装 CreateDIBSection/SetTimer, 用 ctypes 直调
_gdi32 = ctypes.windll.gdi32
_user32 = ctypes.windll.user32
_DIB_RGB_COLORS = 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [(n, t) for n, t in [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD)]]


class _BITMAPINFO(ctypes.Structure):
    class _RGBQUAD(ctypes.Structure):
        _fields_ = [("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
                    ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte)]
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                ("bmiColors", _RGBQUAD * 1)]


_gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                    ctypes.POINTER(ctypes.c_void_p),
                                    wintypes.HANDLE, wintypes.DWORD]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                             ctypes.c_void_p]  # TIMERPROC=None
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]

# 线程间通知消息 (屏幕坐标打包在 wparam=x / lparam=y)
_WM_SHOW_CLICK = win32con.WM_APP + 1
_WM_SHOW_DRAG = win32con.WM_APP + 2
_WM_MOVE = win32con.WM_APP + 3
_WM_SET_ICONS = win32con.WM_APP + 4  # 换一套卡通人物 (wparam=套号)
_WM_TIMER_ID = 1     # 隐藏计时: 到点隐藏窗口
_WM_TIMER_DRAG_ID = 2  # 拖拽起点切换计时: 点击图标显示 MOUSE_ICON_DRAG_CLICK_DELAY 后切拖拽图标

_CLASS_NAME = "JccMouseIconOverlay"

_lock = threading.Lock()
_thread = None      # 覆盖线程 (懒启动)
_ready = threading.Event()  # 窗口就绪或初始化失败
_hwnd = None        # 分层窗口句柄 (由覆盖线程赋值)
_broken = False     # 初始化/通知失败标记: 置 True 后所有调用静默跳过
_bitmaps = {}       # (套号, kind) -> (hbitmap, memdc, w, h), 由覆盖线程填充
_pending_drag = [0, 0]  # 拖拽图标切换时的最新锚点 (屏幕坐标), 仅覆盖线程读写
_icon_set = 0       # 当前套号 (覆盖线程读写; 调用线程只通过 _WM_SET_ICONS 改)
_pending_set = 0    # 调用线程已请求的套号 (避免每次动作都重发 SET 消息)
_target_hwnd = 0    # 最近一次显示请求的目标模拟器主窗口 (调用线程写, move 复用)
_sets_cache = (None, None)  # (模板库目录 mtime, 套列表): 素材增删即时生效, 又不用每次动作都扫盘
_warned_pair = set()        # 缺配对的素材文件只提示一次, 免得刷屏
_CLICK_RE = re.compile(r"^click_mouse(?:_(\d+))?$")


def _discover_sets(folder) -> list:
    """扫模板库: `click_mouse_<i>.png` 与同号的 `drag_mouse_<i>.png` 配套, 按下标升序.

    命名规范见用户 m00303: 第 <i> 套给雷电实例 ID == <i> 的窗口 (window_<i>) 显示。
    无下标的 `click_mouse.png` 视为第 0 套 (兼容老素材); 只有一半配对的整套跳过。
    """
    found = {}
    for path in sorted(Path(folder).glob(str(getattr(default_config, "MOUSE_ICON_GLOB",
                                                     "click_mouse*.png")))):
        match = _CLICK_RE.match(path.stem)
        if match is None:
            continue
        idx = int(match.group(1) or 0)
        suffix = f"_{idx}" if match.group(1) else ""
        drag = path.with_name(f"drag_mouse{suffix}.png")
        if not drag.exists():
            if path.name not in _warned_pair:
                _warned_pair.add(path.name)
                print(f"[mouse_icon] 模板库里 {path.name} 缺配对的 {drag.name}, 该套跳过: {folder}")
            continue
        found[idx] = (path, drag)
    return [found[i] for i in sorted(found)]


def _icon_sets() -> list:
    """当前可用的卡通人物套列表 [(点击图, 拖拽图), ...].

    `config.MOUSE_ICON_SETS` 显式给了就用它 (测试/临时覆盖用); 为 None 时扫
    `config.MOUSE_ICON_DIR` (默认 images/icons), 目录 mtime 变了才重扫。
    """
    global _sets_cache
    explicit = getattr(default_config, "MOUSE_ICON_SETS", None)
    if explicit:
        return [tuple(pair) for pair in explicit]
    folder = Path(str(getattr(default_config, "MOUSE_ICON_DIR", "")))
    try:
        stamp = folder.stat().st_mtime
    except OSError:
        return []
    if _sets_cache[1] is not None and _sets_cache[0] == stamp:
        return _sets_cache[1]
    sets = _discover_sets(folder)
    _sets_cache = (stamp, sets)
    return sets


def _icon_set_count() -> int:
    """有多少套卡通人物 (至少按 1 算, 免得除零)."""
    return max(1, len(_icon_sets()))


def _set_paths(idx: int) -> tuple:
    """取第 idx 套的 (点击图, 拖拽图) 路径; 套号对套数取模."""
    sets = _icon_sets()
    if not sets:
        raise RuntimeError(
            f"模板库里没有成套的 click_mouse_<i>.png + drag_mouse_<i>.png: "
            f"{getattr(default_config, 'MOUSE_ICON_DIR', '')}")
    return sets[int(idx) % len(sets)]


def _mark_broken(reason: str) -> None:
    """置停用标记并打印一次 (装饰功能, 失败必须静默降级, 绝不抛回输入主流程)."""
    global _broken
    _broken = True
    print(reason)


def _fit_icon(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    """素材等比缩放后**底部居中**放进 size=(宽, 高) 画布 (人物不变形, 脚底贴齐下沿)."""
    w, h = int(size[0]), int(size[1])
    scale = min(w / im.width, h / im.height)
    new_w = max(1, min(w, int(im.width * scale)))
    new_h = max(1, min(h, int(im.height * scale)))
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(im.resize((new_w, new_h), Image.LANCZOS), ((w - new_w) // 2, h - new_h))
    return canvas


def _load_icon_bgra(path, size: tuple[int, int]) -> np.ndarray:
    """读取 PNG, 等比缩放到底部居中的 size 画布, 返回预乘 alpha 的 BGRA 像素.

    先缩放再做预乘 (小数组运算, 毫秒级); UpdateLayeredWindow 的 AC_SRC_ALPHA
    要求 RGB 分量预乘 alpha.
    """
    im = _fit_icon(Image.open(path).convert("RGBA"), size)
    arr = np.asarray(im).astype(np.uint16)
    arr[..., :3] = arr[..., :3] * arr[..., 3:4] // 255  # 预乘 alpha
    return arr[..., [2, 1, 0, 3]].astype(np.uint8)      # RGBA -> BGRA


def _make_bitmap(bgra: np.ndarray):
    """创建含像素数据的内存 DC + 位图 (UpdateLayeredWindow 的数据源)."""
    h, w = bgra.shape[:2]
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # 负高 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    hdc_screen = win32gui.GetDC(0)
    try:
        ppixels = ctypes.c_void_p()
        hbitmap = _gdi32.CreateDIBSection(
            hdc_screen, ctypes.byref(bmi), _DIB_RGB_COLORS,
            ctypes.byref(ppixels), None, 0)
        if not hbitmap or not ppixels:
            raise OSError(f"CreateDIBSection 失败 (GetLastError={ctypes.GetLastError()})")
        ctypes.memmove(ppixels, bgra.tobytes(), h * w * 4)  # 像素写入 DIB 内存
        memdc = win32gui.CreateCompatibleDC(hdc_screen)
        win32gui.SelectObject(memdc, hbitmap)
        return hbitmap, memdc, w, h
    finally:
        win32gui.ReleaseDC(0, hdc_screen)


def _load_set(store: dict, idx: int, size: tuple[int, int]) -> None:
    """把第 idx 套的两张图加载成位图放进 store (只由覆盖线程调用, 已加载则跳过)."""
    click_path, drag_path = _set_paths(idx)
    if (idx, "click") not in store:
        store[(idx, "click")] = _make_bitmap(_load_icon_bgra(str(click_path), size))
    if (idx, "drag") not in store:
        store[(idx, "drag")] = _make_bitmap(_load_icon_bgra(str(drag_path), size))


def _switch_icons(idx: int) -> None:
    """覆盖线程内切到第 idx 套卡通人物 (位图按需加载, 已加载则零成本)."""
    global _icon_set
    _icon_set = int(idx) % _icon_set_count()
    _load_set(_bitmaps, _icon_set, default_config.MOUSE_ICON_SIZE)


def _occluded(screen_x: float, screen_y: float, main_hwnd: int) -> bool:
    """点击位置是否被**别的窗口**盖住 (用户 m00064 第 2 条).

    判据: 该点最顶层窗口 (`WindowFromPoint`) 的顶层祖先 (`GA_ROOT`) 是不是目标模拟器主窗口。
    任何异常/判不出来都返回 False (照旧显示) —— 纯装饰功能, 宁可多显示也不漏显示。
    """
    try:
        hwnd = win32gui.WindowFromPoint((int(screen_x), int(screen_y)))
        if not hwnd:
            return True
        return win32gui.GetAncestor(hwnd, win32con.GA_ROOT) != int(main_hwnd)
    except Exception:  # noqa: BLE001
        return False


def _is_foreground(main_hwnd: int) -> bool:
    """目标模拟器窗口是不是当前**前台活动窗口** (用户 m00569 的"只能在前台展示").

    判据: `GetForegroundWindow()` 的顶层祖先 (GA_ROOT) 是不是目标模拟器主窗口 ——
    焦点落在该实例的任何子窗口上都算同一个实例。任何异常都返回 True (照旧显示):
    纯装饰功能, 宁可多显示也不漏显示。
    """
    try:
        fg = win32gui.GetForegroundWindow()
        if not fg:
            return False
        return win32gui.GetAncestor(fg, win32con.GA_ROOT) == int(main_hwnd)
    except Exception:  # noqa: BLE001
        return True


def _should_hide(screen_x: float, screen_y: float, main_hwnd) -> bool:
    """该位置上要不要**藏起**图标 (总闸 MOUSE_ICON_ONLY_FOREGROUND, 用户 m00569/m00572).

    - 没给目标窗口 / 总闸 False (当前默认): 前台与遮挡都不判, 任何时候都画;
    - 总闸 True: 目标窗口不在前台 -> 藏; 在前台再按 MOUSE_ICON_SKIP_OCCLUDED 判遮挡。
    """
    if not main_hwnd:
        return False
    if not getattr(default_config, "MOUSE_ICON_ONLY_FOREGROUND", False):
        return False
    if not _is_foreground(int(main_hwnd)):
        return True
    if getattr(default_config, "MOUSE_ICON_SKIP_OCCLUDED", True):
        return _occluded(screen_x, screen_y, int(main_hwnd))
    return False


def hide() -> None:
    """立刻隐藏图标窗口 (被遮挡/目标窗口切走时用); 任何失败静默忽略."""
    if _hwnd is None:
        return
    try:
        win32gui.ShowWindow(_hwnd, win32con.SW_HIDE)
    except Exception:  # noqa: BLE001 - 纯装饰, 失败就算了
        pass


def _place(hwnd, x: int, y: int, kind: str) -> None:
    """图标左下角对齐点击中心 (x, y), 再按 MOUSE_ICON_OFFSET 向右上偏移, 显示并重置计时."""
    hbitmap, memdc, w, h = _bitmaps[(_icon_set, kind)]
    off_x, off_y = default_config.MOUSE_ICON_OFFSET  # 实时读, 运行时改即时生效
    left, top = x + off_x, y - h - off_y
    hdc_screen = win32gui.GetDC(0)
    try:
        win32gui.UpdateLayeredWindow(
            hwnd, hdc_screen, (left, top), (w, h), memdc, (0, 0),
            0, (win32con.AC_SRC_OVER, 0, 255, win32con.AC_SRC_ALPHA),
            win32con.ULW_ALPHA)
    finally:
        win32gui.ReleaseDC(0, hdc_screen)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    # ULW 可能重置 z 序, 每次显示后重新置顶, 保证在模拟器画面之上
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                          | win32con.SWP_NOACTIVATE)
    seconds = (default_config.MOUSE_ICON_CLICK_SECONDS if kind == "click"
               else default_config.MOUSE_ICON_DRAG_SECONDS)
    _user32.SetTimer(hwnd, _WM_TIMER_ID, int(seconds * 1000), None)


def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == _WM_SET_ICONS:
        try:
            _switch_icons(wparam)   # 切换卡通人物: 位图未加载就现加载 (毫秒级)
        except Exception as exc:  # noqa: BLE001 - 装饰功能, 失败静默降级
            _mark_broken(f"[mouse_icon] 指示图标素材加载失败, 已停用: {exc}")
        return True
    if msg in (_WM_SHOW_CLICK, _WM_SHOW_DRAG):
        kind = "drag" if msg == _WM_SHOW_DRAG else "click"
        if (_icon_set, kind) not in _bitmaps:
            return True  # 位图未就绪: 丢弃本次显示, 绝不阻塞等待
        if msg == _WM_SHOW_DRAG:
            # 拖拽起点: 先显示点击图标 (按下瞬间), MOUSE_ICON_DRAG_CLICK_DELAY 后切拖拽图标
            _pending_drag[:] = [wparam, lparam]
            _place(hwnd, wparam, lparam, "click")
            delay = float(default_config.MOUSE_ICON_DRAG_CLICK_DELAY)  # 实时读, 即时生效
            if delay > 0:
                _user32.SetTimer(hwnd, _WM_TIMER_DRAG_ID, int(delay * 1000), None)
            else:
                _place(hwnd, wparam, lparam, "drag")
        else:
            _place(hwnd, wparam, lparam, kind)
    elif msg == _WM_MOVE:
        _pending_drag[:] = [wparam, lparam]  # 跟随移动同步更新切换锚点
        if (_icon_set, "drag") not in _bitmaps or not win32gui.IsWindowVisible(hwnd):
            return True
        _hbitmap, _memdc, w, h = _bitmaps[(_icon_set, "drag")]
        off_x, off_y = default_config.MOUSE_ICON_OFFSET  # 实时读, 即时生效
        win32gui.SetWindowPos(
            hwnd, 0, wparam + off_x, lparam - h - off_y, 0, 0,
            win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER)
        # 跟随中重置计时: 移动间隔 < DRAG_SECONDS 即可保证拖拽全程持续显示
        _user32.SetTimer(hwnd, _WM_TIMER_ID,
                         int(default_config.MOUSE_ICON_DRAG_SECONDS * 1000), None)
    elif msg == win32con.WM_TIMER and wparam == _WM_TIMER_ID:
        _user32.KillTimer(hwnd, _WM_TIMER_ID)
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
    elif msg == win32con.WM_TIMER and wparam == _WM_TIMER_DRAG_ID:
        _user32.KillTimer(hwnd, _WM_TIMER_DRAG_ID)
        _place(hwnd, *_pending_drag, "drag")  # 点击图标 -> 拖拽图标切换
    elif msg == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def _run(size: tuple[int, int]) -> None:
    """覆盖线程: 先建窗 (毫秒级, hwnd 立即可用) -> 加载第一套图标位图 -> 泵消息 (常驻)."""
    global _hwnd
    try:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = _CLASS_NAME
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpfnWndProc = _wnd_proc
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        win32gui.RegisterClass(wc)
        _hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT  # 逐像素透明 + 鼠标穿透
            | win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW
            | win32con.WS_EX_NOACTIVATE,
            _CLASS_NAME, "", win32con.WS_POPUP,
            0, 0, 0, 0, 0, 0, wc.hInstance, None)
        # 预热第 0 套 (导入即预热, 首次动作前通常已就绪); 其余套在 _WM_SET_ICONS 里按需加载。
        # 位图未就绪期间到达的显示通知由 _wnd_proc 静默跳过 (见 _WM_SHOW_* 分支)。
        _load_set(_bitmaps, 0, size)
        win32gui.PumpMessages()  # 常驻泵消息, 不再返回
    except Exception as exc:  # noqa: BLE001 - 纯装饰功能, 失败必须静默降级
        _mark_broken(f"[mouse_icon] 指示图标初始化失败, 已停用: {exc}")
    finally:
        _ready.set()


def _ensure_started() -> None:
    """懒启动覆盖线程, 纯 fire-and-forget: 绝不等待, 主流程零阻塞."""
    global _thread
    with _lock:
        if _thread is not None:
            return
        _thread = threading.Thread(
            target=_run, args=(default_config.MOUSE_ICON_SIZE,),
            daemon=True, name="mouse-icon-overlay")
        _thread.start()


def show(screen_x: float, screen_y: float, kind: str = "click", *,
         main_hwnd: int | None = None, ld_index: int | None = None) -> None:
    """在屏幕坐标处闪现指示图标 (图标左下角对齐该点, 按 OFFSET 右上偏移): kind="click"/"drag".

    Args:
        main_hwnd: 目标模拟器**主窗口**句柄。给了就交给 `_should_hide` 判定 ——
            config.MOUSE_ICON_ONLY_FOREGROUND=False (当前) 时前台/遮挡都不判, 照画;
            True 时"目标窗口不在前台"或"点击点被别的窗口盖住"都不显示 (用户 m00569 / m00064)。
        ld_index: 该实例的**雷电 ID**, 用来选卡通人物 (对 MOUSE_ICON_SETS 取模, 用户 m00064
            第 1 条: 多开时两个窗口显示不同人物); 不给则沿用当前套。

    kind="drag" 时先显示点击图标, MOUSE_ICON_DRAG_CLICK_DELAY 秒后切换为拖拽图标
    (切换由覆盖线程定时器完成, 主流程零阻塞)。任何失败都静默停用自身, 不影响输入
    主流程; MOUSE_ICON_SHOW=False 时为空操作。
    """
    global _broken, _pending_set, _target_hwnd
    if _broken or not default_config.MOUSE_ICON_SHOW:
        return
    try:
        if main_hwnd:
            _target_hwnd = int(main_hwnd)
        if _should_hide(screen_x, screen_y, main_hwnd):
            hide()  # 只能在前台展示时这里会命中: 不显示, 顺手把上一次的图标收掉
            return
        _ensure_started()
        if _broken or _hwnd is None:
            return
        if ld_index is not None:
            idx = int(ld_index) % _icon_set_count()
            if idx != _pending_set:  # 同一个进程只服务一个实例, 通常只在首次动作时发一次
                _pending_set = idx
                win32api.PostMessage(_hwnd, _WM_SET_ICONS, idx, 0)
        msg = _WM_SHOW_DRAG if kind == "drag" else _WM_SHOW_CLICK
        win32api.PostMessage(_hwnd, msg, int(screen_x), int(screen_y))
    except Exception as exc:  # noqa: BLE001
        _mark_broken(f"[mouse_icon] 指示图标通知失败, 已停用: {exc}")


def move(screen_x: float, screen_y: float) -> None:
    """拖拽跟随: 图标左下角 (+OFFSET) 对齐移动到屏幕坐标处并重置隐藏计时.

    按 `_should_hide` 判定: MOUSE_ICON_ONLY_FOREGROUND=True 时目标实例切走、或该处被
    别的窗口盖住, 就直接隐藏 (拖拽途中用户切了窗口就别再画了); 当前为 False, 照旧跟随。
    """
    if _broken or _hwnd is None:
        return
    if _should_hide(screen_x, screen_y, _target_hwnd):
        hide()
        return
    try:
        win32api.PostMessage(_hwnd, _WM_MOVE, int(screen_x), int(screen_y))
    except Exception:  # noqa: BLE001 - 纯装饰, 任何失败都忽略
        pass


if default_config.MOUSE_ICON_SHOW:
    _ensure_started()  # 导入即预热: 首次动作前窗口通常已就绪, 主流程零阻塞
