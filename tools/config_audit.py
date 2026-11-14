"""config 用量审计 —— 找出 config/config.py 里「本工程实际没人读」的配置键。

用途: 源项目的 config.py 是从更早的大工程里长出来的 (1378 行 / 400+ 键), 图色方案脚手架
只用到其中一部分。这个脚本静态扫一遍全工程 (AST + 字符串, 不 import), 统计
`default_config.<KEY>` 与 `getattr(default_config, "<KEY>")` 两种读法, 输出:
  - 有用键清单 (带引用次数)
  - 从未被读的键清单 (候选: 可删 / 说明为什么留)

跑法: python -X utf8 -u tools/config_audit.py [--show-unused]
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = PROJECT_ROOT / "config" / "config.py"
SKIP_DIRS = {"tempdir", "__pycache__", ".git", "models", "workspace"}


def scan_references() -> tuple[Counter, Counter]:
    """返回 (default_config.<KEY> 计数, getattr 字符串键计数)。"""
    attr_refs: Counter = Counter()
    str_refs: Counter = Counter()
    pat_attr = re.compile(r"default_config\.([A-Za-z_][A-Za-z0-9_]*)")
    pat_getattr = re.compile(r"getattr\(\s*(?:\w+\.)?default_config\s*,\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']")
    for path in PROJECT_ROOT.rglob("*.py"):
        if SKIP_DIRS & set(path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in pat_attr.finditer(text):
            attr_refs[m.group(1)] += 1
        for m in pat_getattr.finditer(text):
            str_refs[m.group(1)] += 1
    return attr_refs, str_refs


def scan_config_keys() -> list[str]:
    """从 config.py 的 class 体里收集全大写赋值键名 (不 import, 避免副作用)。"""
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    keys: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    keys.append(target.id)
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description="config 用量审计")
    parser.add_argument("--show-unused", action="store_true", help="打印从未被读的键 (默认只打统计)")
    args = parser.parse_args()

    attr_refs, str_refs = scan_references()
    all_refs = attr_refs + str_refs
    cfg_keys = scan_config_keys()
    unused = [k for k in cfg_keys if k not in all_refs]

    print(f"[OK] config/config.py 定义 {len(cfg_keys)} 个键")
    print(f"[OK] 工程内被读取 {len(all_refs)} 个键 (共 {sum(all_refs.values())} 处引用)")
    print(f"[!] 从未被读取 {len(unused)} 个键")
    top = all_refs.most_common(20)
    print("[TOP20] " + ", ".join(f"{k}={n}" for k, n in top))
    if args.show_unused:
        for k in unused:
            print(f"  unused: {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
