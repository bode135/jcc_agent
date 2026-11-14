"""把一帧画面组合成 Agent 可直接喂的 Markdown 局面快照 (命令行入口).

只读: 只截图 + 识别 + 排版, 不点鼠标键盘, 不改游戏状态。

输出四张表 (用户口径) + 标题区 + 玩家血量:
    ## 商店 / ## 场上棋子 / ## 备战席棋子 / ## 装备栏

用法:
    # 离线图 -> 直接打印 Markdown
    python -X utf8 -u -m scripts.frame_prompt --img tests/images/board_ingame.png

    # 落盘给 Agent 用 (可与 --no-players 搭配精简)
    python -X utf8 -u -m scripts.frame_prompt --img tempdir/frame_4-5.png --out tempdir/prompt.md

    # 实机截一帧 (只读, 不操作游戏)
    python -X utf8 -u -m scripts.frame_prompt --ld-index 0

    # 机器可读
    python -X utf8 -u -m scripts.frame_prompt --img frame.png --format json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from extract_info.prompt import snapshot, to_json, to_markdown

SECTIONS = ("shop", "board", "bench", "equip_bar", "players")


def load_image(args) -> "object":
    """--img 读文件 (并等比缩放到 1920×1080 基准) / 否则实机截一帧."""
    from PIL import Image

    from config.config import default_config

    target = tuple(default_config.DEFAULT_RESOLUTION)

    if args.img:
        img = Image.open(args.img).convert("RGB")
        if tuple(img.size) != target:
            print(f"[i] 输入 {img.size[0]}x{img.size[1]} 不是基准 {target[0]}x{target[1]}, "
                  f"等比缩放到基准后识别")
            img = img.resize(target, Image.LANCZOS)
        return img

    from capture.screen import capture_game
    if args.ld_index is not None:
        from capture.window import resolve_window
        win, why = resolve_window(str(args.ld_index))
        if not win:
            raise SystemExit(f"[X] 没找到雷电窗口 (ld_index={args.ld_index!r}): {why}")
        print(f"[i] 窗口: {win.get('title') or win.get('class')} hwnd={win.get('hwnd')}")
    return capture_game()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="一帧 -> Agent 局面对局快照 (Markdown/JSON, 只读不操作)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--img", default=None, metavar="PATH",
                        help="离线图 (非 1920×1080 会先等比缩放到基准)")
    parser.add_argument("--ld-index", default=None, metavar="N",
                        help="实机: 雷电模拟器序号, 截一帧 (只读)")
    parser.add_argument("--format", choices=("md", "json"), default="md",
                        help="输出格式 (默认 md)")
    parser.add_argument("--out", default=None, metavar="PATH",
                        help="写到文件而不是 stdout")
    parser.add_argument("--only", default=None, metavar="SHOP,BOARD,BENCH,EQUIP_BAR",
                        help="只输出部分段落 (逗号分隔): " + "/".join(SECTIONS))
    parser.add_argument("--no-players", action="store_true",
                        help="不输出玩家血量那段 (精简 prompt 时用)")
    parser.add_argument("--timings", action="store_true", help="打印每步耗时")
    args = parser.parse_args()

    if args.img and args.ld_index is not None:
        parser.error("--img 与 --ld-index 不能同时使用")

    only: set[str] | None = None
    if args.only:
        keys = {k.strip().lower() for k in args.only.split(",") if k.strip()}
        unknown = keys - set(SECTIONS)
        if unknown:
            parser.error(f"未知段落 {sorted(unknown)}; 可选: {', '.join(SECTIONS)}")
        # board_equips 跟着 board/bench 一起取 (装备挂在棋子上)
        only = set(keys)
        if keys & {"board", "bench"}:
            only.add("board_equips")
    if args.no_players and only is not None:
        only.discard("players")

    t0 = time.perf_counter()
    img = load_image(args)
    timings: dict = {}
    errors: dict = {}
    snap = snapshot(img, only=only, timings=timings, errors=errors)
    if args.no_players:
        snap.pop("players", None)

    if args.format == "json":
        text = json.dumps(to_json(snap), ensure_ascii=False, indent=2)
    else:
        stage = (snap.get("game") or {}).get("stage")
        text = to_markdown(snap, title=None, with_players=not args.no_players)
        if stage:
            pass

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
        print(f"[OK] 已写入 {out} ({len(text)} 字符)")
    else:
        print(text)

    if errors:
        print("[!] 有步骤失败 (其余部分照常输出):", file=sys.stderr)
        for k, v in errors.items():
            print(f"    {k}: {v}", file=sys.stderr)
    if args.timings:
        total = round(time.perf_counter() - t0, 2)
        detail = " ".join(f"{k}={v}s" for k, v in timings.items() if not k.startswith("game."))
        print(f"[耗时] {detail} total={total}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())