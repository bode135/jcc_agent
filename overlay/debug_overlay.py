"""实时调试绘制层 (overlay): 把识别结果实时画在雷电模拟器画面上.

纯外部 Win32 分层透明窗口 —— 与 `overlay/mouse_icon.py` 同一套写法, **不注入不 hook**:
    - 贴**雷电 RenderWindow 客户区** (1920x1080 基准), 不是主窗口: 主窗口客户区含右侧工具栏,
      直接贴会整体错位 (见 `capture.window.find_render_window`)。
    - 样式 `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`:
      逐像素透明 + 鼠标穿透 + 置顶 + 不抢焦点 + 不进任务栏。
    - 每帧用 PIL 生成 RGBA 画布 -> 预乘 alpha -> `UpdateLayeredWindow(ULW_ALPHA)` 原子提交 (不闪)。
    - 自动 hide (用户 m00172 第 1 条): 目标窗口最小化 / 不可见 / 被别的窗口整体盖住 -> 收起;
      恢复可见后自动重新贴回。窗口线程内定时器每 `OVERLAY_WATCH_INTERVAL` 秒巡检一次。
    - 绘制内容的开关集中在 `config.OVERLAY_*` (见 config/config.py 对应小节)。

分工:
    - 本模块只负责 **窗口 + 贴图 + 自动 hide**; 识别取数与线程调度在 `overlay/live_draw.py`。
    - `draw_frame()` 是纯函数 (尺寸 + 各识别结果 -> PIL RGBA 画布), 不依赖窗口, 可离线单测。
    - 人工看效果: `python -m overlay.debug_overlay --ld-index 0 --seconds 10` (画示例框, 不动游戏)。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

import numpy as np
import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw

from config.config import default_config

ctypes.windll.user32.SetProcessDPIAware()  # 物理像素, 与 capture.window 保持一致

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
                             ctypes.c_void_p]
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]

# 线程间通知消息
_WM_SET_FRAME = win32con.WM_APP + 1   # 新的一帧 RGBA 到了 (payload 放 _pending)
_WM_HIDE = win32con.WM_APP + 2        # 立刻收起
_WM_TIMER_WATCH = 1                   # 可见性/遮挡巡检计时器

_CLASS_NAME = "JccDebugOverlay"

# 窗口线程状态 (照 mouse_icon 的模块级全局写法)
_lock = threading.Lock()
_thread: threading.Thread | None = None
_thread_id: int | None = None
_ready = threading.Event()
_hwnd: int | None = None
_broken = False
_stopping = False
_warned: set = set()

_target: dict | None = None            # {"main","render","left","top","w","h"}
_ld_index = None                       # 目标实例 ID (None = 自动第一个可用)
_pending: dict = {"rgba": None}        # 待提交的一帧 (PIL RGBA)

# 位图缓存: 尺寸不变就复用同一块 DIB (整帧 1920x1080x4 = 8MB, 每帧新建必然撑爆 GDI)
_bitmap = {"hbitmap": None, "memdc": None, "ppixels": None, "w": 0, "h": 0}


# --------------------------------------------------------------------------- #
# 目标窗口定位 (纯函数, 可离线测试)
# --------------------------------------------------------------------------- #
def resolve_target(ld_index=None) -> dict | None:
    """定位目标雷电实例的 RenderWindow 客户区, 返回 {main, render, left, top, w, h}.

    `ld_index=None` 时取第一个可用实例 (单开场景)。找不到可用实例 / RenderWindow 时返回 None。
    """
    from capture import window as win

    try:
        insts = win.list_instances(usable_only=True)
    except Exception as exc:  # noqa: BLE001
        _warn_once(f"[overlay] 枚举雷电实例失败: {exc!r}")
        return None
    if not insts:
        return None
    inst = None
    if ld_index is None:
        inst = insts[0]
    else:
        for it in insts:
            if int(it.get("ld_index", -1)) == int(ld_index):
                inst = it
                break
        if inst is None:
            inst = insts[0]
    main_hwnd = int(inst.get("hwnd") or 0)
    if not main_hwnd:
        return None
    try:
        render = int(win.find_render_window(main_hwnd) or 0)
    except Exception:  # noqa: BLE001
        render = 0
    if not render:
        return None
    return _client_rect(main_hwnd, render)


def _client_rect(main_hwnd: int, render_hwnd: int) -> dict | None:
    """取 RenderWindow 客户区在屏幕上的位置与尺寸 (左上角 + 宽高)。"""
    try:
        left, top, right, bottom = win32gui.GetClientRect(render_hwnd)
        w, h = int(right - left), int(bottom - top)
        if w <= 0 or h <= 0:
            return None
        sx, sy = win32gui.ClientToScreen(render_hwnd, (0, 0))
    except Exception:  # noqa: BLE001
        return None
    return {"main": int(main_hwnd), "render": int(render_hwnd),
            "left": int(sx), "top": int(sy), "w": w, "h": h}


def _warn_once(msg: str) -> None:
    if msg in _warned:
        return
    _warned.add(msg)
    print(msg)


# --------------------------------------------------------------------------- #
# 自动 hide: 可见性 / 遮挡判定 (纯逻辑, 可离线测试)
# --------------------------------------------------------------------------- #
def sample_points(left: int, top: int, w: int, h: int, n: int = 3) -> list[tuple[int, int]]:
    """在客户区里取 n×n 个采样点 (屏幕坐标), 用四边内缩 10% 的网格避开窗口圆角/边框。"""
    n = max(1, int(n))
    pts: list[tuple[int, int]] = []
    for i in range(n):
        fx = (i + 0.5) / n
        for j in range(n):
            fy = (j + 0.5) / n
            pts.append((int(left + w * fx), int(top + h * fy)))
    return pts


def _point_covered(screen_x: int, screen_y: int, main_hwnd: int) -> bool:
    """该屏幕点最顶层窗口的顶层祖先是不是目标实例 (不是 = 被别的窗口盖住)."""
    try:
        hwnd = win32gui.WindowFromPoint((int(screen_x), int(screen_y)))
        if not hwnd:
            return True
        return win32gui.GetAncestor(hwnd, win32con.GA_ROOT) != int(main_hwnd)
    except Exception:  # noqa: BLE001 - 判不出来就当没盖住 (宁可多画)
        return False


def should_hide(target: dict | None, *, occluded: bool | None = None) -> bool:
    """目标当前要不要收起. `occluded` 可注入 (供单测); None 时现场采样判定.

    - target 为空 / 句柄失效 / 窗口不可见 / 最小化 -> 收;
    - OVERLAY_HIDE_WHEN_OCCLUDED=True 且**整体被盖住** (采样点几乎全被别的窗口占) -> 收.
    """
    if not target:
        return True
    main = int(target.get("main") or 0)
    render = int(target.get("render") or 0)
    if not main or not render:
        return True
    try:
        if not win32gui.IsWindow(main) or not win32gui.IsWindow(render):
            return True
        if not win32gui.IsWindowVisible(render):
            return True
        if win32gui.IsIconic(main):
            return True
    except Exception:  # noqa: BLE001
        return True
    if occluded is None:
        occluded = _is_occluded(target)
    if occluded and getattr(default_config, "OVERLAY_HIDE_WHEN_OCCLUDED", True):
        return True
    return False


def _is_occluded(target: dict) -> bool:
    """整体被盖住: 3×3 采样里 >= 8 个点被别的窗口占据 (留 1 个点容错圆角/边缘)。"""
    pts = sample_points(target["left"], target["top"], target["w"], target["h"], 3)
    covered = sum(1 for (x, y) in pts if _point_covered(x, y, int(target["main"])))
    return covered >= max(1, len(pts) - 1)


# --------------------------------------------------------------------------- #
# 绘制 (纯函数: 尺寸 + 识别结果 -> PIL RGBA 画布, 不依赖窗口)
# --------------------------------------------------------------------------- #
# 颜色口径: 棋子蓝 / 血条深灰 / 商店橙 / 装备栏青绿 / 携带装备品红 / 球浅蓝 / 小小英雄紫。
_C_YOLO = (0, 100, 255);      _T_YOLO = (200, 200, 200)      # 蓝框 + 浅灰字 (棋子)
_C_BAR = (64, 64, 64);        _T_BAR = (255, 240, 150)       # 深灰框 + 浅黄字 (血条)
_C_BALL_Y = (110, 185, 255);  _T_BALL_Y = (150, 210, 255)    # balls-yolo: 浅蓝框 + 浅蓝字
_C_BALL_C = (90, 200, 255);   _T_BALL_C = (170, 225, 255)    # balls-color: 浅蓝偏青框 + 浅蓝字
_C_LEGEND = (170, 0, 200);    _T_LEGEND = (235, 180, 255)    # 紫框 + 浅紫字 (小小英雄)
_C_SHOP = (255, 140, 0);      _T_SHOP = (255, 215, 150)      # 橙框 + 浅橙字
_C_EQUIP = (0, 180, 160);     _T_EQUIP = (170, 235, 225)     # 青绿框 + 浅青字
_C_BEQ = (230, 50, 150);      _T_BEQ = (255, 190, 220)       # 品红框 + 浅粉字
_C_STAR = (255, 200, 40)      # 星级徽记: 金黄框 + 金星 (血条左侧那枚锚点)
_C_STAR_TXT = (255, 235, 120) # 星级文字: 更亮的金黄, 压在实机上也能读
_C_HERO_M = (60, 255, 120)    # 小小英雄(我方): 亮绿框 (用户 m01362/m01368: 右侧小血条绿=我方)
_T_HERO_M = (190, 255, 205)
_C_HERO_E = (255, 80, 80)     # 小小英雄(敌方): 亮红框 (红=敌方)
_T_HERO_E = (255, 195, 195)
_C_HEAD = (235, 235, 235)     # 顶部状态行 (白字黑描边, 压在实机画面上也能读)

_FONT_CACHE: dict = {}


def _font(size: int = 14):
    f = _FONT_CACHE.get(size)
    if f is None:
        from utils.show_img import load_cjk_font
        f = load_cjk_font(size, bold=True)
        _FONT_CACHE[size] = f
    return f


def draw_frame(size, *, units=None, bars=None, balls=None, legends=None,
               shop=None, equip_bar=None, board_equips=None,
               balls_yolo=None, balls_color=None, heroes=None, show=None,
               header: str | None = None) -> Image.Image:
    """把各识别结果画到一张**全透明**的 size 画布上, 返回 PIL RGBA 图.

    纯图色方案: 入参全是"识别结果", 不读游戏进程内存, 本函数也不碰窗口 (可离线单测)。

    Args:
        size: (宽, 高), 应与目标客户区一致 (1920x1080 基准)。
        units: `recognize.yolo_units.extract_units` 返回 {"board": [], "bench": []}; 单元里若有
               `stars` (int, 由血条条纹给出的星数) 且 >= 2, 会以 "2★ 名字" 形式并在名字牌前缀
               (1★ 默认不显示, 用户 m02211: 并进名字牌不占额外行数)。
        bars:  `recognize.board.extract_units` 返回 {"board": [], "bench": []} (血条)
        balls_yolo: YOLO(球权重) 检出的球 list, 每项 {box, name}; legends 同源 (小小英雄)
        balls_color: `reward_ball.find_balls()` 结果, 每项 {rect, color}
        shop: `recognize.extract.extract_shop` 返回的 5 槽 list
        equip_bar: `recognize.equipment.extract_equipment` 返回的格子 list
        board_equips: `recognize.equipment.extract_board_equips` 返回的 list
        show: 覆盖绘制开关的 dict (键名同 config.OVERLAY_SHOW_*, 供单测/临时覆盖); None 读 config.
        header: 顶部状态行文字; None 时自动按画了几个类生成。
    """
    w, h = int(size[0]), int(size[1])
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pen = ImageDraw.Draw(canvas)
    font = _font()
    font14 = font
    font_star = _font(20)        # 英雄/状态牌用大一号字, 实机上才看得清
    font_cell = _font(12)        # 格位标签 (r1c4 / -r2c1 / slot3) 用小的, 别糊住棋子

    def _overlap(a, b) -> bool:
        """两个矩形是否相交 (坐标 [x1,y1,x2,y2]); 用来去重同一只英雄画两遍。"""
        try:
            ax1, ay1, ax2, ay2 = (float(v) for v in a)
            bx1, by1, bx2, by2 = (float(v) for v in b)
        except Exception:  # noqa: BLE001
            return False
        return not (ax2 < bx1 or bx2 < ax1 or ay2 < by1 or by2 < ay1)

    def _on(key: str) -> bool:
        if show is not None and key in show:
            return bool(show[key])
        return bool(getattr(default_config, key, False))

    def _rgba(c):
        """颜色统一补 alpha=255 (RGBA 画布上 3 元组在部分 Pillow 版本会报错)。"""
        c = tuple(c)
        return c if len(c) == 4 else (c[0], c[1], c[2], 255)

    def _stroke(box, color, label: str = "", below: bool = False, *,
                text_color=None, width: int = 3, label_y=None, font=None,
                plate: bool = False, stroke_width: int = 2) -> int:
        if not box:
            return 0
        try:
            x1, y1, x2, y2 = (int(round(float(v))) for v in box[:4])
        except Exception:  # noqa: BLE001
            return 0
        pen.rectangle([x1, y1, x2, y2], outline=_rgba(color), width=width)
        if label:
            _f = font or font14
            if label_y is not None:      # 显式指定文字行 (内存 GT 要让到第二行, 用户 m00916)
                ty = int(label_y)
            else:
                ty = y2 + 1 if below else (y1 - 17 if y1 >= 17 else y1 + 1)
            tx = max(0, x1)
            if plate:
                # 半透明黑底牌: 星级要"一眼看得清", 压在花花绿绿的实机画面上纯描边字会糊
                try:
                    bx1, by1, bx2, by2 = pen.textbbox((tx, ty), label, font=_f,
                                                      stroke_width=stroke_width)
                    pen.rectangle([bx1 - 2, by1 - 2, bx2 + 2, by2 + 2],
                                  fill=(0, 0, 0, 210))
                except Exception:  # noqa: BLE001
                    pass
            ty = max(0, min(ty, h - 4))
            pen.text((tx, ty), label, fill=_rgba(text_color or color), font=_f,
                     stroke_width=stroke_width, stroke_fill=(0, 0, 0, 255))
        return 1

    n_units = n_shop = n_equip = n_beq = n_bar = n_byolo = n_bcolor = n_badge = 0
    n_hero = 0
    # 英雄框位置先算出来: 后面的 YOLO legend 紫框与英雄框重叠时不再重复画
    # (两条路指同一只英雄, 画两遍只会糊住判据)。
    _hero_boxes = []
    _hero_pts = []      # 英雄落点 (等级块中心 + DX/DY): YOLO legend 的紫框和它撞上就是同一只
    for _hp in (heroes or []):
        _nb = _hp.get("numblk")
        if _nb:
            _x, _y, _w, _h = (int(v) for v in _nb)
            _hero_boxes.append([_x - 6, _y - 6, _x + _w + 8, _y + _h + 6])
        if _hp.get("x") is not None and _hp.get("y") is not None:
            _hero_pts.append((float(_hp["x"]), float(_hp["y"])))

    if _on("OVERLAY_SHOW_UNITS"):
        for area in ("board", "bench"):
            for u in (units or {}).get(area) or []:
                # 星级并进名字牌 (用户 m02211: "改为 2★ 3★ 之类, 就不占行数了, 1★ 默认不显示")。
                # 原来单独一块 ★×N 字牌会被棋盘同列 76px 行距挤进上一格棋子的框里,
                # 并进名字牌后信息不减、也不再多占一行垂直空间。
                label = u.get("name") or u.get("model_name") or "?"
                _st = u.get("stars")
                if _st and int(_st) >= 2:
                    label = f"{int(_st)}★ " + label
                _s = u.get("score")
                if _s is not None:
                    label += f" {float(_s):.2f}"
                n_units += _stroke(u.get("box"), _C_YOLO, label, below=True, text_color=_T_YOLO)
                # ---- 星级徽记锚点 (用户 m01029: 星级得能一眼看出对错) ----
                # 血条左侧那枚徽记是星级的物理来源, 只描框不写字:
                # 一旦"框对了但星数错", 一眼就能定位是徽记判据坏了还是血条配错了格。
                bb = u.get("badge_box")
                if bb and _on("OVERLAY_SHOW_STAR_BADGE"):
                    n_badge += _stroke(bb, _C_STAR, "", width=2)

    if _on("OVERLAY_SHOW_BLOOD_BAR"):
        for area in ("board", "bench"):
            for u in (bars or {}).get(area) or []:
                # 血条只画框不写字: 星级由左侧徽记的 ★ 牌说明, "血条"三个字会压住那枚牌
                n_bar += _stroke(u.get("bar"), _C_BAR, "", width=2)

    _by = balls_yolo if balls_yolo is not None else balls
    if _on("OVERLAY_SHOW_BALLS_YOLO"):
        for b in _by or []:
            n_byolo += _stroke(b.get("box"), _C_BALL_Y, b.get("name") or "ball",
                               below=True, text_color=_T_BALL_Y, width=2)
        for lg in legends or []:
            lb = lg.get("box")
            if not lb:
                continue
            if any(_overlap(lb, _hb) for _hb in _hero_boxes):
                continue        # 已由英雄路画过 (等级块 + 红/绿小血段)
            lcx, lcy = (lb[0] + lb[2]) / 2.0, (lb[1] + lb[3]) / 2.0
            if any(((lcx - px) ** 2 + (lcy - py) ** 2) ** 0.5 < 70.0
                   for px, py in _hero_pts):
                continue        # 框中心离英雄落点很近 = 同一只, 交给英雄路画
            n_byolo += _stroke(lb, _C_LEGEND, lg.get("name") or "小小英雄",
                               below=True, text_color=_T_LEGEND)

    if _on("OVERLAY_SHOW_BALLS_COLOR"):
        _cname = {"blue": "蓝", "silver": "银", "gold": "金"}
        for b in balls_color or []:
            col = b.get("color")
            n_bcolor += _stroke(b.get("rect"), _C_BALL_C, f"球-{_cname.get(col, col or '?')}",
                                below=True, text_color=_T_BALL_C, width=2)

    if _on("OVERLAY_SHOW_SHOP"):
        for slot in shop or []:
            name, price = slot.get("name"), slot.get("price")
            if not name and price is None:
                continue
            label = name or "?"
            if price is not None:
                label += f" {price}"
            n_shop += _stroke(slot.get("rect"), _C_SHOP, label, text_color=_T_SHOP, width=2)

    if _on("OVERLAY_SHOW_EQUIP_BAR"):
        for it in equip_bar or []:
            name = it.get("name")
            if not name:
                continue
            cnt = it.get("count")
            label = f"{name}x{cnt}" if cnt and cnt > 1 else name
            n_equip += _stroke(it.get("rect"), _C_EQUIP, label, text_color=_T_EQUIP, width=2)

    if _on("OVERLAY_SHOW_BOARD_EQUIPS"):
        for grp in board_equips or []:
            for eq in grp.get("equips") or []:
                label = eq.get("name") or eq.get("equipId") or "?"
                box = eq.get("rect")
                if box:
                    n_beq += _stroke(box, _C_BEQ, label, below=True, text_color=_T_BEQ,
                                    width=2)
                    continue
                # 只有中心点 (没有 rect) 时画个小圈 + 文字, 不能让 breakpoint 走 _stroke 的空 box。
                c = eq.get("center")
                if not c:
                    continue
                cx, cy, r = int(c[0]), int(c[1]), 12
                pen.ellipse([cx - r, cy - r, cx + r, cy + r], outline=_rgba(_C_BEQ),
                            width=2)
                pen.text((cx - r, cy + r + 1), label, fill=_rgba(_T_BEQ), font=font,
                         stroke_width=2, stroke_fill=(0, 0, 0, 255))
                n_beq += 1

    if _on("OVERLAY_SHOW_HERO"):
        # 小小英雄 (用户 m01362): 等级数字块 + 右侧红/绿小血段 -> 绿=我方, 红=敌方.
        # 框画在等级数字块上 (那是判据的锚点), 落点位置再点一个小圈, 方便核对
        # "这个数字块确实属于这只英雄"。
        for hp in heroes or []:
            nb = hp.get("numblk")
            if not nb:
                continue
            # 变量名别用 x/y/w/h: draw_frame 顶上的 w,h 是画布尺寸, 被同名覆盖会把
            # 标签的 ty 钳到 h-4 (=块高-4) 上, 文字跑到画布左上角 (实测踩过)。
            bx, by, bw, bh = (int(v) for v in nb)
            box = [bx - 6, by - 6, bx + bw + 8, by + bh + 6]
            mine = (hp.get("kind") == "mine")
            col = _C_HERO_M if mine else _C_HERO_E
            txt = _T_HERO_M if mine else _T_HERO_E
            who = "我方" if mine else "敌方"
            lv = hp.get("level")
            # 标签只留"我方 Lv5"这种短串 (绿/红框本身已经说明敌我): 太长会糊住血条牌
            label = who + (f" Lv{lv}" if lv is not None else "")
            n_hero += _stroke(box, col, f"小小英雄 {label}", below=True, text_color=txt,
                              width=3, font=_font(17), plate=True, stroke_width=1)
            px, py = hp.get("x"), hp.get("y")
            if px is not None and py is not None:
                r = 7
                pen.ellipse([px - r, py - r, px + r, py + r], outline=_rgba(col), width=2)
                n_hero += 1

    if header is None:
        header = (f"[overlay] 棋子 {n_units} · 商店 {n_shop} · 装备栏 {n_equip} "
                  f"· 携带装备 {n_beq}")
        if _on("OVERLAY_SHOW_BLOOD_BAR"):
            header += f" · 血条 {n_bar}"
        if _on("OVERLAY_SHOW_BALLS_YOLO"):
            header += f" · 球-y {n_byolo}"
        if _on("OVERLAY_SHOW_BALLS_COLOR"):
            header += f" · 球-色 {n_bcolor}"
        if _on("OVERLAY_SHOW_HERO"):
            header += f" · 英雄 {n_hero}"
    if header:
        pen.text((10, 8), header, fill=_rgba(_C_HEAD), font=font,
                 stroke_width=2, stroke_fill=(0, 0, 0, 255))
    return canvas


# --------------------------------------------------------------------------- #
# 位图 (复用 mouse_icon 的 DIB 写法, 但整帧复用同一块内存)
# --------------------------------------------------------------------------- #
def _rgba_to_bgra_premultiplied(im: Image.Image) -> np.ndarray:
    """PIL RGBA -> 预乘 alpha 的 BGRA uint8 (UpdateLayeredWindow AC_SRC_ALPHA 要求)。"""
    arr = np.asarray(im.convert("RGBA")).astype(np.uint16)
    arr[..., :3] = arr[..., :3] * arr[..., 3:4] // 255
    return arr[..., [2, 1, 0, 3]].astype(np.uint8)


def _free_bitmap() -> None:
    for key, deleter in (("hbitmap", win32gui.DeleteObject), ("memdc", win32gui.DeleteDC)):
        h = _bitmap.get(key)
        if h:
            try:
                deleter(h)
            except Exception:  # noqa: BLE001
                pass
            _bitmap[key] = None
    _bitmap["ppixels"] = None
    _bitmap["w"] = _bitmap["h"] = 0


def _ensure_bitmap(w: int, h: int, bgra: np.ndarray):
    """尺寸不变则复用同一块 DIB (memmove 覆盖像素), 变了则重建。返回 (hbitmap, memdc)。"""
    if _bitmap["hbitmap"] and _bitmap["w"] == w and _bitmap["h"] == h:
        ctypes.memmove(_bitmap["ppixels"], bgra.tobytes(), w * h * 4)
        return _bitmap["hbitmap"], _bitmap["memdc"]
    _free_bitmap()
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # 负高 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    hdc_screen = win32gui.GetDC(0)
    try:
        ppixels = ctypes.c_void_p()
        hbitmap = _gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), _DIB_RGB_COLORS,
                                          ctypes.byref(ppixels), None, 0)
        if not hbitmap or not ppixels:
            raise OSError(f"CreateDIBSection 失败 (GetLastError={ctypes.GetLastError()})")
        ctypes.memmove(ppixels, bgra.tobytes(), w * h * 4)
        memdc = win32gui.CreateCompatibleDC(hdc_screen)
        win32gui.SelectObject(memdc, hbitmap)
    finally:
        win32gui.ReleaseDC(0, hdc_screen)
    _bitmap.update({"hbitmap": hbitmap, "memdc": memdc, "ppixels": ppixels, "w": w, "h": h})
    return hbitmap, memdc


def _commit(hwnd, target: dict, frame: Image.Image) -> None:
    """把 frame 提交到分层窗口: 尺寸不符先缩放, 再 UpdateLayeredWindow 原子贴上去。"""
    w, h = int(target["w"]), int(target["h"])
    if frame.size != (w, h):
        frame = frame.resize((w, h), Image.LANCZOS)
    bgra = _rgba_to_bgra_premultiplied(frame)
    hbitmap, memdc = _ensure_bitmap(w, h, bgra)
    hdc_screen = win32gui.GetDC(0)
    try:
        win32gui.UpdateLayeredWindow(
            hwnd, hdc_screen, (int(target["left"]), int(target["top"])), (w, h),
            memdc, (0, 0), 0,
            (win32con.AC_SRC_OVER, 0, 255, win32con.AC_SRC_ALPHA), win32con.ULW_ALPHA)
    finally:
        win32gui.ReleaseDC(0, hdc_screen)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    # ULW 可能重置 z 序, 每次显示后重新置顶, 保证在模拟器画面之上
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)


# --------------------------------------------------------------------------- #
# 窗口线程
# --------------------------------------------------------------------------- #
def _mark_broken(reason: str) -> None:
    global _broken
    _broken = True
    print(reason)


def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == _WM_SET_FRAME:
        frame = _pending.get("rgba")
        if frame is None or _broken:
            return True
        # 每帧先刷新一次目标 (窗口可能被拖动/改尺寸), 不可见就收起
        _refresh_target()
        if should_hide(_target):
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            return True
        try:
            _commit(hwnd, _target, frame)
        except Exception as exc:  # noqa: BLE001 - 装饰功能, 单帧失败忽略
            _warn_once(f"[overlay] 提交一帧失败(忽略): {exc!r}")
        return True
    if msg == _WM_HIDE:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        except Exception:  # noqa: BLE001
            pass
        return True
    if msg == win32con.WM_TIMER and wparam == _WM_TIMER_WATCH:
        # 自动 hide 巡检: 最小化/不可见/被整体盖住 -> 隐藏; 恢复可见后等下一次 SET_FRAME 重贴
        _refresh_target()
        try:
            if should_hide(_target):
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        except Exception:  # noqa: BLE001
            pass
        return True
    if msg == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


def _refresh_target() -> None:
    """刷新目标客户区 (位置/尺寸); 句柄失效则尝试按 ld_index 重新定位。"""
    global _target
    t = _target
    if t and win32gui.IsWindow(t["main"]) and win32gui.IsWindow(t["render"]):
        rect = _client_rect(t["main"], t["render"])
        if rect:
            _target = rect
            return
        return
    _target = resolve_target(_ld_index)


def _run() -> None:
    global _hwnd, _thread_id
    try:
        _thread_id = win32api.GetCurrentThreadId()
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = _CLASS_NAME
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpfnWndProc = _wnd_proc
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        try:
            win32gui.RegisterClass(wc)
        except Exception as exc:  # noqa: BLE001 - 类已注册 (重启用) 时忽略, 其余照抛
            if "already" not in str(exc).lower() and "1410" not in str(exc):
                raise
        _hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT
            | win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE,
            _CLASS_NAME, "", win32con.WS_POPUP, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        if not _hwnd:
            raise OSError(f"CreateWindowEx 失败 (GetLastError={ctypes.GetLastError()})")
        _ready.set()  # 窗口已建好, 立刻放行 start() (不能等到消息泵退出才 set)
        interval_ms = max(100, int(float(getattr(default_config, "OVERLAY_WATCH_INTERVAL", 0.5)) * 1000))
        _user32.SetTimer(_hwnd, _WM_TIMER_WATCH, interval_ms, None)
        win32gui.PumpMessages()  # 常驻泵消息, 直到 WM_QUIT
    except Exception as exc:  # noqa: BLE001 - 装饰功能, 失败必须静默降级
        _mark_broken(f"[overlay] 调试绘制层初始化失败, 已停用: {exc}")
    finally:
        _ready.set()


def start(ld_index=None) -> bool:
    """启动调试绘制层 (懒启动, 幂等)。返回是否可用; 失败静默降级为不可用。"""
    global _thread, _ld_index, _target, _stopping
    if _broken:
        return False
    with _lock:
        _ld_index = ld_index
        if _thread is not None and _thread.is_alive():
            return True
        _target = resolve_target(ld_index)
        if not _target:
            _warn_once("[overlay] 未找到可用的雷电 RenderWindow, 调试绘制层不启动")
            return False
        _stopping = False
        _ready.clear()
        _thread = threading.Thread(target=_run, daemon=True, name="debug-overlay")
        _thread.start()
    _ready.wait(timeout=3.0)
    return bool(_hwnd) and not _broken


def set_frame(frame: Image.Image) -> bool:
    """提交一帧 PIL RGBA 画布 (由绘制线程调用)。窗口不可用/已停用则丢弃。"""
    if _broken or _hwnd is None or frame is None:
        return False
    _pending["rgba"] = frame
    try:
        win32api.PostMessage(_hwnd, _WM_SET_FRAME, 0, 0)
        return True
    except Exception as exc:  # noqa: BLE001
        _mark_broken(f"[overlay] 提交帧通知失败, 已停用: {exc}")
        return False


def hide() -> None:
    if _hwnd is None:
        return
    try:
        win32api.PostMessage(_hwnd, _WM_HIDE, 0, 0)
    except Exception:  # noqa: BLE001
        pass


def stop(wait: float = 2.0) -> None:
    """停止窗口线程 (收起窗口、释放位图、退出消息泵)。"""
    global _stopping, _thread, _hwnd, _thread_id
    with _lock:
        if _thread is None:
            return
        _stopping = True
        tid = _thread_id
        th = _thread
    try:
        if _hwnd is not None:
            win32gui.ShowWindow(_hwnd, win32con.SW_HIDE)
        if tid:
            win32api.PostThreadMessage(tid, win32con.WM_QUIT, 0, 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        if th is not None:
            th.join(timeout=wait)
    except Exception:  # noqa: BLE001
        pass
    _free_bitmap()
    with _lock:
        _thread = None
        _hwnd = None
        _thread_id = None
        _pending["rgba"] = None


def is_active() -> bool:
    """窗口线程是否就绪可用 (供单测/日志)。"""
    return bool(_hwnd) and not _broken


# --------------------------------------------------------------------------- #
# 人工看效果 (不动游戏, 只画示例框)
# --------------------------------------------------------------------------- #
def _demo(ld_index=None, seconds: float = 10.0) -> int:
    import random
    if not start(ld_index):
        print("[overlay] 启动失败: 没找到雷电 RenderWindow")
        return 1
    tgt = resolve_target(ld_index)
    if not tgt:
        print("[overlay] 定位失败")
        return 1
    print(f"[overlay] 目标 render={tgt['render']} 客户区 {tgt['w']}x{tgt['h']} "
          f"屏幕原点 ({tgt['left']},{tgt['top']})", flush=True)
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            sz = (tgt["w"], tgt["h"])
            rnd = lambda: [random.randint(80, sz[0] - 260), random.randint(80, sz[1] - 200),
                           random.randint(140, 340), random.randint(60, 160)]
            def box():
                x, y, bw, bh = rnd()
                return [x, y, x + bw, y + bh]
            frame = draw_frame(
                sz,
                units={"board": [{"box": box(), "model_name": "阿狸", "name": "阿狸",
                                  "score": 0.91}],
                       "bench": [{"box": box(), "model_name": "unknown", "name": None,
                                  "score": 0.42}]},
                shop=[{"rect": (485 + i * 233, 902, 485 + i * 233 + 230, 1053),
                       "name": f"示例卡{i + 1}", "price": 3} for i in range(5)],
                equip_bar=[{"rect": (60 + i * 70, 470, 110 + i * 70, 520),
                            "name": "暴风大剑", "count": 2} for i in range(4)],
                board_equips=[{"equips": [{"rect": [900, 380, 926, 406], "name": "巨人腰带"}]}],
                balls_yolo=[{"box": box(), "name": "ball"}],
                balls_color=[{"rect": box(), "color": "blue"}],
                show={"OVERLAY_SHOW_BALLS_COLOR": True},
                header="[overlay demo] 示例: 棋子(带星级)/血条/商店/装备栏/携带装备/球",
            )
            set_frame(frame)
            time.sleep(0.5)
    finally:
        stop()
    print("[overlay] demo 结束")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="调试绘制层人工验证: 画示例框, 不动游戏")
    ap.add_argument("--ld-index", type=int, default=None)
    ap.add_argument("--seconds", type=float, default=10.0)
    _a = ap.parse_args()
    raise SystemExit(_demo(_a.ld_index, _a.seconds))
