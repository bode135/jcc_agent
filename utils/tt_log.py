# -*- coding: utf-8 -*-
"""给**带 `[tag]` 标识的日志行**插 `--- tt.now: [Xs] ---`(脚本运行时间) —— `--debug_cost_time` 调试用。

用户口径 (2026-10-16 提出, 2026-10-17 收紧): 打开该开关后, 该动作流程里**带前置 `[xxx]` 方括号
标识的行**才带上脚本运行时间, 形如
    [动作] --- tt.now: [12.4s] --- 奕子详情提取 mode=default
    [回合] --- tt.now: [12.4s] --- stage=3-7 phase=combat 当前回合剩余时间[remain_time]=2
即**有前置 `[tag]` 的行插在 tag 之后**; **没有 `[tag]` 的行原样输出**。
(2026-10-17 收紧原因: 旧版对无 tag 的行也硬插, 结果边框表格的 `+---+`、`| 项 | 值 |`、
`--- 基础 ---` 每行都挂一遍时间戳, 刷屏且看不清表格 —— 用户实机反馈"输出有点过头了"。)
关闭时逐字节原样透传 (零改动)。

为什么是"包一层 stdout"而不是去改散布在各模块的几十处 print: 该流程跨多个模块,
逐处改既漏又乱; 包一层后, 任何模块的 print 都自动带上, 一处开关全链路生效。

时间参照 = 本模块被 import 的那一刻 (≈脚本启动), 与项目既有"脚本运行时间"口径同源;
bdtime 的 ``Time().now()`` 返回距今秒数 (默认 3 位小数), 这里统一按 ``[X.Xs]`` 展示。
"""

from __future__ import annotations

import re
import sys

from bdtime import Time

_TT = Time()                                  # 脚本启动参照 (import 时刻起算)
_TAG = re.compile(r"^(\s*)(\[[^\]]+\])(.*)$")  # 前置 [tag] + 其余
# 用户 m00633: 横幅行 `===== [点击提取所有截图] --- ...` 也要带 tt.now —— 插在 `[tag]` 之后,
#   `=` 与 tag 原样保留 (只认 `=` 打头的等价于 `[tag]` 行, 不碰 `===== 第 N 次 =====` 这种无 tag 的)。
_TAG_EQ = re.compile(r"^(\s*)(={3,}[ \t]*)(\[[^\]]+\])(.*)$")


def now_tag() -> str:
    """本轮的时间戳片段, 形如 ``--- tt.now: [12.4s] ---``。"""
    return f"--- tt.now: [{_TT.now():.1f}s] ---"


def prefix_line(line: str) -> str:
    """给一行插前缀: **只有**带前置 ``[tag]`` 的行才插 (插在 tag 之后), 其余行**原样返回**。

    用户口径 (2026-10-17): 表格边框/表头/数据行 (``+---+``、``| 项 | 值 |``、``--- 基础 ---``)
    这类没有 ``[xxx]`` 标识的行不加 —— 否则一张表每行都挂一遍时间戳, 刷屏且看不出表格形状。
    空行同样原样返回。
    """
    m = _TAG_EQ.match(line)
    if m:                                 # `===== [tag] ...` -> `===== [tag] --- tt.now: [Xs] --- ...`
        return f"{m.group(1)}{m.group(2)}{m.group(3)} {now_tag()}{m.group(4)}"
    m = _TAG.match(line)
    if m:
        return f"{m.group(1)}{m.group(2)} {now_tag()}{m.group(3)}"
    return line


class _TtWriter:
    """按行改写 stdout 的包装器: 攒到 ``'\\n'`` 才成行 (半行/多行 print 都不会被切坏)。

    原始流为 ``None`` (pythonw 无控制台) 时只累积不写外流 —— 由上层 (_StreamTee) 负责展示。
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, original) -> None:
        self._orig = original
        self._buf = ""

    def write(self, s: str) -> None:
        if not s:
            return
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(prefix_line(line))
            self._raw("\n")
        self._flush()

    def _emit(self, text: str) -> None:
        self._raw(text)

    def _raw(self, s: str) -> None:
        if self._orig is not None:
            try:
                self._orig.write(s)
            except Exception:                 # noqa: BLE001 - 原流可能已关, 不能拖垮日志
                pass

    def _flush(self) -> None:
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:                 # noqa: BLE001
                pass

    def flush(self) -> None:
        if self._buf:                         # 收尾: 没有换行的残行也要带前缀吐出去
            self._emit(prefix_line(self._buf))
            self._buf = ""
        self._flush()

    def isatty(self) -> bool:
        return False


def enable() -> bool:
    """把当前 ``sys.stdout`` 包上改写层 (幂等)。**必须在 GUI 装好 _StreamTee 之后再调**,

    否则后面 sys.stdout 被 _StreamTee 覆盖, 这一层就白包了。返回 True = 本次真的包上了。
    """
    out = sys.stdout
    if isinstance(out, _TtWriter):
        return False
    sys.stdout = _TtWriter(out)
    return True


def disable() -> bool:
    """撤掉 ``_TtWriter`` 那层, 恢复成它包着的原始流 (幂等)。返回 True = 本次真的撤了。

    用户 2026-10-17: 「耗时分析」做成界面勾选框后要能**运行中随手开/关** —— 关就是把这层拆掉,
    逐字节还原, 与"从未 enable 过"等价。
    """
    out = sys.stdout
    if not isinstance(out, _TtWriter):
        return False
    sys.stdout = out._orig
    return True


def enabled() -> bool:
    """当前 stdout 是否已被包上改写层 (界面勾选框状态/测试断言用)。"""
    return isinstance(sys.stdout, _TtWriter)
