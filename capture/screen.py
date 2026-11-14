"""屏幕截取: PrintWindow 截取窗口画面, 并统一到目标分辨率."""

import ctypes
import hashlib
import io
import os
import struct
import subprocess
import time

from PIL import Image, ImageGrab
import win32con
import win32gui
import win32ui

from capture.window import find_ldplayer, find_render_window, restore_window
from config.config import default_config
# 子进程不弹控制台黑框 (2026-10-28): GUI exe / 值守进程下 adb.exe 会每帧弹一个黑框
from utils.no_window import NO_WINDOW

# Win8.1+ 的 PrintWindow 标志, 可截取 DirectX 渲染的画面
PW_RENDERFULLCONTENT = 0x00000002
RENDER_RESIZE_TIMEOUT = 3.0   # 每轮 MoveWindow 后等待渲染区跟上的上限秒数 (异步, 轮询)
RESIZE_ROUNDS = 4             # 闭环重试轮数 (模拟器套用尺寸可能滞后数秒~更久)

user32 = ctypes.windll.user32


# ---- 锁屏感知的 adb 截图兜底 (2026-10-26 实机事故) -------------------------------
# 事故经过: 用户睡前启动 6 小时正式采集, 08:33 正常开跑, **08:45 起画面永久冻结** ——
#   Windows 锁屏后 DWM 停止为被遮挡的窗口合成画面, `PrintWindow(RenderWindow)` 只能拿到
#   最后一帧缓存, 于是 OCR 每 2 秒都读到同一个 stage/remain, 采集进程在门 3 空转到底
#   (CPU 还在涨, 日志却一行不涨; py-spy 三次采样都停在 prep_capture.py:1305 的 sleep)。
# 实测判据 (同一时刻两条通道对比): adb 截图 md5 **每次都变**(画面是活的), PrintWindow
#   恒定不变(冻结); 且 adb 通道 `exec-out screencap -p` 只要 0.468s/张、1920x1080,
#   与 RenderWindow 渲染区 1:1 —— 截图发生在 Android 内部, 锁屏/窗口被遮挡都不影响它。
# 因此: ① 前台是锁屏/登录界面时直接走 adb; ② 没锁屏但 PrintWindow 连续冻结(其它原因的
#   假死, 例如窗口被拖到屏幕外不再重绘)时也切 adb。每张图都记 `last_shot_source()`,
#   落盘时写进 manifest, 让"adb 帧"**可筛选** —— 数据正确性优先, 绝不混进来还看不出处。
_SHOT_SRC = {"kind": "printwindow", "t": 0.0, "detail": ""}
_FREEZE = {"md5": "", "t": 0.0, "stuck": 0, "static": ""}  # PrintWindow 冻结检测
FREEZE_STUCK_LIMIT = 3        # 连续几张(且每张间隔>=FREEZE_MIN_GAP)完全相同就算"可疑冻结"
FREEZE_MIN_GAP = 0.5          # 两次采样最小间隔秒数 (太密的话同一帧本来就会重复)


def last_shot_source() -> dict:
    """最近一次截图的来源: {"kind": "printwindow"|"adb"|"screen", "t": 时间戳, "detail": 说明}.

    落盘时写进 manifest (`shot` 字段), 事后可筛出 adb 帧单独处理/剔除。
    注意这是**进程内最近一次**, 一批连拍 (b1/A/b2) 会互相覆盖; 要精确到"这张图",
    用 ``shot_source_of(img)`` (capture_game 会把来源贴在返回的 PIL Image 上)。
    """
    return dict(_SHOT_SRC)


def shot_source_of(img) -> dict:
    """**这一张图**的来源 (capture_game 贴在 Image 属性 ``_shot_src`` 上的那份).

    取不到就退回进程内最近一次 (老调用路径/图片被 resize 过后属性会丢), 再退 unknown。
    """
    src = getattr(img, "_shot_src", None)
    if isinstance(src, dict) and src:
        return dict(src)
    return dict(_SHOT_SRC)


def _set_src(kind: str, detail: str = "") -> dict:
    """记下"这次截图走的哪条通道", 返回该记录 (调用方贴到返回的 Image 上)。"""
    old = _SHOT_SRC.get("kind")
    _SHOT_SRC.update(kind=kind, t=time.time(), detail=detail)
    if old != kind:
        # 只在**通道切换**时打印一行 (不是每帧都打, 免得污染日志) ——
        #   用户按字符串搜 "通道切换" 就能知道这批帧是从哪条通道来的。
        print(f"[截图] 通道切换: {old} -> {kind}"
              f"{' (' + detail + ')' if detail else ''}", flush=True)
    return dict(_SHOT_SRC)


def screen_locked() -> bool:
    """当前桌面是否被 Windows **锁屏界面**盖住 (锁屏时 PrintWindow 拿不到新画面).

    判据: 前台窗口是 UWP CoreWindow 且标题是"Windows 默认锁屏界面"/"锁屏界面"。
    (登录界面 LogonUI 走的是安全桌面, 那种情况下前台句柄可能拿不到, 一并按锁屏处理。)
    """
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if cls == "Windows.UI.Core.CoreWindow" and ("锁屏" in title or "Lock screen" in title):
            return True
        if "锁屏界面" in title or "Lock screen" in title:
            return True
        return False
    except Exception:  # noqa: BLE001 - 判断失败一律按"没锁屏", 别把采集拦下
        return False


def _adb_exe() -> str | None:
    """雷电自带 adb.exe 的路径 (与 ldconsole.exe 同目录, 复用 capture.window 的定位)。"""
    try:
        from capture.window import _ldconsole_path
        p = _ldconsole_path()
    except Exception:  # noqa: BLE001
        return None
    if not p:
        return None
    cand = os.path.join(os.path.dirname(p), "adb.exe")
    return cand if os.path.isfile(cand) else None


def _adb_serial(ld_index: int) -> str:
    """雷电实例 N 的 adb 序列号 (实测实例 0=emulator-5554, 实例 1=emulator-5556, 步长 2)。"""
    return "emulator-%d" % (5554 + 2 * int(ld_index))


def _parse_raw_screencap(data: bytes) -> Image.Image | None:
    """解析 `adb exec-out screencap`(不带 -p) 的原始像素: 小端头 + RGBA_8888 像素.

    头有两种历史格式 (实测本机 Android 9 是 16 字节: w/h/fmt/colorspace):
      - 12 字节: w, h, fmt
      - 16 字节: w, h, fmt, colorspace
    用 `len == 头长 + w*h*4` 反推是哪一种, 对不上就当解析失败返回 None (调用方回退 PNG)。
    为什么值得解析: 实测 raw 通道 **0.29s/张**, PNG 通道 0.95~1.0s —— 采集窗口只有 25s,
    8 家每张快 0.7s 就是 5.6s, 直接决定能不能采满 8 家。
    """
    if not data or len(data) < 16:
        return None
    w, h, fmt = struct.unpack("<III", data[:12])
    if not (100 <= w <= 4096 and 100 <= h <= 4096):
        return None
    if fmt != 1:            # 1 = RGBA_8888 (其它格式没见过, 不猜)
        return None
    body = len(data) - w * h * 4
    if body not in (12, 16):
        return None
    raw = data[body:body + w * h * 4]
    try:
        return Image.frombuffer("RGBA", (w, h), raw, "raw", "RGBA", 0, 1).convert("RGB")
    except Exception:  # noqa: BLE001 - 解析不了就回退 PNG
        return None


def _adb_capture(ld_index: int) -> Image.Image | None:
    """用 adb 在 Android 内部截一张 1920x1080 的游戏画面; 失败返回 None (调用方回退)。

    两条路: ① `exec-out screencap`(raw, 实测 0.29s) 优先; ② 解析不了再走
    `exec-out screencap -p`(PNG, 实测 0.95~1.0s, 慢但稳)。
    """
    exe = _adb_exe()
    if not exe:
        return None
    serial = _adb_serial(ld_index)
    try:
        out = subprocess.run([exe, "-s", serial, "exec-out", "screencap"],
                             capture_output=True, timeout=30,
                             creationflags=NO_WINDOW).stdout
        img = _parse_raw_screencap(out)
        if img is not None:
            return img
    except Exception:  # noqa: BLE001 - raw 通道失败不代表 adb 不通, 继续试 PNG
        pass
    try:
        out = subprocess.run([exe, "-s", serial, "exec-out", "screencap -p"],
                             capture_output=True, timeout=30,
                             creationflags=NO_WINDOW).stdout
        if not out or len(out) < 1024:
            return None
        return Image.open(io.BytesIO(out)).convert("RGB")
    except Exception:  # noqa: BLE001 - adb 掉线/超时都不许把采集打断, 回退 PrintWindow
        return None


def _md5(img: Image.Image) -> str:
    """图片内容的指纹, **只用于「两张图是不是同一帧」的判等**.

    2026-10-31 提速: 全尺寸 1920x1080 算一次 md5 要 **14.5ms**, 而调用处只做两种判等
    —— ``_md5(img) == _FREEZE['md5']``(跟上一帧比) 和 ``_md5(live) == pw_md5``
    (PrintWindow 与 adb 两条通道比) —— 判定结果跟"缩到 160x90 再算"完全等价。
    缩小方式用 **``img.reduce(k)``(平均池化)** 而不是 ``resize``:
    - 1920x1080 原样算 md5 **18.5ms** -> ``resize(BILINEAR) 160x90`` **9.2ms**
      -> ``resize(NEAREST) 0.15ms`` -> **``reduce(12) 1.7ms``**;
      NEAREST 最快但只抽样 1/144 的像素, 局部小变化(倒计时数字跳一格)可能被抽没,
      会误判"冻结"进而白走一次 adb(0.3s); ``reduce`` 是块平均, 一个像素变化也会传导下来,
      判等语义最稳, 且比原实现省 **16.8ms/帧**。
    - k 取 ``w // 160`` 并要求宽高都能整除(1920x1080 -> k=12 -> 160x90, 1440x810 -> k=9),
      不整除时退回 BILINEAR 缩放, 都不会抛错。
    同一张图必得同值; 不同图概率上不会撞 (md5 抗碰撞, 与尺寸无关)。
    """
    w, h = img.size
    k = w // 160
    if k >= 2 and w % k == 0 and h % k == 0:
        small = img.reduce(k)
    elif w > 320:
        small = img.resize((160, max(1, round(h * 160 / w))), Image.BILINEAR)
    else:
        small = img
    return hashlib.md5(small.tobytes()).hexdigest()


def capture(hwnd: int) -> Image.Image:
    """截取窗口当前画面, 返回 PIL Image.

    优先用 PrintWindow (窗口被遮挡也能截取);
    若截到全黑画面 (部分 DirectX 窗口不支持), 回退为屏幕截取.
    """
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError(f"窗口尺寸无效: {width}x{height}")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bmp)
        user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        info = bmp.GetInfo()
        buf = bmp.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), buf, "raw", "BGRX", 0, 1
        )
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)

    if img.getbbox() is None:  # 全黑, PrintWindow 失败, 回退屏幕截取
        screen = ImageGrab.grab((left, top, right, bottom))
        if screen.getbbox() is not None:
            img = screen
    return img


def _freeze_check(img: Image.Image) -> bool:
    """PrintWindow 这张图是不是"冻住的旧帧" (连续多张完全相同且间隔够久).

    只做判断, 不改图片; 命中后调用方切 adb。理由: 游戏 prep 阶段顶栏倒计时每秒在跳,
    正常连拍不可能 3 张(每张间隔 >=0.5s)字节级完全一致。

    **注意这只是"可疑", 不是结论** (2026-10-26 12:20 踩过): 结算页/静止的战斗画面本来就
    一帧不变, 光看 PrintWindow 一模一样分不清"窗口冻了"和"画面本来就不动"。真正的判据是
    调用方拿 adb 帧对比 —— 两通道**都**一样才算画面静止 (见 ``capture_game``)。
    """
    md5 = _md5(img)
    now = time.time()
    if md5 == _FREEZE["md5"] and (now - _FREEZE["t"]) >= FREEZE_MIN_GAP:
        _FREEZE["stuck"] = int(_FREEZE["stuck"]) + 1
    else:
        _FREEZE["stuck"] = 1        # 1 = 当前这一张是新的
        _FREEZE["static"] = ""      # 画面变了, 之前"本来就静止"的判定作废
    _FREEZE["md5"] = md5
    _FREEZE["t"] = now
    return int(_FREEZE["stuck"]) >= FREEZE_STUCK_LIMIT


def capture_game() -> Image.Image:
    """截取雷电模拟器的纯游戏画面 (RenderWindow 子窗口, 无标题栏/工具栏).

    窗口最小化时会自动还原; 不会强制置顶或抢焦点.

    **锁屏/冻结兜底 (2026-10-26 事故)**: 桌面被 Windows 锁屏界面盖住时 DWM 停止为被遮挡窗口
    合成画面, PrintWindow 只能返回最后一帧缓存 (实测 md5 恒定、OCR 永远读到同一个 stage,
    采集在门 3 空转)。此时改用 adb 在 Android 内部截图 (`exec-out screencap -p`, 实测 0.468s,
    画面是活的)。没锁屏但 PrintWindow 连续冻结 (其它假死原因) 时同样切 adb 救一帧。
    实际用了哪条通道记在 `last_shot_source()`, 落盘写进 manifest 的 `shot` 字段。

    Returns:
        游戏画面 PIL Image (尺寸跟随模拟器窗口大小; adb 通道为 Android 原生 1920x1080).
    """
    wins = find_ldplayer()
    if not wins:
        raise RuntimeError("未找到雷电模拟器窗口, 请先启动模拟器")
    main_hwnd = wins[0]["hwnd"]
    ld_index = wins[0].get("ld_index")

    render_hwnd = find_render_window(main_hwnd)
    if render_hwnd is None:
        raise RuntimeError("未找到 RenderWindow 子窗口, 无法截取游戏画面")

    # ① 锁屏: 直接走 adb (PrintWindow 此时必然是旧帧, 连试都别试)
    fallback_detail = ""      # 锁屏但 adb 失败时, 这条说明要跟着落盘 (别被下面的空串盖掉)
    if screen_locked():
        img = _adb_capture(ld_index) if ld_index is not None else None
        if img is not None:
            src = _set_src("adb", "桌面锁屏, PrintWindow 拿不到新画面")
            _FREEZE.update(md5="", t=0.0, stuck=0)
            img._shot_src = src      # 贴在这张图上, 落盘时按图取来源
            return img
        fallback_detail = "桌面锁屏且 adb 截图失败, 只能用缓存帧"
        _set_src("printwindow", fallback_detail)

    if win32gui.IsIconic(main_hwnd):  # 最小化时先还原
        win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
        time.sleep(0.3)  # 等待窗口还原刷新

    img = capture(render_hwnd)
    if _freeze_check(img):
        # ② PrintWindow 连续多张一样: 有两种可能 —— 窗口真冻了 / 画面本来就不动(结算页、
        #    静止的战斗画面)。分不清就拿 adb 帧对比 (2026-10-26 12:20 事故: 只按 PW 判冻结,
        #    结果静止画面被反复当冻结, 通道来回切换刷屏, 每帧还白花 0.3s 走 adb)。
        pw_md5 = _md5(img)
        if pw_md5 == _FREEZE.get("static"):
            # 这张画面已经对比过: PW 与 adb 字节级一致 -> 就是静止画面, 别再浪费 adb 时间
            src = _set_src("printwindow", "画面本来就静止 (PW 与 adb 一致), 不是冻结")
        else:
            live = _adb_capture(ld_index) if ld_index is not None else None
            if live is None:
                src = _set_src("printwindow", f"PrintWindow 冻结但 adb 截图失败 "
                                              f"(连续 {int(_FREEZE['stuck'])} 帧)")
            elif _md5(live) == pw_md5:
                # 两条通道拿到同一张图 -> 画面真的没动, 不是窗口冻结; 记住这个 md5
                _FREEZE["static"] = pw_md5
                src = _set_src("printwindow", "画面本来就静止 (PW 与 adb 一致), 不是冻结")
            else:
                # PW 停在旧帧、adb 是活的 -> 真冻结, 用 adb 救这一帧
                src = _set_src("adb", f"PrintWindow 连续 {int(_FREEZE['stuck'])} 帧冻结, 改走 adb")
                live._shot_src = src
                return live
    else:
        src = _set_src("printwindow", fallback_detail)
    img._shot_src = src
    return img


def init_window_size(size: tuple[int, int] | None = None) -> tuple[int, int] | None:
    """将模拟器窗口渲染区调整为指定分辨率 (默认读 default_config.INIT_WINDOW_SIZE).

    MoveWindow 的宽高含边框/标题栏, 按主窗口与渲染窗口的尺寸差补偿,
    使 RenderWindow 渲染区恰好等于目标分辨率, 与 UI 标定的 1920×1080 坐标系一致.
    配置为 None (显式传 size=False 亦可) 时跳过并返回 None.

    两个实机坑 (用户踩过, 这里都处理掉):
      ① **最大化/铺满时 MoveWindow 调尺寸会被 Windows 忽略** → 每轮先 restore_window() 还原再调;
      ② 模拟器**异步且很慢**才让渲染区跟上 (实测: MoveWindow 后 6 秒内读到的还是旧尺寸
         1774x998, 几分钟后才变成目标 1920x1080, 结果被误报成"可能受窗口最小尺寸限制")
         → 做成**闭环重试**: 重算边框差值 →(必要时还原)→ MoveWindow → 轮询等待,
         等不到就再来一轮, 最多 RESIZE_ROUNDS 轮; 每轮打印实际值便于判断卡在哪一步。

    Returns:
        调整后的渲染区实际 (宽, 高); 未调整返回 None.
    """
    if size is None:
        size = default_config.INIT_WINDOW_SIZE
    if not size:
        return None
    wins = find_ldplayer()
    if not wins:
        raise RuntimeError("未找到雷电模拟器窗口, 请先启动模拟器")
    main_hwnd = wins[0]["hwnd"]
    render_hwnd = find_render_window(main_hwnd)
    if render_hwnd is None:
        raise RuntimeError("未找到 RenderWindow 子窗口, 无法调整窗口大小")

    target = (int(size[0]), int(size[1]))
    actual = _render_size(render_hwnd)
    for round_no in range(1, RESIZE_ROUNDS + 1):
        if actual == target:
            break
        # 每轮重算边框差值: 窗口状态 (铺满/侧栏展开) 变了, 补偿量也跟着变
        ml, mt, mr, mb = win32gui.GetWindowRect(main_hwnd)
        rl, rt, rr, rb = win32gui.GetWindowRect(render_hwnd)
        dx, dy = (mr - ml) - (rr - rl), (mb - mt) - (rb - rt)
        restored = restore_window(main_hwnd)   # 最大化/铺满时尺寸调不动, 先还原
        if restored:
            time.sleep(0.3)
            ml, mt, mr, mb = win32gui.GetWindowRect(main_hwnd)
        win32gui.MoveWindow(main_hwnd, ml, mt, target[0] + dx, target[1] + dy, True)
        deadline = time.perf_counter() + RENDER_RESIZE_TIMEOUT
        while True:   # 轮询等渲染区跟上 (异步), 拿到位就退出, 别死等固定时长
            time.sleep(0.2)
            actual = _render_size(render_hwnd)
            if actual == target or time.perf_counter() >= deadline:
                break
        if actual != target:
            print(f"[窗口尺寸] 第 {round_no}/{RESIZE_ROUNDS} 轮未生效: 渲染区 {actual[0]}x{actual[1]}"
                  f" (目标 {target[0]}x{target[1]}, 主窗口 {mr - ml}x{mb - mt}, 边框差值 {dx}x{dy}"
                  f"{', 已还原最大化/铺满' if restored else ''}) —— 继续重试", flush=True)
    return actual


def _render_size(render_hwnd: int) -> tuple[int, int]:
    """渲染子窗口当前 (宽, 高)."""
    left, top, right, bottom = win32gui.GetWindowRect(render_hwnd)
    return (right - left, bottom - top)


def resize(img: Image.Image, size: tuple[int, int] | None = None) -> Image.Image:
    """将截图统一缩放到目标分辨率 (默认 default_config.DEFAULT_RESOLUTION)."""
    out = img.resize(size or default_config.DEFAULT_RESOLUTION, Image.LANCZOS)
    src = getattr(img, "_shot_src", None)   # 缩放会产生新对象, 把"来源"一起带过去
    if src is not None:
        out._shot_src = src
    return out


# ---------------- 截图计时 (用户口径 2026-10-13) ----------------
# 用户: "我就想要你打印一下截图的时间 —— 场上奕子信息提取完之后截图花多久? 备战席截图花多久?
#        两个都分别打印出来。"
# 口径: 一次"截图" = capture_game() + resize() 这条抓屏链路本身。
#       点击鼠标、详情卡渲染等待 (ROSTER_CARD_DELAY)、OCR/识别、存证写盘 **都不算截图时间**。
SHOT_GROUP_BOARD = "board"      # 场上奕子那趟
SHOT_GROUP_BENCH = "bench"      # 备战席那趟
SHOT_GROUP_FRAME = "整帧"        # 整帧抓拍 (initial / 截图B / 落盘帧 / final)
_SHOT_STATS: dict[str, list] = {}   # group -> [张数, 总秒数]
# **用户口径 (2026-10-13)**: "把你一个一个提取、每张都要打印时间的那个步骤删掉 —— 每提取一个格子
#   就给我打印一行, 完全是污染我的 debug 信息。" → 逐张打印默认**关闭** (只留阶段汇总),
#   需要排查时再 set_shot_verbose(True) 打开。
SHOT_VERBOSE = False


def set_shot_verbose(on: bool = True) -> None:
    """逐张 `[截图] xxx 耗时` 打印开关 (默认关; 汇总行不受影响)."""
    global SHOT_VERBOSE
    SHOT_VERBOSE = bool(on)


def shot_snapshot() -> dict:
    """当前累计快照 {group: (张数, 秒数)}; 前后各取一次即为"本趟"的增量."""
    return {g: (v[0], v[1]) for g, v in _SHOT_STATS.items()}


def grab_timed(tag: str = "", group: str = SHOT_GROUP_FRAME) -> Image.Image:
    """截图并计时: 等价于 `resize(capture_game())`, 额外累计本次耗时并按 group 汇总.

    默认**不逐张打印** (用户口径: 那是 debug 噪音); 只累计, 由 print_shot_totals /
    `[阶段耗时]` 行按阶段汇总输出。
    """
    t0 = time.perf_counter()
    img = resize(capture_game())
    dt = time.perf_counter() - t0
    cell = _SHOT_STATS.setdefault(group, [0, 0.0])
    cell[0] += 1
    cell[1] += dt
    if SHOT_VERBOSE:
        print(f"[截图] {tag or '截图'} 耗时 {dt:.3f}s "
              f"(分组={group} 第 {cell[0]} 张, 该组累计 {cell[1]:.2f}s)", flush=True)
    return img


def print_shot_totals(before: dict, title: str = "") -> None:
    """打印相对 before 快照的**每分组截图合计** (用户口径: 要截图时间, 不是 OCR 时间)."""
    after = shot_snapshot()
    rows = [(g, n - before.get(g, (0, 0.0))[0], s - before.get(g, (0, 0.0))[1])
            for g, (n, s) in after.items() if n - before.get(g, (0, 0.0))[0] > 0]
    if not rows:
        return
    total_n = sum(r[1] for r in rows)
    total_s = sum(r[2] for r in rows)
    parts = " | ".join(f"{g} {n} 张 {s:.2f}s (平均 {s / n:.3f}s)" for g, n, s in rows)
    print(f"[截图] {title}截图合计: {parts} \n   --- 合计 {total_n} 张 {total_s:.2f}s", flush=True)
