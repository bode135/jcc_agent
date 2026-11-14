"""一键自检 —— 开箱验证这个脚手架是否完整可用 (不点游戏、不改游戏, 纯本地).

分三段:
  [1] 完整性: 目录/权重/templates/数据文件是否齐, 有没有漏拷
  [2] 干净度: 全工程源码的 **import 行** 里是否还残留父项目的内存/frida/sam3 依赖 (应为 0 命中;
      注释/docstring 里提到这些模块名是历史说明, 不算残留, 只统计数量)
  [3] 可导入: 逐个 import 所有包模块 (不需要模拟器窗口)

跑法 (工程根目录):
    python -X utf8 -u tools/selfcheck.py            # 只跑上面三段 (约 10s)
    python -X utf8 -u tools/selfcheck.py --full     # 追加跑四个功能自测 (约 100s, 需要模型可用)

退出码: 0 全通过 / 1 有失败项.
"""
from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MARKS = ("[OK]", "[X]", "[!]")
FAILS: list[str] = []


def ok(msg: str) -> None:
    print(f"{MARKS[0]} {msg}")


def bad(msg: str) -> None:
    FAILS.append(msg)
    print(f"{MARKS[1]} {msg}")


def warn(msg: str) -> None:
    print(f"{MARKS[2]} {msg}")


# ---------------------------------------------------------------- [1] 完整性
REQUIRED_FILES = [
    "config/config.py",
    "models/yolo_v2.1_balls.onnx",
    "models/yolo_v4.1_units_11n.onnx",
    "train_yolo/classes.json",
    "recognize/templates/health_bar.png",
    "recognize/templates/phase_swords.png",
    "recognize/templates/phase_cards.png",
    "recognize/templates/augment_eye.png",
    "recognize/templates/augment_refresh.png",
    "workspace/scrape_info/v18.3b/chess_info/items.json",
    "workspace/scrape_info/v18.3b/chess_info/item_recipes.json",
    "config/equip_id_map.json",
    "config/equip_name_map.json",
    "scripts/game.py",
    "scripts/extract_img_info.py",
    "scripts/overlay_info.py",
    "control/fallback.py",
    "extract_info/extractor.py",
    "overlay/debug_overlay.py",
    "overlay/live_draw.py",
]

REQUIRED_DIRS = [
    "recognize/templates/balls",
    "recognize/templates/equipment",
    "recognize/templates/equipment_game",
    "recognize/templates/digits",
    "recognize/templates/bench_items",
    "recognize/templates/game_over",
    "recognize/templates/reward_ball",
    "workspace/scrape_info/v18.3b/images/items",
    "units/templates_alpha",
    "tests/images",
]


def check_assets() -> None:
    for rel in REQUIRED_FILES:
        p = PROJECT_ROOT / rel
        if not p.is_file():
            bad(f"缺文件 {rel}")
        elif p.stat().st_size == 0:
            bad(f"空文件 {rel}")
    for rel in REQUIRED_DIRS:
        p = PROJECT_ROOT / rel
        if not p.is_dir():
            bad(f"缺目录 {rel}")
        elif not any(p.iterdir()):
            bad(f"空目录 {rel}")
    if not FAILS:
        ok(f"完整性: {len(REQUIRED_FILES)} 个关键文件 + {len(REQUIRED_DIRS)} 个资源目录齐全")
    # 权重不在 git 里 (走 Release 附件, 见 models/README.md), 缺了要指路怎么拉
    for rel, min_mb in (("models/yolo_v2.1_balls.onnx", 8.0), ("models/yolo_v4.1_units_11n.onnx", 8.0)):
        p = PROJECT_ROOT / rel
        if not p.is_file():
            bad(f"缺权重 {rel} —— 跑 python -m tools.fetch_models (权重走 Release 附件, 不进 git)")
            continue
        mb = p.stat().st_size / 1024 / 1024
        if mb < min_mb:
            bad(f"{rel} 只有 {mb:.1f}MB (< {min_mb}MB), 疑似 LFS 指针/下载不完整")
        else:
            ok(f"权重 {rel} = {mb:.1f}MB")


# ---------------------------------------------------------------- [2] 干净度
# 判据只看 **import 行**: 父项目的内存/frida/sam3 依赖必须一行都不剩.
# 注释与 docstring 里提这些名字属于历史说明 (例如「源项目这条链路内存优先, 本工程没有该模块」),
# 不算残留, 单独计数提示.
BANNED = [
    "trae_agent",
    "frida",
    "openai",
    "ocr_server",
    "mem_provider",
    "draft_ring",
    "ball_mem_collect",
    "sam3_client",
    "recognize.bar_less",
    "recognize.population",
    "augment_state",
]
IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))")


def iter_sources() -> list[Path]:
    skip = {"tempdir", "__pycache__", ".git", "models", "workspace", "tests"}
    out = []
    for p in PROJECT_ROOT.rglob("*.py"):
        if skip & set(p.parts):
            continue
        out.append(p)
    return out


def check_clean() -> None:
    import_hits = 0
    mentions = 0
    for p in iter_sources():
        rel = p.relative_to(PROJECT_ROOT).as_posix()
        for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            m = IMPORT_RE.match(line)
            if m:
                mod = m.group(1) or m.group(2) or ""
                for token in BANNED:
                    if token in mod:
                        import_hits += 1
                        bad(f"残留 import {token!r} -> {rel}:{lineno}")
            elif any(token in line for token in BANNED):
                mentions += 1
    if not import_hits:
        ok(f"干净度: {len(iter_sources())} 个 .py 的 import 行无父项目内存/frida/sam3 依赖残留")
    if mentions:
        warn(f"干净度: 另有 {mentions} 处注释/docstring 提到这些模块名 (历史说明, 不影响运行)")


# ---------------------------------------------------------------- [3] 可导入
MODULES = [
    "config.config",
    "recognize.ocr", "recognize.ocr_client", "recognize.regions", "recognize.grid",
    "recognize.board", "recognize.equipment", "recognize.yolo_units", "recognize.extract",
    "recognize.reward_ball", "recognize.phase", "recognize.augment", "recognize.game_over",
    "recognize.yolo_trust",
    "units.db", "units.matcher", "train_yolo.class_table",
    "utils.overlay_draw", "utils.template_match", "utils.unit_names", "utils.temp_dirs",
    "utils.mem_guard", "utils.portrait_match", "utils.reward_ball", "utils.show_img",
    "utils.tt_log", "utils.no_window",
    "capture.screen", "capture.window", "capture.save",
    "control.input", "control.mouse", "control.fallback",
    "extract_info", "extract_info.extractor",
    "overlay.debug_overlay", "overlay.live_draw",
]


def check_imports() -> None:
    failed = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 自检要把所有失败都列出来
            failed.append((name, f"{type(exc).__name__}: {exc}"))
    for name, why in failed:
        bad(f"导入失败 {name}: {why}")
    if not failed:
        ok(f"可导入: {len(MODULES)} 个模块全部 import 成功")


# ---------------------------------------------------------------- [4] 功能自测
SELFTESTS = [
    ("tools.selftest_recognize", ["-m"]),
    ("tools.selftest_extract", []),
    ("tools.selftest_overlay", []),
    ("tools.selftest_prompt", []),
    ("tools.selftest_ops", ["--live"]),
]


def run_selftests() -> None:
    py = sys.executable
    for target, extra in SELFTESTS:
        cmd = [py, "-X", "utf8", "-u"]
        cmd += ["-m", target] if extra[:1] == ["-m"] else [str(PROJECT_ROOT / f"{target.replace('.', '/')}.py")]
        cmd += extra[1:] if extra[:1] == ["-m"] else extra
        print(f"\n==== {target} ====", flush=True)
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT))
        if proc.returncode != 0:
            bad(f"{target} 退出码 {proc.returncode}")
        else:
            ok(f"{target} 通过")


# ---------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser(description="工程一键自检 (完整性/干净度/可导入/功能自测)")
    parser.add_argument("--full", action="store_true", help="追加跑四个功能自测 (约 100s)")
    args = parser.parse_args()

    print("== [1] 完整性 ==")
    check_assets()
    print("\n== [2] 干净度 ==")
    check_clean()
    print("\n== [3] 可导入 ==")
    check_imports()
    if args.full:
        print("\n== [4] 功能自测 ==")
        run_selftests()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"[X] 自检未通过, {len(FAILS)} 项失败:")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("[OK] 自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
