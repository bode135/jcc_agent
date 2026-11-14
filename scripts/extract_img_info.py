"""对局信息抽取: 指定一张静态图片 (或实时截图), 提取其中的结构化对局信息.

用法 (项目根目录, 必须带 -X utf8 以免中文输出乱码):
    python -X utf8 -u -m scripts.extract_img_info                          # 实时截图抽取
    python -X utf8 -u -m scripts.extract_img_info --img tests/images/board_ingame.png
    python -X utf8 -u -m scripts.extract_img_info --img x.png --format json # 终端输出 JSON
    python -X utf8 -u -m scripts.extract_img_info --live --loop 2          # 每 2 秒实时截图抽一次 (Ctrl+C 结束)
    python -X utf8 -u -m scripts.extract_img_info --img x.png --only game,shop --no-save

输出 (默认 table 表格; --format json 输出机器可读 JSON):
    game      : 8 个全局字段 + 2 个判定字段 (phase 对局阶段 / hex 海克斯选择状态)
    players   : 右侧玩家列表 (ID/血量/区域)
    shop      : 商店 5 张卡牌 (棋子名/价格/区域)
    little_legend: 小小英雄坐标 (棋盘/备战席)
    hextech   : 海克斯强化符文卡 (仅海克斯选择阶段-未选择时有内容)

结果同时保存为 JSON 到 --out 目录 (默认 config.EXTRACT_INFO_DIR):
    img_info__<图名>.json (--img) / img_info__live.json (实时截图); --no-save 不落盘.

耗时: 每次抽取结束打印一行 [耗时], 其中 ocr.recognize 即"整图 OCR 一次"的耗时.
"""

# 直接运行支持: `python scripts/extract_img_info.py ...` 与 `python -m scripts.extract_img_info ...` 等价。
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import time
from pathlib import Path

from PIL import Image

from capture.screen import capture_game, resize
from config.config import default_config
from extract_info import extract
from extract_info.extractor import ALL_GROUPS
from recognize import augment, board, phase

# 要展示的信息: {键: 中文注释}; --only 只抽其中列出的键
SHOW_INFOS = {
    "game": "当前游戏信息 (回合/剩余时间/等级/经验/金币/阶段/海克斯)",
    "players": "玩家信息 (ID/血量)",
    "shop": "商店信息 (棋子名/价格)",
    "little_legend": "小小英雄坐标",
    "hextech": "海克斯强化符文卡 (仅海克斯选择阶段-未选择时有内容)",
}

# 表格输出时的列定义: {信息键: [(字段, 中文列名)]}
TABLE_COLS = {
    "game": [("key", "键"), ("value", "值"), ("rect", "区域")],
    "players": [("id", "ID"), ("hp", "血量"), ("rect", "区域")],
    "shop": [("name", "棋子"), ("price", "价格"), ("rect", "区域")],
    "little_legend": [("region", "位置"), ("name", "名字"), ("score", "置信度"), ("center", "中心点")],
    "hextech": [("card_no", "卡号"), ("name", "card_name"), ("desc", "card_desc"),
                ("refresh_left", "refresh_times"), ("center", "click_center")],
}

# 对局阶段中文标注: 公共映射在 recognize/phase.py (extract_info 的 phase 键共用)
PHASE_NOTES = phase.PHASE_NOTES

# ---- 默认参数: 直接改这里 (命令行参数优先) ----
IMG_PATH = ""         # 要抽取的图片路径, 如 "tests/images/board_ingame.png"; 留空=实时截图
FORMAT = "table"      # 终端输出格式: "json" 或 "table" (JSON 文件始终保存, 不受影响)
# ---------------------------------------------


def load_img(path: str | None):
    """加载静态图并缩放到 1920×1080 基准; 路径为空则实时截图 (capture_game + resize)."""
    if path:
        img = Image.open(path).convert("RGB")
        if img.size != default_config.DEFAULT_RESOLUTION:
            img = img.resize(default_config.DEFAULT_RESOLUTION, Image.LANCZOS)
        return img
    return resize(capture_game())


def _disp_width(s: str) -> int:
    """终端显示宽度: 中文等全角字符按 2 列计."""
    return sum(2 if ord(ch) > 127 else 1 for ch in s)


def _fmt_cell(v) -> str:
    """单元格值紧凑显示: None→'-', 列表→'(a, b)', 浮点保留 2 位."""
    if v is None:
        return "-"
    if isinstance(v, (list, tuple)):
        nums = [f"{int(n)}" if isinstance(n, float) and n.is_integer() else _fmt_cell(n) for n in v]
        return "(" + ", ".join(nums) + ")"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def print_table(rows: list[dict], cols: list[tuple[str, str]]) -> None:
    """把 list[dict] 打成对齐表格. cols: [(字段, 中文列名)], 中英文混排按显示宽度对齐."""
    if not rows:
        print("(空)")
        return
    heads = [c for _, c in cols]
    body = [[_fmt_cell(r.get(k)) for k, _ in cols] for r in rows]
    widths = [max(_disp_width(h), *(_disp_width(row[i]) for row in body))
              for i, h in enumerate(heads)]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(cells: list[str]) -> str:
        return "|" + "|".join(f" {c}{' ' * (w - _disp_width(c))} " for c, w in zip(cells, widths)) + "|"

    print(sep)
    print(line(heads))
    print(sep)
    for row in body:
        print(line(row))
    print(sep)


def _safe(label: str, fn, timings: dict, errors: dict):
    """执行一个识别步骤: 成功返回结果并记耗时, 失败返回 None 并记错误 (不向外抛)."""
    t0 = time.perf_counter()
    try:
        out = fn()
        timings[label] = round(time.perf_counter() - t0, 3)
        return out
    except Exception as exc:  # noqa: BLE001 - 单个环节失败不应中断整次抽取
        timings[label] = round(time.perf_counter() - t0, 3)
        errors[label] = f"{type(exc).__name__}: {exc}"
        return None


def _narrow_or_all(sections: set[str]) -> list[str] | None:
    """把展示分组映射成 extract_info 的抽取分组; 只关心小小英雄/海克斯时不抽三组."""
    groups = [g for g in ALL_GROUPS if g in sections]
    return groups or None


def run_once(args, sections: set[str], iteration: int) -> dict:
    """抽一次 (静态图或实时截图), 打印结果与耗时, 返回本次 shown 字典."""
    timings: dict[str, float] = {}
    errors: dict[str, str] = {}
    t_all = time.perf_counter()
    want_phase = "game" in sections
    want_aug = ("game" in sections) or ("hextech" in sections)

    # 1. 取图
    t0 = time.perf_counter()
    try:
        img = load_img(args.img)
    except Exception as exc:  # noqa: BLE001 - 截图/读图失败给出可读信息
        print(f"[X] 取图失败: {type(exc).__name__}: {exc}")
        raise
    timings["load_img"] = round(time.perf_counter() - t0, 3)

    # 2. 三组结构化信息 (整图一次 OCR, 多字段共用)
    info: dict = {}
    groups = _narrow_or_all(sections)
    if groups:
        info = extract(img, only=groups, timings=timings, errors=errors)

    # 3. 对局阶段 + 海克斯选择状态 (插在 game 的 stage 之后, 第二/三行, 阶段一眼可见)
    if want_phase:
        t0 = time.perf_counter()
        ph = _safe("phase.detect", lambda: phase.detect(img), timings, errors)
        if ph:
            game_rows = info.setdefault("game", [])
            game_rows[1:1] = [
                {"key": "phase", "value": PHASE_NOTES.get(ph["phase"], ph["phase"]),
                 "rect": list(phase.TRACKER_REGION)},
                {"key": "hex", "value": None,
                 "rect": list(phase.CARD_ICON_REGION)},
            ]
            print(f"阶段判定: {ph['phase']} (剑 {ph['sword_score']} / 卡牌 {ph['card_score']})")
    if want_aug:
        aug = _safe("augment.extract", lambda: augment.extract(img), timings, errors)
        if aug:
            # hex 值依赖 augment 判定 (is_augment/chosen), 回填 game 中的 hex 行
            for row in info.get("game", []):
                if row.get("key") == "hex":
                    row["value"] = ("hex_1 海克斯阶段-已选择海克斯" if aug["chosen"]
                                    else "hex_0 海克斯阶段-未选择海克斯") if aug["is_augment"] else None
            info["hextech"] = [{"card_no": i, **card} for i, card in enumerate(aug["cards"], 1)]
            print(f"海克斯: is_augment={aug['is_augment']}, chosen={aug['chosen']} "
                  f"(眼睛 {aug['eye_score']})")

    # 4. 小小英雄坐标: board.extract_units 默认过滤小小英雄, 此处单独开启 (棋盘+备战席合并)
    if "little_legend" in sections:
        units = _safe("board.extract_units", lambda: board.extract_units(img, include_legend=True),
                      timings, errors)
        if units:
            info["little_legend"] = [
                {**u, "region": region}
                for region in ("board", "bench") for u in units[region] if u.get("legend")
            ]
    if "hextech" in sections:
        info.setdefault("hextech", [])

    shown = {key: info.get(key, []) for key in SHOW_INFOS if key in sections}

    # 5. 打印
    if args.format == "json":
        for key in shown:
            print(f"\n=== {key}  # {SHOW_INFOS[key]} ===")
            print(json.dumps(shown[key], ensure_ascii=False, indent=2))
    else:
        for key in shown:
            print(f"\n=== {key}  # {SHOW_INFOS[key]} ===")
            if key == "hextech" and want_aug and errors.get("augment.extract") is None \
                    and not info.get("hextech") and shown.get("game"):
                print("(空) 已完成海克斯选择或无卡牌可抽")
            else:
                print_table(shown[key], TABLE_COLS[key])

    for label, msg in errors.items():
        print(f"[X] {label} 抽取失败: {msg}")

    # 6. 耗时 (ocr.recognize 即"整图 OCR 一次"的耗时)
    steps = " ".join(f"{k}={v:.2f}s" for k, v in timings.items())
    total = round(time.perf_counter() - t_all, 3)
    print(f"[耗时] {steps} total={total:.2f}s")

    # 7. 落盘 (只存本次展示的分组)
    if not args.no_save:
        image_name = (args.img.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
                      if args.img else "live")
        out_dir = Path(args.out) if args.out else Path(default_config.EXTRACT_INFO_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"img_info__{image_name}.json"
        out.write_text(json.dumps(shown, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"来源图片: {args.img or '(实时截图)'}")
        print(f"结果已保存: {out}")
    print(f"[OK] 第 {iteration} 次抽取完成, {len(shown)} 个分组 "
          f"({', '.join(shown) if shown else '无'})")
    return shown


def _parse_only(raw: str | None) -> set[str]:
    """--only: 逗号分隔的分组名 -> set; 未知分组报错 (拼错就早早失败, 不静默忽略)."""
    if not raw:
        return set(SHOW_INFOS)
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    unknown = keys - set(SHOW_INFOS)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"未知分组 {sorted(unknown)}; 可选: {', '.join(SHOW_INFOS)}")
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="提取截图中的结构化对局信息 (纯图色: 整图一次 OCR + 区域切片)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--img", default=IMG_PATH or None,
                        help=f"用已有静态图片抽取 (默认: 代码内 IMG_PATH={IMG_PATH!r}, 为空则实时截图)")
    parser.add_argument("--live", action="store_true",
                        help="实时截图 (capture.screen.capture_game()+resize(); 与 --img 互斥)")
    parser.add_argument("--format", choices=("json", "table"), default=FORMAT,
                        help=f"终端输出格式 (默认: 代码内 FORMAT={FORMAT!r}; JSON 文件始终保存)")
    parser.add_argument("--out", default=None, metavar="DIR",
                        help="JSON 落盘目录 (默认: config.EXTRACT_INFO_DIR)")
    parser.add_argument("--no-save", action="store_true", help="只打印, 不落盘 JSON")
    parser.add_argument("--only", default=None, metavar="GAME,PLAYERS,SHOP",
                        help="只抽部分分组 (逗号分隔): game/players/shop/little_legend/hextech; "
                             "默认全抽. 只抽 shop 时走窄带快路径 (不跑整图 OCR)")
    parser.add_argument("--loop", type=float, default=0.0, metavar="SEC",
                        help="每 SEC 秒抽一次, 连续打印 (0=只抽一次; 用于调试/被动观察)")
    parser.add_argument("--loop-count", type=int, default=0, metavar="N",
                        help="配合 --loop: 最多抽 N 次后退出 (0=无限, Ctrl+C 结束)")
    args = parser.parse_args()

    try:
        sections = _parse_only(args.only)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if args.img and args.live:
        parser.error("--img 与 --live 不能同时使用 (静态图 / 实时截图二选一)")
    if args.loop < 0:
        parser.error("--loop 必须 >= 0 秒 (0 = 只抽一次)")
    if args.loop_count < 0:
        parser.error("--loop-count 必须 >= 0 (0 = 无限)")

    live = args.live or not args.img
    print(f"--- on extract, img={args.img or '(live)'} live={live} "
          f"sections={','.join(SHOW_INFOS.keys() & sections) or '无'} "
          f"loop={args.loop or 'off'} ---")
    t_start = time.time()
    iteration = 0
    try:
        while True:
            iteration += 1
            if iteration > 1 or args.loop:
                print(f"===== 第 {iteration} 次 {time.strftime('%H:%M:%S')} =====")
            try:
                run_once(args, sections, iteration)
            except Exception as exc:  # noqa: BLE001 - 实时循环里单次失败不退出 (等下一次)
                if args.loop or args.loop_count:
                    print(f"[X] 本次抽取失败, 继续下一轮: {type(exc).__name__}: {exc}")
                else:
                    print(f"[X] 抽取失败: {type(exc).__name__}: {exc}")
                    return 1
            if not args.loop:
                break
            if args.loop_count and iteration >= args.loop_count:
                break
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print(f"\n[X] 已中断 (Ctrl+C), 共 {iteration} 次, 总 {time.time() - t_start:.1f}s")
    print(f"[OK] 结束, 共 {iteration} 次, 总耗时 {time.time() - t_start:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
