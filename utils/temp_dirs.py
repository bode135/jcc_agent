# -*- coding: utf-8 -*-
"""临时文件 / 临时目录的统一入口: 一律建在项目 `tempdir/` 下面。

## 为什么要有这个模块 (用户 m00001, 2026-10-28)
以前项目里到处是 `tempfile.mkdtemp(dir=str(ROOT))` —— 临时目录直接建在**项目根**。
测试一中断、或清理因权限失败, 根目录就堆满 `.dsh-tmp-*` / `tmp*` 空目录壳:
用户看着很脏, 而且每个都得单独判定"能不能删" (有没有真东西), 非常费劲。

现在统一收口: 临时产物全部进 `tempdir/`。它是**整目录一次性**清理的, 不会漏, 也
不会误删用户的东西。`tempdir/` 已被 `.gitignore` 整体忽略, 不进 git status。

## 用法
    from utils.temp_dirs import temp_root, mkdtemp, temp_dir, temp_path

    d = mkdtemp("eval-ab-")            # tempdir/eval-ab-xxxxxxxx, 调用方自己负责删
    with temp_dir("synth-") as d:      # 退出时自动整棵删 (异常也删)
        ...
    p = temp_path("run.log")           # tempdir/run.log, 固定名, 不建随机目录

## 硬规则 (见 AGENTS.md 第 3 节)
- 任何 `tempfile.mkdtemp` / `TemporaryDirectory` / 临时文件路径, **不要**再传
  `dir=str(ROOT)`、也不要裸写 `ROOT / "xxx.log"` —— 一律走本模块。
- 需要"必须落在项目内"才能算相对路径的场合 (例如 build_dataset 算 rel path),
  也走本模块: `tempdir/` 本来就在项目根下面, `Path(t).relative_to(PROJECT_ROOT)` 仍然成立。
- 建出来的目录**不再带** `.dsh-tmp-` 前缀: 那个前缀当年只是为了让 `.gitignore`
  一条规则兜住, 现在改由 `tempdir/` 整个目录兜住, 前缀已经没有意义了。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from config.config import default_config

__all__ = [
    "temp_root",
    "mkdtemp",
    "temp_dir",
    "temp_path",
    "install_default_tempdir",
]


def temp_root(sub: str = "") -> Path:
    """临时根目录 (默认 `tempdir/`, 给了 sub 就是其子目录); 不存在则先建出来。

    注意: `tempfile.mkdtemp(dir=...)` 要求 dir **事先存在**, 所以这里必须 mkdir。
    """
    root = Path(default_config.TEMP_ROOT)
    if sub:
        root = root / sub
    root.mkdir(parents=True, exist_ok=True)
    return root


def mkdtemp(prefix: str = "tmp-", sub: str = "") -> Path:
    """在 `tempdir/` 下建一个随机临时目录并返回 Path。**调用方负责删**。"""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(temp_root(sub))))


@contextmanager
def temp_dir(prefix: str = "tmp-", sub: str = ""):
    """`with temp_dir("xxx-") as d:` —— 退出时整棵删掉 (出异常也删)。"""
    d = mkdtemp(prefix=prefix, sub=sub)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def temp_path(name: str, sub: str = "") -> Path:
    """`tempdir/<name>` 的固定路径 (不建目录, 不保证唯一); 用于日志/中间 json 这类已知文件名。"""
    return temp_root(sub) / name


def install_default_tempdir(sub: str = "pytmp") -> Path:
    """把**本进程**的 `tempfile` 默认根改到 `tempdir/<sub>`, 并同步 TEMP/TMP/TMPDIR。

    干什么用的: 有些老代码 / 第三方库 (ultralytics、openpyxl、rembg …) 自己调
    `tempfile.mkdtemp()` 且**不传 dir**, 拿的是 `tempfile.gettempdir()`。把这里改掉,
    它们也一起落进 `tempdir/`, 不用逐个去改源码。

    只在当前进程 + 它拉起的子进程内生效, **不动用户系统环境变量**。

    已实测 (2026-10-28, 查证过才写这条): 项目根放 `sitecustomize.py` **不会**被自动导入
    (Python 只从 `sys.path` 里找 sitecustomize, cwd 不在启动期的 sys.path 里),
    所以不能靠它兜底 —— 必须在真正跑起来的入口里显式调用本函数。
    """
    d = temp_root(sub)
    tempfile.tempdir = str(d)              # 影响本进程内所有没传 dir 的 mkdtemp/mkstemp
    for key in ("TEMP", "TMP", "TMPDIR"):  # 影响子进程 (subprocess / C 扩展读环境变量)
        os.environ[key] = str(d)
    return d
