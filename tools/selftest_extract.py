"""图色抽取脚手架自测: 校验静态图抽取的键值/条数与 JSON 落盘.

跑法 (项目根目录):
    python -X utf8 -u tools/selftest_extract.py
    python -X utf8 -u -m tools.selftest_extract            # 等价 (tools/ 为命名空间包)
    python -X utf8 -u tools/selftest_extract.py --no-cli   # 只测 API, 不跑 CLI 落盘 (省 4s)

校验项 (每项打印 [OK]/[X], 任一 [X] 退出码 1):
  * CLI --help 可用, 且含 --img/--live/--format/--out/--no-save/--only/--loop
  * board_ingame.png: game 8 键齐全且关键值可读; players 8 行血量可读 (ID 至少 7/8);
    shop 5 张卡牌名称+价格齐全
  * augment_choose.png: 阶段=海克斯选择, hex 已判定, hextech 3 张卡牌有名字
  * board_green_model.png: 非对局图不抛异常, 三组结构完整 (空值由 [X] 行如实报告)
  * CLI 落盘 JSON: 合法 JSON, 3 个分组均为 list, 中文不转义 (utf-8 原文, 非 \\uXXXX)
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def _bootstrap() -> None:
    """以脚本方式运行时把项目根加入 sys.path (以 -m 包方式运行时无需)."""
    if __package__ in (None, ""):
        sys.path.insert(0, str(ROOT))


_bootstrap()

from config.config import default_config  # noqa: E402
from extract_info import extract  # noqa: E402
from extract_info.extractor import ALL_GROUPS, GAME_KEYS  # noqa: E402
from recognize import augment, phase  # noqa: E402

PY = sys.executable
_checks: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记录并打印一条断言; 返回 ok 便于组合."""
    if not ok:
        _checks.append(name)
    print(f"[{'OK' if ok else 'X'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def load_img(path: Path) -> Image.Image:
    """与 CLI 同口径: 打开并缩放到 DEFAULT_RESOLUTION (LANCZOS)."""
    img = Image.open(path).convert("RGB")
    if tuple(img.size) != tuple(default_config.DEFAULT_RESOLUTION):
        img = img.resize(tuple(default_config.DEFAULT_RESOLUTION), Image.LANCZOS)
    return img


def test_cli_help() -> None:
    """CLI 参数表必须齐全 (回归保护: 别把 --live/--out 之类改没了)."""
    proc = subprocess.run([PY, "-X", "utf8", "-u", "-m", "scripts.extract_img_info", "--help"],
                          capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    out = (proc.stdout or "") + (proc.stderr or "")
    check("CLI --help 退出码 0", proc.returncode == 0, f"returncode={proc.returncode}")
    for flag in ("--img", "--live", "--format", "--out", "--no-save", "--only",
                 "--loop", "--loop-count"):
        check(f"CLI --help 含 {flag}", flag in out)


def test_board_ingame(img_dir: Path) -> None:
    """对局图: 三组信息全抽 (整图 OCR 一次) + 阶段判定."""
    img = load_img(img_dir / "board_ingame.png")
    timings: dict = {}
    errors: dict = {}
    info = extract(img, timings=timings, errors=errors)
    check("board_ingame: 抽取无异常", not errors, "; ".join(f"{k}={v}" for k, v in errors.items()))
    game = {r["key"]: r["value"] for r in info.get("game", [])}
    check("board_ingame: game 8 键齐全", set(game) == set(GAME_KEYS), str(sorted(game))
          if set(game) != set(GAME_KEYS) else "")
    for key in ("stage", "round_time", "level", "xp", "gold"):
        check(f"board_ingame: game.{key} 可读", game.get(key) is not None, f"值={game.get(key)!r}")
    check("board_ingame: xp_buy_cost/refresh_cost/free_refresh 可读",
          all(game.get(k) is not None for k in ("xp_buy_cost", "refresh_cost", "free_refresh")),
          f"值={[game.get(k) for k in ('xp_buy_cost', 'refresh_cost', 'free_refresh')]!r}")

    players = info.get("players", [])
    check("board_ingame: players 8 行", len(players) == 8, f"条数={len(players)}")
    check("board_ingame: players 血量全部可读", all(p["hp"] is not None for p in players),
          f"hp={[p['hp'] for p in players]!r}")
    n_ids = sum(1 for p in players if p["id"])
    check("board_ingame: players ID 至少 7/8 可读", n_ids >= 7, f"可读 {n_ids}/{len(players)}")

    shop = info.get("shop", [])
    check("board_ingame: shop 5 张卡牌", len(shop) == 5, f"条数={len(shop)}")
    check("board_ingame: shop 名称齐全", all(s["name"] for s in shop),
          f"名称={[s['name'] for s in shop]!r}")
    check("board_ingame: shop 价格齐全", all(s["price"] is not None for s in shop),
          f"价格={[s['price'] for s in shop]!r}")

    ph = phase.detect(img)
    check("board_ingame: 阶段判定非 unknown", ph["phase"] != "unknown", f"phase={ph['phase']}")
    print(f"[耗时] board_ingame: ocr.recognize={timings.get('ocr.recognize')}s "
          f"total≈{round(sum(v for v in timings.values()), 2)}s + phase {ph['phase']}")


def test_augment_choose(img_dir: Path) -> None:
    """海克斯选择图: 阶段/hex 判定 + hextech 3 张卡牌."""
    img = load_img(img_dir / "augment_choose.png")
    timings: dict = {}
    errors: dict = {}
    info = extract(img, timings=timings, errors=errors)
    check("augment_choose: 抽取无异常", not errors, "; ".join(f"{k}={v}" for k, v in errors.items()))
    game = {r["key"]: r["value"] for r in info.get("game", [])}
    check("augment_choose: game 8 键齐全", set(game) == set(GAME_KEYS))
    check("augment_choose: stage 可读", game.get("stage") is not None, f"值={game.get('stage')!r}")

    ph = phase.detect(img)
    check("augment_choose: phase=augment", ph["phase"] == "augment", f"phase={ph['phase']}")
    aug = augment.extract(img)
    check("augment_choose: is_augment=True 且未选择", bool(aug["is_augment"]) and not aug["chosen"],
          f"is_augment={aug['is_augment']} chosen={aug['chosen']}")
    cards = aug["cards"]
    check("augment_choose: hextech 3 张卡牌", len(cards) == 3, f"条数={len(cards)}")
    check("augment_choose: 卡牌均读到名字", all(c.get("name") for c in cards),
          f"名称={[c.get('name') for c in cards]!r}")
    print(f"[耗时] augment_choose: ocr.recognize={timings.get('ocr.recognize')}s "
          f"phase={round(ph['sword_score'], 3)}/{round(ph['card_score'], 3)}")


def test_board_green_model(img_dir: Path) -> None:
    """非对局图 (绿幕模型): 不抛异常, 结构完整 (空值不算失败, 只报告)."""
    img = load_img(img_dir / "board_green_model.png")
    timings: dict = {}
    errors: dict = {}
    info = extract(img, timings=timings, errors=errors)
    check("board_green_model: 抽取不抛异常", set(info) == set(ALL_GROUPS), f"分组={sorted(info)}")
    check("board_green_model: game 8 键结构齐全",
          [r["key"] for r in info.get("game", [])] == list(GAME_KEYS))
    ph = phase.detect(img)
    check("board_green_model: phase=unknown (非对局画面)", ph["phase"] == "unknown",
          f"phase={ph['phase']}")
    print(f"[耗时] board_green_model: ocr.recognize={timings.get('ocr.recognize')}s; "
          f"errors={list(errors) or '无'}")


def test_cli_json(out_dir: Path) -> None:
    """CLI 静态图跑通 + JSON 落盘: 合法 JSON / list 结构 / 中文不转义."""
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [PY, "-X", "utf8", "-u", "-m", "scripts.extract_img_info",
         "--img", "tests/images/board_ingame.png", "--format", "json",
         "--out", str(out_dir)],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    check("CLI 静态图退出码 0", proc.returncode == 0,
          f"returncode={proc.returncode} stderr={proc.stderr[-300:]!r}")
    js = out_dir / "img_info__board_ingame.json"
    check("CLI 落盘 JSON 存在", js.exists(), str(js))
    if js.exists():
        raw = js.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
            ok_json = True
        except Exception as exc:  # noqa: BLE001
            data, ok_json = None, False
            print(f"  JSON 解析失败: {type(exc).__name__}: {exc}")
        check("JSON 合法可解析", ok_json)
        if ok_json:
            check("JSON 三组均为 list",
                  all(isinstance(data.get(g), list) for g in ALL_GROUPS),
                  f"类型={ {g: type(data.get(g)).__name__ for g in ALL_GROUPS} }")
            check("JSON game 10 条 (8 键 + phase/hex)",
                  len(data.get("game", [])) == 10, f"条数={len(data.get('game', []) )}")
            check("JSON players/shop 条数", len(data.get("players", [])) == 8
                  and len(data.get("shop", [])) == 5,
                  f"players={len(data.get('players', []))} shop={len(data.get('shop', []))}")
            check("JSON 中文不转义 (ensure_ascii=False)", "\\u" not in raw,
                  "文件内含 \\uXXXX 转义" if "\\u" in raw else "")
            check("JSON 含中文原文", "飞驰" in raw, "")


def main() -> int:
    parser = argparse.ArgumentParser(description="图色抽取脚手架自测 (3 张静态图 + CLI 落盘)")
    parser.add_argument("--img-dir", default=str(ROOT / "tests" / "images"),
                        help="测试图目录 (默认 tests/images)")
    parser.add_argument("--out", default=str(ROOT / "tempdir" / "selftest"),
                        help="CLI 落盘检查用的输出目录 (默认 tempdir/selftest)")
    parser.add_argument("--no-cli", action="store_true", help="跳过 CLI 子进程测试 (只测 API)")
    args = parser.parse_args()

    img_dir = Path(args.img_dir)
    print(f"--- selftest_extract: img_dir={img_dir} python={PY} ---")
    t0 = time.time()
    test_cli_help()
    for name, fn in (("board_ingame", test_board_ingame),
                     ("augment_choose", test_augment_choose),
                     ("board_green_model", test_board_green_model)):
        img_path = img_dir / f"{name}.png"
        if not img_path.exists():
            check(f"{name}: 测试图存在", False, f"缺 {img_path}")
            continue
        fn(img_dir)
    if not args.no_cli:
        test_cli_json(Path(args.out))

    print(f"\n统计: {len(_checks)} 项失败 / 共检查项如上, 用时 {time.time() - t0:.2f}s")
    if _checks:
        print("[X] 失败项: " + "; ".join(_checks))
        return 1
    print("[OK] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
