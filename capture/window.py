"""窗口定位与激活: 查找雷电模拟器及渲染子窗口, 置顶/还原等窗口操作."""

import ctypes
import os
import subprocess
import time
from datetime import timezone

import win32api
import win32con
import win32gui
import win32process

from config.config import default_config
# 子进程不弹控制台黑框 (2026-10-28): ldconsole list2 每 5s 一次, 无控制台父进程下会闪黑框
from utils.no_window import NO_WINDOW

user32 = ctypes.windll.user32

# 进程 DPI 感知: 高缩放屏 (如 125%) 下, 非 DPI 感知进程的 GetWindowRect/MoveWindow/
# PrintWindow 会走逻辑坐标虚拟化 —— 窗口调不到位、截图被系统缩放变模糊 (OCR 全挂).
# 在模块加载时声明, 使所有入口 (python -m / PyCharm 直跑) 统一使用物理像素.
user32.SetProcessDPIAware()


def find_windows(class_name: str | None = None,
                 title_keywords: tuple[str, ...] = ()) -> list[dict]:
    """枚举所有可见顶层窗口, 返回匹配的窗口信息列表.

    Returns:
        [{"hwnd", "title", "class", "rect", "client_size"}, ...]
    """
    results = []

    def on_enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        matched = (class_name and cls == class_name) or any(
            kw in title for kw in title_keywords
        )
        if matched:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
            results.append({
                "hwnd": hwnd,
                "title": title,
                "class": cls,
                "rect": (left, top, right, bottom),
                "client_size": (cr - cl, cb - ct),
            })

    win32gui.EnumWindows(on_enum, None)
    return results


# ---- 模拟器实例身份: 雷电 ID / 进程 / 启动时间 (用户 m04916) ---------------------
# 用户诉求 (m04916): 「你可以看到雷电模拟器的ID么? 最好能按照雷电模拟器进程创建时间的排序来,
#   单线程采集时, 都默认用最新的那个.」背景是单帧截图**莫名其妙绑到了后建的那个实例** ——
#   旧代码吃 `EnumWindows` 的 **Z 序**, 用户切一下窗口 `wins[0]` 就换人 (同一台机器两次枚举
#   实例 0/1 位置对调), 所以必须换成**与鼠标焦点无关的恒定判据**: 实例进程的创建时间。
# 身份来源: 雷电自带的 `ldconsole.exe list2` 给出权威映射
#   `index, title, topHwnd, bottomHwnd, androidStarted, pid, vboxPid, w, h, dpi`
# 实测 (2026-10-14): `0,雷电模拟器-0,5113316,2230250,1,132188,...` /
#   `1,雷电模拟器-0-1,265988,9700092,1,238524,...` 与 win32 枚举完全对得上。
_LD_TTL = 5.0                      # ldconsole list2 结果缓存秒数 (find_ldplayer 每帧都会调)
_LD_CACHE: dict = {"t": 0.0, "rows": None}
_IDENT_TTL = 60.0                  # 单窗口身份缓存秒数
_IDENT_CACHE: dict[int, tuple] = {}


def _exe_dir_of_pid(pid: int) -> str | None:
    """取进程可执行文件所在目录 (自动定位同目录的 ldconsole.exe, 不写死安装路径)."""
    flags = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
    for fl in (flags, win32con.PROCESS_QUERY_LIMITED_INFORMATION):
        h = None
        try:
            h = win32api.OpenProcess(fl, False, pid)
            return os.path.dirname(win32process.GetModuleFileNameEx(h, 0))
        except Exception:  # noqa: BLE001 - 权限不足/进程已退, 换下一种权限或放弃
            continue
        finally:
            if h:
                win32api.CloseHandle(h)
    return None


# 冷启动兜底 (2026-10-15, 用户 --start_ld_window 需求): 下面那份"从雷电进程目录推"的定位法在
# **模拟器一个都没开**时必然失效 —— 拿不到任何 pid, 于是返回 None, `ldconsole` 一律 rc=127,
# "自动启动实例"这条链路直接死路。补几个常见安装路径 (命中即用), 让冷启动也能发命令;
# 想钉死自己的安装位置仍走 config.LDPLAYER_CONSOLE。
_LD_COMMON_PATHS = (
    r"D:\leidian\LDPlayer9\ldconsole.exe",              # 本机实测路径 (2026-10-15)
    r"C:\leidian\LDPlayer9\ldconsole.exe",
    r"D:\LDPlayer9\ldconsole.exe",
    r"C:\Program Files\LDPlayer\LDPlayer9\ldconsole.exe",
    r"C:\Program Files (x86)\LDPlayer\LDPlayer9\ldconsole.exe",
)


def _ldconsole_path(sample_pid: int | None = None) -> str | None:
    """定位 ldconsole.exe: ① 配置显式指定 ② 从雷电进程目录推 ③ 常见安装路径兜底。"""
    p = str(getattr(default_config, "LDPLAYER_CONSOLE", "") or "").strip()
    if p and os.path.isfile(p):
        return p
    pids = [sample_pid] if sample_pid else []
    pids += [w["pid"] for w in _dnplayer_wins() if w.get("pid")]
    for pid in pids:
        d = _exe_dir_of_pid(pid)
        if d:
            cand = os.path.join(d, "ldconsole.exe")
            if os.path.isfile(cand):
                return cand
    for cand in _LD_COMMON_PATHS:
        if os.path.isfile(cand):
            return cand
    return None


def _dnplayer_wins() -> list[dict]:
    """轻量枚举雷电窗口 (只补 pid, 不跑 ldconsole —— 给 _ldconsole_path 定位用, 避免递归)。"""
    out = []
    for w in find_windows(default_config.LDPLAYER_CLASS,
                          default_config.LDPLAYER_TITLE_KEYWORDS):
        try:
            _, pid = win32process.GetWindowThreadProcessId(w["hwnd"])
            w["pid"] = pid
        except Exception:  # noqa: BLE001
            w["pid"] = 0
        out.append(w)
    return out


def _ld_list2(sample_pid: int | None = None, ttl: float = _LD_TTL) -> list[dict]:
    """跑 `ldconsole.exe list2` 拿**权威**实例表 (index/标题/句柄/pid), 带 TTL 缓存。"""
    now = time.time()
    if _LD_CACHE["rows"] is not None and now - _LD_CACHE["t"] < ttl:
        return _LD_CACHE["rows"]
    rows: list[dict] = []
    exe = _ldconsole_path(sample_pid)
    if exe:
        try:
            out = subprocess.run([exe, "list2"], capture_output=True,
                                 timeout=10,
                                 creationflags=NO_WINDOW).stdout.decode("gbk", "replace")
            for line in out.splitlines():
                f = [x.strip() for x in line.split(",")]
                if len(f) >= 7 and f[0].isdigit():
                    rows.append({
                        "ld_index": int(f[0]), "ld_title": f[1],
                        "top_hwnd": int(f[2] or 0), "bottom_hwnd": int(f[3] or 0),
                        "started": f[4] == "1", "pid": int(f[5] or 0),
                        "vbox_pid": int(f[6] or 0),
                    })
        except Exception:  # noqa: BLE001 - 取不到就当没有, 退到标题解析
            rows = []
    _LD_CACHE.update(t=now, rows=rows)
    return rows


def _proc_created(pid: int) -> float:
    """实例进程的创建时间 (本地时间戳秒), 取不到返回 0.0.

    注意: `GetProcessTimes` 给的是 **UTC** (实测实例 0 返回 20:54:15 而本地是 04:54:15),
    这里显式按 UTC 解释再转本地, 否则排序会整体偏 8 小时 (同一天内相对顺序不变,
    但跨天就会把新实例排到旧实例前面)。
    """
    h = None
    try:
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        ct = win32process.GetProcessTimes(h)["CreationTime"]
        return ct.replace(tzinfo=timezone.utc).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0
    finally:
        if h:
            win32api.CloseHandle(h)


def identify(w: dict) -> dict:
    """给窗口补齐实例身份 (原地改并返回): `pid` / `created` / `ld_index` / `ld_title`.

    `ld_index` 是**雷电多开器里那个 ID** (截图左边那列), 优先取 `ldconsole list2`;
    取不到时退回从标题解析 (`雷电模拟器-0-1` -> 1), 那只是近似值, 所以标 `ld_index_src`。
    """
    hwnd = w["hwnd"]
    hit = _IDENT_CACHE.get(hwnd)
    if hit and time.time() - hit[0] < _IDENT_TTL:
        w.update(hit[1])
        return w
    info = {"pid": 0, "created": 0.0, "ld_index": None, "ld_title": "",
            "ld_index_src": ""}
    pid = 0
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        info["pid"] = pid
        info["created"] = _proc_created(pid)
    except Exception:  # noqa: BLE001
        pass
    for r in _ld_list2(pid or None):
        if r["top_hwnd"] == hwnd or r["bottom_hwnd"] == hwnd or (pid and r["pid"] == pid):
            info["ld_index"] = r["ld_index"]
            info["ld_title"] = r["ld_title"]
            info["ld_index_src"] = "ldconsole"
            break
    if info["ld_index"] is None:
        tail = str(w.get("title", "")).rsplit("-", 1)[-1].strip()
        if tail.isdigit():
            info["ld_index"] = int(tail)
            info["ld_index_src"] = "title"
    _IDENT_CACHE[hwnd] = (time.time(), dict(info))
    w.update(info)
    return w


def describe(w: dict) -> str:
    """一行描述实例身份 (日志用, 让用户一眼看到脚本绑的是哪一个)。"""
    parts = [str(w.get("title") or "?")]
    ld = w.get("ld_index")
    if ld is not None:
        src = w.get("ld_index_src") or ""
        parts.append(f"雷电ID={ld}" + ("" if src == "ldconsole" else f"({src})"))
    parts.append(f"hwnd={w.get('hwnd')}")
    if w.get("pid"):
        parts.append(f"pid={w['pid']}")
    c = float(w.get("created") or 0.0)
    if c:
        parts.append("启动于 " + time.strftime("%m-%d %H:%M:%S", time.localtime(c)))
    return parts[0] + " (" + ", ".join(parts[1:]) + ")"


def _spec_num(spec: str) -> int | None:
    """从 `--window` 取值里解析出**雷电实例 ID** (纯数字 / `ldN` / `wN` 三种写法同义).

    `wN` 是用户习惯写法 (用户 m: "w0 或者 w1"), 2026-10-15 补上 —— 之前只认 `ldN`。
    取不出 (`idxN` 第几个 / `0x` 句柄 / 标题) 返回 None: 那些取值表达的是"某台窗口", 不是
    "某个实例 ID", 冷启动时无从下手 (见 `spec_ld_index`)。
    """
    s = str(spec or "").strip()
    low = s.lower()
    if low.startswith("ld") and low[2:].isdigit():
        return int(low[2:])
    if low.startswith("w") and low[1:].isdigit():
        return int(low[1:])
    return int(s) if s.isdigit() else None


def spec_ld_index(spec: str, *, rows: list[dict] | None = None) -> int | None:
    """把 `--window` 取值解析成**雷电实例 ID** —— 供"实例没开 → 自动启动它"用 (用户 2026-10-15).

    口径:

    ① 纯数字 / `ldN` / `wN` 直接就是雷电 ID;
    ② 标题 (如 `雷电模拟器-1`) 用 `ldconsole list2` 的 `ld_title` 反查 —— **没启动的实例也在
       list2 里** (它列的是"已配置的实例"), 所以冷启动时标题同样能定位;
    ③ `idxN` / `0x句柄` 表达的是"枚举出来的第几个 / 某个具体窗口", 推不出实例 ID, 返回 None
       (调用方据此明确报错, 而不是随便挑一个实例去启动)。
    """
    num = _spec_num(spec)
    if num is not None:
        return num
    s = str(spec or "").strip()
    if not s:
        return None
    rs = _ld_list2() if rows is None else rows
    for r in rs:                                  # 精确标题优先
        if str(r.get("ld_title") or "") == s:
            return int(r["ld_index"])
    for r in rs:                                  # 再退标题子串
        if s in str(r.get("ld_title") or ""):
            return int(r["ld_index"])
    return None


def _match_window(wins: list[dict], spec: str) -> dict | None:
    """从候选窗口里按 **雷电ID / 第几个 / 窗口句柄 / 标题** 挑一个 (多开时指定实例用)。

    取值口径 (`capture.window` 是**全项目唯一**的实例选择实现 —— 用户 m07922 要求
    `collect_v2` / `capture_once` / `multi_collecter` 不许各写一套):

    - 纯数字 `0` / `1` / …: **雷电多开器里的实例 ID** (`ldconsole list2` 的 index, 0 起, 就是
      多开器左列那个号)。**用户 m08223 拍板改回这个口径** —— 2026-10-15 当天曾短暂改成
      "第几个"(1 起), 当天就回退了, 别再来一次。匹配不到 ID 时依次退**列表下标**、
      **十进制窗口句柄**, 所以老写法 `--window 9438774` 一直可用;
    - `idx1` / `idx2` / …: **第几个实例** (1 起), 与 `list_instances()` 打印的 `[1] [2] …` 一致
      (按实例创建时间升序, 最旧的是第 1 个)。序号与雷电 ID 会因为"建了删、删了建"而错开
      (多开器左列可能是 0、2), 想按"枚举出来的第几个"选就用这个;
    - `ld0` / `ld1`: 与纯数字**同义**, 显式写法, 一眼看得出选的是雷电 ID;
    - `w0` / `w1`: 也与纯数字**同义** (用户习惯写法 "w0 或者 w1", 2026-10-15 补上);
    - `0x...`: 十六进制句柄;
    - 其余: 先精确匹配标题, 再退回标题子串 (如 `"雷电模拟器-0"`).

    各入口启动时都会把解析结果打出来 (`resolve_window`), 例如
    `--window '1' -> 第 2/2 个: 雷电模拟器-0-1 (雷电ID=1, hwnd=198826, …)`。
    """
    spec = spec.strip()
    if not spec:
        return None
    low = spec.lower()
    if low.startswith("idx") and low[3:].isdigit():
        n = int(low[3:])                    # idx1 / idx2 = 第几个 (1 起)
        idx = n - 1
        return wins[idx] if 0 <= idx < len(wins) else None
    num = _spec_num(spec)                   # 纯数字 / ldN / wN 三者同义 (都指雷电实例 ID)
    if num is not None:
        n = num
        for w in wins:                      # ① 雷电实例 ID (默认口径)
            if w.get("ld_index") == n:
                return w
        if 0 <= n < len(wins):              # ② 列表下标 (拿不到 ldconsole 时的兜底)
            return wins[n]
        for w in wins:                      # ③ 十进制句柄
            if w["hwnd"] == n:
                return w
        return None
    if low.startswith("0x"):
        try:
            hwnd = int(spec, 16)
        except ValueError:
            return None
        for w in wins:
            if w["hwnd"] == hwnd:
                return w
        return None
    for w in wins:
        if w["title"] == spec:
            return w
    for w in wins:
        if spec in w["title"]:
            return w
    return None


def list_instances(*, usable_only: bool = True) -> list[dict]:
    """列出雷电实例 —— **全项目唯一的实例枚举口径** (用户 m07922: 不许各写一套).

    返回的顺序就是"第几个"的顺序, 也正是 `--window idx1/idx2/…` 的选择口径, 以及
    `resolve_window` / `pick_instances` 打印出来的 `[1] [2] …` 序号。(纯数字 `--window 0/1`
    走的是**雷电实例 ID**, 见 `_match_window` —— 那个号与这里的序号可能错开。)

    排序 (用户 m04916 / m05080): 旧代码吃 `EnumWindows` 的 **Z 序**, 新开的实例排前面, 所以谁在
    `wins[0]` 完全看用户当前点着哪个窗口 —— 实机表现为"单帧截图莫名绑到我后建的那个模拟器"。
    现在每个窗口都补上实例身份 (`identify`) 并按**实例进程创建时间升序**排, 即:
    **不带 `--window` 时默认用最早创建 (最旧) 的那个实例** —— 用户 m05080 定下的口径:
    「修改下, 单线程默认用旧的那个.」(m04916 原本要求"默认最新的", 现已反转; 因为游戏一直
    跑在旧实例里, 默认新实例反而让单帧脚本采错机器)。取不到创建时间的实例排到最后,
    不许它冒充"最旧"抢占第 1 个 (它的 `describe` 里不会出现"启动于", 一眼可辨)。

    Args:
        usable_only: 只保留**真有 `RenderWindow` 渲染子窗口**的窗口 (默认 True)。
            踩过 (2026-10-14 实机事故): `find_windows` 的匹配是 **OR** 语义, 只有在浏览器里搜过
            "雷电模拟器" 这类词, 浏览器窗口的标题也会含关键字并被命中; 它一旦排在列表第 0 位,
            `wins[0]` 就变成浏览器 -> `find_render_window` 返回 None ->
            整条采集链抛 `RuntimeError('未找到 RenderWindow 子窗口, 无法截取游戏画面')` 退出
            (collect_v8 就是这么在 15 分钟处崩的)。
            这道硬过滤既挡标题误命中, 也保证 `wins[0]` 一定可用。
    """
    wins = find_windows(default_config.LDPLAYER_CLASS,
                        default_config.LDPLAYER_TITLE_KEYWORDS)
    if usable_only:
        wins = [w for w in wins if find_render_window(w["hwnd"])]
    for w in wins:
        identify(w)
    wins.sort(key=lambda w: (float(w.get("created") or 0.0) == 0.0,
                             float(w.get("created") or 0.0),
                             int(w.get("ld_index") or 0)))
    return wins


def find_ldplayer() -> list[dict]:
    """`list_instances()` + 把 `default_config.LDPLAYER_WINDOW` 选中的实例提到第 0 位.

    枚举与排序口径全在 `list_instances()` 里 (那里有历史事故的来龙去脉)。这里只多做一件事:
    `LDPLAYER_WINDOW` 非空时 (如 `--window 0` / `--window 雷电模拟器-0`) 把 `_match_window`
    选中的实例提到第 0 位 —— 采集链里所有 `wins[0]` 调用点靠这个切实例。
    要用日志看绑了谁: `describe(wins[0])`。

    `LDPLAYER_SHARED_MACHINE` (2026-10-24 多开事故): 为真时, **指定了实例却匹配不上**
    直接抛错, 而不是静默退回最旧实例。两条线跑两台模拟器时, 实例-1 的 RenderWindow
    只因游戏崩溃短暂消失, 旧逻辑就会把 `wins[0]` 换成实例-0 —— runner 于是把游戏开到
    另一条线的机器上、还在那边点击。宁可报错停下, 也不能误伤别的实例。
    """
    wins = list_instances()
    spec = str(getattr(default_config, "LDPLAYER_WINDOW", "") or "").strip()
    if spec and wins:
        picked = _match_window(wins, spec)
        if picked is None:
            if getattr(default_config, "LDPLAYER_SHARED_MACHINE", False):
                avail = " | ".join(f"[{i + 1}] {describe(w)}" for i, w in enumerate(wins))
                raise RuntimeError(
                    f"绑定的雷电实例 {spec!r} 不在可用列表里, 拒绝退回其它实例 "
                    f"(LDPLAYER_SHARED_MACHINE=True, 防多开误伤); 当前可用: {avail}")
            return wins
        if wins[0] is not picked:
            wins.remove(picked)
            wins.insert(0, picked)
    return wins


def resolve_window(spec: str) -> tuple[dict | None, str]:
    """把 `--window` 的取值解析成一个实例, 返回 `(实例, 一行说明)`.

    三个入口 (`collect_v2` / `capture_once` / `multi_collecter`) 共用这一份口径与文案
    (用户 m07922)。解析失败时说明里会列出**所有可选实例**, 调用方直接打印并报错退出 ——
    绝不允许"匹配不上就悄悄退回最旧的那个": 那会让人以为采的是指定实例, 实际采了另一台
    (2026-10-15 之前的 `collect_v2 --window` 就是这个行为)。
    """
    wins = list_instances()
    if not wins:
        return None, "没找到任何可用雷电实例 (要求窗口里真有 RenderWindow 渲染子窗口)"
    picked = _match_window(wins, str(spec))
    if picked is None:
        avail = " | ".join(f"[{i + 1}] {describe(w)}" for i, w in enumerate(wins))
        return None, f"--window {str(spec)!r} 没匹配到实例; 可选: {avail}"
    return picked, (f"--window {str(spec)!r} -> 第 {wins.index(picked) + 1}/{len(wins)} 个: "
                    + describe(picked))


def pick_instances(specs, num: int, wins: list[dict] | None = None
                   ) -> tuple[list[dict], list[str]]:
    """给多开挑 `num` 个实例, 返回 `(选中的实例, 问题列表)`.

    `specs` 为空 = 按默认顺序取前 `num` 个 (最旧的先, 用户 m05080)。取值口径与单实例的
    `--window` 完全一致 (都走 `_match_window`), 序号就是 `list_instances()` 的 `[1] [2] …`。

    问题列表非空 = 调用方应打印并**报错退出**: 取值没匹配上 / 选到了同一个实例两次 /
    可用实例少于 `num` —— 免得"少起了几路""起了两路同一个实例"这种半成品状态被当成正常
    (旧代码匹配不上就悄悄丢掉, 用户 m07922 说的"无法复现"就是这么来的)。
    """
    all_wins = list_instances() if wins is None else wins
    picked: list[dict] = []
    problems: list[str] = []
    avail = " | ".join(f"[{i + 1}] {describe(w)}" for i, w in enumerate(all_wins))
    for i, spec in enumerate(list(specs or [])):
        w = _match_window(all_wins, str(spec))
        if w is None:
            problems.append(f"--windows 第 {i + 1} 个取值 {str(spec)!r} 没匹配到实例; 可选: {avail}")
            continue
        if any(x["hwnd"] == w["hwnd"] for x in picked):
            problems.append(f"--windows {str(spec)!r} 与前面选到了同一个实例 ({describe(w)})")
            continue
        picked.append(w)
    if not specs:
        picked = list(all_wins)
    if len(picked) < num:
        problems.append(f"只选到 {len(picked)} 个可用实例, 少于 --num={num}")
    return picked[:num], problems


def find_render_window(main_hwnd: int) -> int | None:
    """在模拟器主窗口下寻找 RenderWindow 子窗口 (游戏画面本体).

    Args:
        main_hwnd: 模拟器主窗口句柄.

    Returns:
        渲染子窗口句柄, 未找到返回 None.
    """
    result = []

    def on_child(child_hwnd, _):
        if win32gui.GetClassName(child_hwnd) == "RenderWindow":
            result.append(child_hwnd)

    try:
        win32gui.EnumChildWindows(main_hwnd, on_child, None)
    except win32gui.error:
        pass  # 无子窗口时 EnumChildWindows 会抛 error
    return result[0] if result else None


def is_maximized(hwnd: int) -> bool:
    """窗口是否处于最大化/铺满状态 (此时 MoveWindow 调尺寸会被 Windows 忽略).

    两道判据:
      ① GetWindowPlacement 的 showCmd == SW_SHOWMAXIMIZED (标准最大化按钮/双击标题栏);
      ② 兜底: 窗口矩形恰好等于所在显示器的工作区 —— 模拟器自带的"铺满/全屏窗口"模式下
         showCmd 仍报正常, 但尺寸已经顶满工作区, 一样调不动 (实测 1920x1032 == 工作区)。
    """
    placement = win32gui.GetWindowPlacement(hwnd)
    if len(placement) >= 2 and placement[1] == win32con.SW_SHOWMAXIMIZED:
        return True
    import win32api
    try:
        work = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))["Work"]  # 最近显示器
    except Exception:  # noqa: BLE001 - 取不到显示器信息就只认 showCmd 判据
        return False
    rect = win32gui.GetWindowRect(hwnd)
    return (rect[2] - rect[0], rect[3] - rect[1]) == (work[2] - work[0], work[3] - work[1])


def restore_window(hwnd: int) -> bool:
    """还原最小化/最大化/铺满的窗口 (SW_RESTORE), 返回是否执行了还原.

    调窗口尺寸之前必须先还原: 最大化状态下 MoveWindow 不生效 (用户实机踩到:
    渲染区卡在 1774x998 调不回 1920x1080, 报"可能受窗口最小尺寸限制"却是最大化导致的)。
    """
    if win32gui.IsIconic(hwnd) or is_maximized(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        return True
    return False


def cancel_topmost(hwnd: int):
    """取消窗口的置顶状态 (HWND_TOPMOST -> HWND_NOTOPMOST)."""
    win32gui.SetWindowPos(
        hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
    )


_ASFW_ANY = -1  # AllowSetForegroundWindow(ASFW_ANY): 允许任意进程设置前台窗口


def release_foreground(hwnd: int):
    """撤掉窗口置顶并释放它"抢"来的前台锁 (用户口径 2026-10-17).

    现象 (用户): "每局 restart 时激活雷电窗口到前台, 激活后还锁定它了, 必须点击一下雷电模拟器
    内部, 才能显示原来的窗口界面"。对策 = 激活完成后**立刻**做两件事:
      ① 撤掉 TOPMOST (别长期压在用户窗口之上);
      ② `AllowSetForegroundWindow(ASFW_ANY)` 让出前台抢锁, 使用户点其它窗口能正常覆盖它。
    """
    try:
        cancel_topmost(hwnd)
    except Exception as exc:      # noqa: BLE001 - 释放失败不许影响采集主流程
        print(f"[窗口] [!] 取消置顶失败: {exc!r}")
    try:
        user32.AllowSetForegroundWindow(_ASFW_ANY)
    except Exception as exc:      # noqa: BLE001
        print(f"[窗口] [!] 释放前台锁失败: {exc!r}")


def _force_foreground(hwnd: int):
    """SetForegroundWindow 受 Windows 前台锁定限制时, 模拟按 ALT 后重试."""
    try:
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        user32.keybd_event(win32con.VK_MENU, 0, 0, 0)
        user32.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)


def activate(hwnd: int, topmost: bool = True):
    """将窗口还原(若最小化)、置顶并切换为前台."""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if topmost:
        win32gui.SetWindowPos(
            hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
        )
    _force_foreground(hwnd)


# ---- 窗口"唤醒": 挪位 + 置前 + 送鼠标, 逼模拟器渲染器重绘 (用户 2026-10-15) ----------
# 用户现场: 把模拟器窗口拖到桌面左下角、大半超出屏幕下沿 —— Windows 不再给这块区域发重绘,
#   模拟器渲染器于是"冻"在最后一帧(对局结束后的画面), 采集端**每帧读出来的都是同一张旧图**,
#   `phase=unknown` 一直读不出(实测连续 25 回合 / unknown 持续 126s > 30s 门 → 被误判成
#   "已离开对局", 白走一遍 auto_start 开局流程却依旧认不出画面)。
# 解法: 记下原位置 → 挪到桌面左上角 (0,0) (超屏尺寸一并压回工作区: **尺寸一变必然触发
#   重绘**) → 置前 → 在渲染子窗口里送几下真鼠标(移动/点击/拖拽) → 调用方探测到画面恢复后
#   用 `restore_wake()` 按原 rect 挪回去。
# 注意: 这几下鼠标是**真的宿主键鼠事件**(用户明确要求"鼠标也随便点击、拖拽几下让程序苏醒"),
#   所以只适合"已经读不出画面的僵死窗口"; 千万别在正常采集途中调。
_WAKE_SAVED: dict[int, tuple[int, int, int, int]] = {}


def _work_area(hwnd: int) -> tuple[int, int]:
    """窗口所在显示器的工作区宽高 (排除任务栏)."""
    info = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))
    work = info["Work"]
    return work[2] - work[0], work[3] - work[1]


def _work_rect(hwnd: int) -> tuple[int, int, int, int]:
    """窗口所在显示器的工作区矩形 (左, 上, 右, 下; 排除任务栏)."""
    work = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))["Work"]
    return int(work[0]), int(work[1]), int(work[2]), int(work[3])


def offscreen_ratio(hwnd: int) -> float:
    """窗口矩形落在所在显示器工作区**之外**的面积占比 (0.0=全在屏内 ~ 1.0=全在屏外).

    用户口径 (2026-10-17): "检测到雷电模拟器有 2/3 都在屏幕外, 就需要把模拟器移动到屏幕内,
    然后激活一下, 以防雷电模拟器画面不刷新了"。取不到窗口矩形/显示器信息时返回 0.0
    (= 视为在屏幕内, 不触发挪位激活)。
    """
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
    except Exception:      # noqa: BLE001
        return 0.0
    area = max(1, r - l) * max(1, b - t)
    try:
        wl, wt, wr, wb = _work_rect(hwnd)
    except Exception:      # noqa: BLE001
        return 0.0
    ix = max(0, min(r, wr) - max(l, wl))       # 窗口 ∩ 工作区 的宽高
    iy = max(0, min(b, wb) - max(t, wt))
    return max(0.0, min(1.0, 1.0 - (ix * iy) / area))


def _nudge_mouse(render_hwnd: int | None, *, clicks: int = 2, drags: int = 1,
                 pause: float = 0.25, drag_steps: int = 8) -> None:
    """在渲染子窗口客户区里送几下真鼠标 (移动 + 点击 + 拖拽), 逼渲染器重绘.

    落点刻意避开**右上角**(游戏在那里有设置/退出按钮)与棋盘中央, 只走左下/底部中/左上
    这几个"多半是边框或空区"的位置。`render_hwnd` 为空(认不出渲染子窗口)时整个跳过。
    """
    if not render_hwnd:
        return
    try:
        cl, ct, cr, cb = win32gui.GetClientRect(render_hwnd)
    except Exception:      # noqa: BLE001 - 取不到客户区就只挪窗口, 不送鼠标
        return
    w, h = max(1, cr - cl), max(1, cb - ct)
    pts = ((0.08, 0.92), (0.50, 0.95), (0.08, 0.08))

    def _to_screen(px: float, py: float) -> tuple[int, int]:
        return win32gui.ClientToScreen(render_hwnd, (int(w * px), int(h * py)))

    for i in range(max(0, clicks)):
        x, y = _to_screen(*pts[i % len(pts)])
        win32api.SetCursorPos((x, y))
        time.sleep(pause)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(pause)
    for _ in range(max(0, drags)):
        x1, y1 = _to_screen(0.10, 0.85)
        x2, y2 = _to_screen(0.30, 0.75)
        win32api.SetCursorPos((x1, y1))
        time.sleep(pause)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        for s in range(1, drag_steps + 1):     # 分小步移动: 瞬移的拖拽对渲染器刺激不足
            win32api.SetCursorPos((int(x1 + (x2 - x1) * s / drag_steps),
                                   int(y1 + (y2 - y1) * s / drag_steps)))
            time.sleep(0.03)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(pause)


def wake_window(hwnd: int, *, to: tuple[int, int] = (0, 0),
                clicks: int = 2, drags: int = 1) -> bool:
    """激活窗口 (置前) 逼模拟器重绘; 若窗口大半在屏幕外, 先挪回屏幕内再送鼠标. 成功返回 True.

    **激活无条件, 挪窗口有条件** (用户口径 2026-10-17):
      * **总是** `activate()` 激活到前台 (画面不刷新时激活一下救急), 激活完**立刻**
        `release_foreground()` —— 撤置顶 + 释放前台锁, 让用户点其它窗口能正常覆盖;
      * 只有离屏面积占比 >= `config.WINDOW_WAKE_OFFSCREEN_RATIO` (默认 2/3) 时, 才把窗口
        **挪回屏幕内** (默认左上角 (0,0)) 并在渲染区送几下鼠标 —— "窗口大半在屏幕外"才是模拟器
        停止重绘的根因。窗口本来就在屏幕内时**不挪窗口、不送鼠标**, 只激活。
      * 取不到窗口矩形时放弃 (返回 False)。

    原位置**只记一次** (重复调用不会把"已经在 (0,0)" 当成原位置), 供 `restore_wake()` 复原;
    窗口在屏幕内 (没挪) 时不记。
    """
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception as exc:      # noqa: BLE001
        print(f"[唤醒] [!] 取窗口位置失败, 放弃唤醒: {exc!r}")
        return False
    _thr = float(getattr(default_config, "WINDOW_WAKE_OFFSCREEN_RATIO", 2.0 / 3.0) or 0.0)
    _move = offscreen_ratio(hwnd) >= _thr if _thr > 0 else True   # 需要挪回屏幕内吗
    x = y = w = h = 0
    try:
        if _move:
            _WAKE_SAVED.setdefault(hwnd, tuple(rect))
            x, y = int(to[0]), int(to[1])
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            try:
                aw, ah = _work_area(hwnd)
                w, h = min(w, aw), min(h, ah)  # 超屏尺寸压回工作区: 尺寸一变必然触发重绘
            except Exception:      # noqa: BLE001 - 取不到工作区就按原尺寸挪
                pass
            restore_window(hwnd)               # 最大化/铺满时 MoveWindow 不生效, 先还原
            win32gui.MoveWindow(hwnd, x, y, w, h, True)
        activate(hwnd)                         # 总是激活到前台 (用户口径 2026-10-17)
        if _move:
            _nudge_mouse(find_render_window(hwnd), clicks=clicks, drags=drags)
        release_foreground(hwnd)               # 激活完立刻撤置顶 + 释放前台锁 (用户口径 2026-10-17)
    except Exception as exc:      # noqa: BLE001 - 唤醒失败必须能返回, 不许打断采集
        print(f"[唤醒] [!] 挪窗口/激活失败: {exc!r}")
        return False
    if _move:
        print(f"[唤醒] 窗口已挪到 ({x},{y}) 尺寸 {w}x{h} 并送鼠标动作 "
              f"(移动/点击 {clicks} 次/拖拽 {drags} 次), 等画面刷新")
    else:
        print("[唤醒] 窗口大部分在屏幕内 → 只激活不挪窗口/不送鼠标 (用户口径 2026-10-17)")
    return True


def restore_wake(hwnd: int) -> bool:
    """把 `wake_window()` 挪走的窗口按**原 rect** 挪回去 (含原尺寸). 无记录时返回 False."""
    rect = _WAKE_SAVED.pop(hwnd, None)
    if not rect:
        return False
    try:
        restore_window(hwnd)
        win32gui.MoveWindow(hwnd, rect[0], rect[1],
                            rect[2] - rect[0], rect[3] - rect[1], True)
    except Exception as exc:      # noqa: BLE001
        print(f"[唤醒] [!] 复原窗口位置失败: {exc!r}")
        return False
    print(f"[唤醒] 窗口已复原到原位置 ({rect[0]},{rect[1]}) "
          f"尺寸 {rect[2] - rect[0]}x{rect[3] - rect[1]}")
    return True
