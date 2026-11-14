"""分析源项目 (jcc_robot) 的模块 import 闭包, 用来确定"纯图色脚手架"要搬哪些文件.

区分两类依赖:
  - 模块级 import: 必须搬 (否则 import 就失败)
  - 函数内 import (惰性): 只在某条代码路径才用到, 可选搬; 单独列出来供人工判断

用法 (在源项目根跑):
    python -X utf8 tools/import_closure.py --src-root <jcc_robot 根>

只读源码, 不做任何修改.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import deque
from pathlib import Path

INTERNAL_PKGS = {
    "config", "capture", "control", "recognize", "units", "train_yolo",
    "utils", "extract_info", "overlay", "scripts",
}

ENTRYPOINTS = [
    "extract_info.extractor",
    "recognize.ocr", "recognize.regions", "recognize.grid",
    "recognize.extract", "recognize.board", "recognize.equipment", "recognize.yolo_units",
    "recognize.reward_ball", "recognize.phase", "recognize.augment", "recognize.game_over",
    "recognize.yolo_trust",
    "units.db", "units.matcher", "train_yolo.class_table",
    "utils.reward_ball", "utils.show_img", "utils.template_match", "utils.unit_names",
    "utils.temp_dirs", "utils.overlay_draw", "utils.tt_log",
    "capture.screen", "capture.window", "capture.save",
    "control.input", "control.mouse",
    "overlay.debug_overlay", "overlay.live_draw",
]


def module_path(src_root: Path, dotted: str) -> Path | None:
    parts = dotted.split(".")
    pkg = src_root.joinpath(*parts)
    if pkg.with_suffix(".py").is_file():
        return pkg.with_suffix(".py")
    init = pkg / "__init__.py"
    if init.is_file():
        return init
    return None


def dotted_of(py_file: Path, src_root: Path) -> str:
    rel = py_file.relative_to(src_root).with_suffix("")
    return ".".join(rel.parts)


def _resolve_relative(node: ast.ImportFrom, pkg_dir: str) -> str:
    base = node.module or ""
    if not node.level:
        return base
    up = pkg_dir.split(".") if pkg_dir else []
    keep = up[:len(up) - (node.level - 1)] if node.level > 1 else up
    return ".".join([*keep, base] if base else keep)


def imports_of(py_file: Path, src_root: Path) -> tuple[list[str], list[str]]:
    """返回 (模块级 import 目标, 函数内惰性 import 目标), 含 from X import a 的 X.a."""
    tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
    mod = dotted_of(py_file, src_root)
    pkg_dir = mod if py_file.name == "__init__.py" else mod.rsplit(".", 1)[0]

    top: list[str] = []
    lazy: list[str] = []

    def emit(node, bucket: list[str]) -> None:
        if isinstance(node, ast.Import):
            bucket.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(node, pkg_dir)
            if not base:
                return
            bucket.append(base)
            bucket.extend(f"{base}.{a.name}" for a in node.names if a.name != "*")

    for node in tree.body:                      # 模块顶层语句
        emit(node, top)
        for sub in ast.walk(node):              # 顶层的 try/if 里也算模块级
            if sub is not node:
                emit(sub, top)
    for node in ast.walk(tree):                 # 函数体内的 import
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for sub in ast.walk(node):
                emit(sub, lazy)
    return top, lazy


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-root", default=r"E:\projects\pycharmProjects\jcc_robot")
    args = ap.parse_args()
    src_root = Path(args.src_root).resolve()

    seen: dict[str, Path] = {}
    external: set[str] = set()
    lazy_external: set[str] = set()
    lazy_only: dict[str, set[str]] = {}          # 仅出现在惰性 import 里的内部模块 -> 谁用
    order: list[str] = []

    queue = deque(ENTRYPOINTS)
    queued = set(ENTRYPOINTS)
    while queue:
        dotted = queue.popleft()
        top_pkg = dotted.split(".")[0]
        if top_pkg not in INTERNAL_PKGS:
            external.add(top_pkg)
            continue
        path = module_path(src_root, dotted)
        if path is None or dotted in seen:
            continue
        seen[dotted] = path
        order.append(dotted)
        top, lazy = imports_of(path, src_root)
        for dep in top + lazy:
            if dep.split(".")[0] not in INTERNAL_PKGS:
                continue
            cand = dep if module_path(src_root, dep) else dep.split(".")[0]
            if cand not in queued:
                queued.add(cand)
                queue.append(cand)
        for dep in lazy:
            if dep.split(".")[0] not in INTERNAL_PKGS:
                lazy_external.add(dep.split(".")[0])
                continue
            cand = dep if module_path(src_root, dep) else dep.split(".")[0]
            lazy_only.setdefault(cand, set()).add(dotted)

    print(f"=== 内部模块闭包 (含惰性): {len(seen)} 个 ===")
    total = 0
    for dotted in order:
        p = seen[dotted]
        size = p.stat().st_size
        total += size
        note = "  [仅惰性引用]" if dotted in lazy_only and all(
            dotted not in _top_deps(src_root, x)[0] for x in lazy_only[dotted]) else ""
        print(f"{size:>9}  {p.relative_to(src_root).as_posix()}{note}")
    print(f"\n合计 {total} bytes ({total / 1024:.0f} KB)")

    print(f"\n=== 外部第三方依赖 (模块级+惰性) ===")
    print(" ".join(sorted(external | lazy_external)))

    print(f"\n=== 只被惰性 import 包住的内部模块 ({len(lazy_only)}) ===")
    for mod, users in sorted(lazy_only.items()):
        print(f"  {mod}  <- {', '.join(sorted(users))}")
    return 0


def _top_deps(src_root: Path, dotted: str) -> tuple[list[str], list[str]]:
    p = module_path(src_root, dotted)
    return imports_of(p, src_root) if p else ([], [])


if __name__ == "__main__":
    raise SystemExit(main())
