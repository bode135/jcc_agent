"""自测: extract_info.prompt 的 prompt 组合 (一帧 -> 四张 Markdown 表).

跑法:
    python -X utf8 -u -m tools.selftest_prompt            # 测 tests/images + tempdir 帧
    python -X utf8 -u -m tools.selftest_prompt --verbose

判据 (对 board_ingame.png, 一帧干净准备回合):
    game 段有 stage/level/gold; shop 5 张且名字全中
    board 名字命中 >= 4 (YOLO 主力路); bench >= 3; equip_bar >= 5
    board_equips 至少 1 组挂上棋子
    Markdown 四张表的表头/分隔行齐全; 同格位重复已被去重
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import extract_info.prompt as P  # noqa: E402

BASE = (1920, 1080)
CLEAN = ROOT / "tests" / "images" / "board_ingame.png"
FRAME_45 = ROOT / "tempdir" / "frame_4-5.png"

_ok = 0
_bad = 0


def check(cond: bool, label: str, detail: str = "") -> bool:
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"[OK] {label}" + (f" · {detail}" if detail else ""))
    else:
        _bad += 1
        print(f"[X ] {label}" + (f" · {detail}" if detail else ""))
    return cond


def load(path: Path) -> Image.Image:
    img = Image.open(path).convert("RGB")
    if tuple(img.size) != BASE:
        img = img.resize(BASE, Image.LANCZOS)
    return img


def names_hit(rows: list[dict]) -> tuple[int, int]:
    return sum(1 for r in rows if r.get("name") and r["name"] != "?"), len(rows)


def check_markdown(text: str, label: str) -> None:
    heads = ["## 商店", "## 场上棋子", "## 备战席棋子", "## 装备栏"]
    missing = [h for h in heads if h not in text]
    check(not missing, f"{label} 四张表都在", "缺 " + str(missing) if missing else "")
    for h in heads:
        i = text.find(h)
        if i < 0:
            continue
        seg = text[i:i + 400]
        check("| :---" in seg, f"{label} {h.strip('# ')} 有表头分隔行")
    check(text.count("| :---") >= 4, f"{label} 至少 4 张表", f"实际 {text.count('| :---')}")


def run_clean(verbose: bool) -> None:
    print("\n=== 1. 干净准备帧 board_ingame.png ===")
    img = load(CLEAN)
    timings: dict = {}
    errors: dict = {}
    t0 = time.perf_counter()
    snap = P.snapshot(img, timings=timings, errors=errors)
    dt = round(time.perf_counter() - t0, 2)
    print(f"[耗时] total={dt}s " + " ".join(f"{k}={v}s" for k, v in timings.items()))

    game = snap.get("game") or {}
    check(bool(game.get("stage")), "game.stage", str(game.get("stage")))
    check(isinstance(game.get("level"), int), "game.level", str(game.get("level")))
    check(isinstance(game.get("gold"), int), "game.gold", str(game.get("gold")))

    shop = snap.get("shop") or []
    sn, st = names_hit(shop)
    check(st == 5, "shop 5 张", f"{st}")
    check(sn >= 4, "shop 名字命中 >=4", f"{sn}/{st}")

    board = snap.get("board") or []
    bn, bt = names_hit(board)
    check(bn >= 4, "board 名字命中 >=4", f"{bn}/{bt}")
    check(all(r.get("cell") for r in board), "board 每行都有格位",
          str([r.get("cell") for r in board]))
    check(any(r.get("stars") for r in board), "board 有星级",
          str([(r["name"], r.get("stars")) for r in board]))

    bench = snap.get("bench") or []
    sbn, sbt = names_hit(bench)
    check(sbn >= 3, "bench 名字命中 >=3", f"{sbn}/{sbt}")

    bar = snap.get("equip_bar") or []
    check(len(bar) >= 5, "equip_bar >=5", f"{len(bar)}")

    equips = [(r.get("name"), r.get("equips")) for r in board if r.get("equips")]
    # 这张测试图上没有棋子带装备 (干净准备帧), 所以这里只要求"不崩 + 空列表",
    # 真正"装备挂回棋子"的链路在 4-5 帧里验 (那帧有圣盾使的誓约)。
    check(all(r.get("equips") == [] for r in board), "干净帧 board_equips 空 (该帧无携装棋子)",
          str(equips))

    text = P.to_markdown(snap)
    check_markdown(text, "干净帧")
    check("4-5" not in text and "3-6" in text, "标题区带上阶段号", text.splitlines()[0])
    if verbose:
        print(text)


def run_45(verbose: bool) -> None:
    print("\n=== 2. 4-5 战斗帧 (备战席已满弹窗) ===")
    if not FRAME_45.exists():
        print(f"[--] 跳过: 没有 {FRAME_45}")
        return
    img = load(FRAME_45)
    timings: dict = {}
    errors: dict = {}
    t0 = time.perf_counter()
    snap = P.snapshot(img, timings=timings, errors=errors)
    dt = round(time.perf_counter() - t0, 2)
    print(f"[耗时] total={dt}s " + " ".join(f"{k}={v}s" for k, v in timings.items()))

    game = snap.get("game") or {}
    check(game.get("stage") == "4-5", "stage=4-5", str(game.get("stage")))
    board = snap.get("board") or []
    bn, bt = names_hit(board)
    bench = snap.get("bench") or []
    sbn, sbt = names_hit(bench)
    print(f"      场上 {bn}/{bt} 备战席 {sbn}/{sbt}")
    check(bn >= 4, "board 名字命中 >=4", f"{bn}/{bt}")
    check(sbn >= 6, "bench 名字命中 >=6", f"{sbn}/{sbt}")

    cells = [r.get("cell") for r in board]
    check(len(cells) == len(set(cells)), "同格位已去重", str(cells))

    linked = [(r.get("name"), r.get("equips")) for r in board if r.get("equips")]
    check(bool(linked), "4-5 帧 board_equips 挂到棋子", str(linked))

    text = P.to_markdown(snap)
    check_markdown(text, "4-5 帧")
    if verbose:
        print(text)


def run_only(verbose: bool = False) -> None:
    print("\n=== 3. --only / no-players 组合 ===")
    img = load(CLEAN)
    snap = P.snapshot(img, only={"shop"})
    check(set(snap) & {"board", "bench", "equip_bar"} == set(), "--only shop 不产出棋盘/装备",
          str(sorted(k for k in snap if not k.startswith("_"))))
    check(len(snap.get("shop") or []) == 5, "--only shop 仍有 5 张",
          f"实际 {len(snap.get('shop') or [])}")

    snap2 = P.snapshot(img, only={"board"})
    check("players" not in snap2, "--only board 不含 players")
    check(bool(snap2.get("board")), "--only board 有棋子")

    j = P.to_json(P.snapshot(img, only={"game"}))
    check("game" in j and "_timings" not in j, "to_json 去掉内部键", str(sorted(j)))


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    print("=" * 70)
    print("selftest_prompt: 一帧 -> Agent prompt (四张 Markdown 表)")
    print("=" * 70)
    t0 = time.perf_counter()
    for fn in (run_clean, run_45, run_only):
        try:
            fn(verbose)
        except Exception as exc:  # noqa: BLE001 - 单段崩了也要把后面跑完
            global _bad
            _bad += 1
            print(f"[X ] {fn.__name__} 抛异常: {type(exc).__name__}: {exc}")
    dt = round(time.perf_counter() - t0, 2)
    print("\n" + "=" * 70)
    print(f"结果: {_ok} 项 [OK], {_bad} 项失败, 总耗时 {dt}s")
    print("=" * 70)
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())