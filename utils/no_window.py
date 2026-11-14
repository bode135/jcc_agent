"""Windows 子进程「不弹控制台黑框」的统一 creationflags (2026-10-28 修 adb / ldconsole 黑框风暴)。

## 为什么需要这个模块
控制台程序 (adb.exe / ldconsole.exe / powershell.exe ...) 被拉起时,**只有当父进程自己
没有控制台**才会新建一个控制台窗口 —— 有控制台的父进程, 子进程直接继承那个控制台, 看不到黑框。
所以「在 cmd / 我们后台跑的 python 里一切正常」而「用户拿 runner_client.exe / 值守进程跑就疯狂闪黑框」,
根因不在广告也不是病毒, 而是这些父进程是 **GUI exe / CREATE_NO_WINDOW 起的进程**:
它们下面的每一次 adb 截图回退、每一次 ldconsole 查询或后台点击, 都会各自新建一个黑框。

实测证据见 `scripts/diag/_probe_popup_repro.py`: 用 pythonw.exe(无控制台) 当父进程, 调
`capture.screen._adb_capture` -> 弹 1 个 adb.exe 窗口; 调 `prep_capture._game_pid` /
`control.input._android_size` / `capture.window._ld_list2` -> 各弹 1 个 ldconsole.exe 窗口;
而走 `scripts.proc_mem.adb`(已带 flags) -> 0 个。采集每帧都要截图, 所以看起来是「疯狂弹窗」。

## 用法
    from utils.no_window import NO_WINDOW
    subprocess.run([...], capture_output=True, creationflags=NO_WINDOW)

**必须用本模块的值, 不要自己写 `0x08000000`**: POSIX 的 subprocess 分支要求
`creationflags` 必须为 0, 否则直接抛 ValueError —— `getattr(...) if os.name=="nt" else 0`
在 Windows 上取到 0x08000000, 在 macOS/Linux 上是 0, 两边都安全。
"""

import subprocess
import os

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# 别名: 有些老代码按 CREATE_NO_WINDOW 这个名字引用, 保留一个同名导出方便迁移。
CREATE_NO_WINDOW = NO_WINDOW

__all__ = ["NO_WINDOW", "CREATE_NO_WINDOW"]
