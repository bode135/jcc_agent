"""整机内存体检 (用户 m08914 事故 2026-10-14 02:58)。

事故链: 常驻 OCR 服务不可用 → 采集进程起**本地 GPU 引擎** → `CUDA failure 2: out of memory` →
回落 CPU → 主机内存也见底 (`Load model ... failed:bad allocation` / `BFCArena ... Failed to
allocate memory for requested buffer of size 66846720`) → CRT `fatal : Memory allocation failure`
**直接 abort** (不是 Python 异常, 所以没有 traceback, 整轮采集当场死掉)。

为什么不能只看自己进程: `config.OCR_MAX_RSS_GB` 那个看门狗只管**服务进程自己的 RSS**
(其注释也写明"那次 MemoryError 是整机内存压力, 不是 OCR 自己泄漏"), 而这次死的是**采集进程** ——
所以闸门必须读**整机**。psutil 已装, 读数失败一律返回 None/安全值, 绝不抛。
"""
from __future__ import annotations

import os

try:                       # psutil 缺失也要能用 (退化为"读不到" = 不拦)
    import psutil
except Exception:          # noqa: BLE001
    psutil = None          # type: ignore[assignment]


def free_gb() -> float | None:
    """整机**可用物理内存** (GB); 读不到返回 None。"""
    if psutil is None:
        return None
    try:
        return float(psutil.virtual_memory().available) / (1024 ** 3)
    except Exception:      # noqa: BLE001
        return None


def commit_ratio() -> float | None:
    """Windows 提交内存占用比 (commit / commit_limit); 读不到返回 None。

    `fatal : Memory allocation failure` 更接近"提交内存见底"而不是"物理内存为 0" ——
    所以两个指标一起看。
    """
    if psutil is None or os.name != "nt":
        return None
    try:
        vm = psutil.virtual_memory()
        _total = float(getattr(vm, "total", 0.0) or 0.0)
        if _total <= 0:
            return None
        _used = _total - float(getattr(vm, "available", 0.0) or 0.0)
        return max(0.0, min(1.0, _used / _total))
    except Exception:      # noqa: BLE001
        return None


def rss_mb() -> float:
    """**本进程**常驻内存 (MB); 读不到返回 0.0。"""
    if psutil is None:
        return 0.0
    try:
        return float(psutil.Process().memory_info().rss) / (1024 ** 2)
    except Exception:      # noqa: BLE001
        return 0.0


def line(tag: str = "") -> str:
    """一行体检文本: `[内存] 可用 11.2GB / 提交 60% / 本进程 1234MB` (可选前缀 tag)。"""
    _f = free_gb()
    _c = commit_ratio()
    _parts = [
        f"可用 {'?' if _f is None else f'{_f:.1f}GB'}",
        f"提交 {'?' if _c is None else f'{_c * 100:.0f}%'}",
        f"本进程 {rss_mb():.0f}MB",
    ]
    return f"[内存]{(' ' + tag) if tag else ''} " + " / ".join(_parts)


def too_low(min_free_gb: float) -> tuple[bool, str]:
    """可用内存是否低于闸门; 返回 (是否过低, 说明)。读不到内存时不拦。"""
    _f = free_gb()
    if _f is None or min_free_gb <= 0:
        return False, ""
    if _f < min_free_gb:
        return True, f"可用内存 {_f:.1f}GB < 闸门 {min_free_gb:.1f}GB"
    return False, ""
