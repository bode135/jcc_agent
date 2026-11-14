"""纯图色识别核心自测 (T1): 一张图跑通 OCR / YOLO / 血条 / 装备 / 球 / 阶段 / 海克斯.

用法 (工作目录 = 工程根):
    python -X utf8 -u tools/selftest_recognize.py --img tests/images/board_ingame.png
    python -X utf8 -u tools/selftest_recognize.py            # 默认跑 tests/images 下全部图
    python -X utf8 -u tools/selftest_recognize.py --img x.png --no-yolo   # 跳过 YOLO (只测图色)

每一段都是"打印真实数字 + 单段异常不拖垮其它段": 某段抛异常会打 [X] 与该段耗时, 后续段继续。
输出口径: [OK] 通过 / [X] 失败 / [!] 降级或已知局限 (禁 emoji)。退出码: 有 [X] 段 -> 1。
"""

import argparse
import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image                                     # noqa: E402
from recognize import augment, board, equipment, hero, ocr, phase, reward_ball   # noqa: E402
from recognize import yolo_units                          # noqa: E402
from units import matcher                                 # noqa: E402

DEFAULT_IMAGES = (
    "board_ingame.png",
    "augment_choose.png",
)

# 同一单位在 YOLO 类名 / units.db 名里可能叫法不同, 交叉校验时视为一致
ALIAS_TREE = {"绯红印记树怪", "绯红树怪"}


def _sec(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 66 - len(title)), flush=True)


def _fmt_loc(loc) -> str:
    """格位 dict -> 可读字符串."""
    if not loc:
        return "-"
    if loc.get("slot"):
        return f"slot{loc['slot']}"
    if loc.get("row") and loc.get("column"):
        return f"r{loc['row']}c{loc['column']}"
    return "-"


def run_one(path: Path, *, use_yolo: bool = True, rows: bool = True) -> dict:
    """跑一张图, 返回各段结果 dict; failures 里列失败段."""
    out = {"path": str(path), "failures": [], "timings": {}}
    print("=" * 72, flush=True)
    print(f"[IMG] {path.name}", flush=True)
    t0 = time.time()
    try:
        img = phase.normalize_1080(Image.open(path))
        print(f"[IMG] 归一化 -> {img.size} ({time.time() - t0:.2f}s)", flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[X] 读图/归一化失败: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        out["failures"].append("load")
        return out
    out["size"] = list(img.size)

    # ---- 1. OCR 整图 -------------------------------------------------------
    _sec("1. OCR 整图")
    t = time.time()
    try:
        items = ocr.recognize(img)
        out["timings"]["ocr"] = round(time.time() - t, 2)
        out["ocr_items"] = len(items)
        print(f"[OK] OCR 条目 = {len(items)}  ({out['timings']['ocr']}s, "
              f"引擎={'GPU' if ocr.engine_gpu() else 'CPU'})", flush=True)
        for it in items[:6]:
            print("     " + repr(it["text"])[:44].ljust(46)
                  + f" score={it['score']:.3f} box={tuple(int(v) for v in it['box'])}", flush=True)
        if len(items) < 3:
            print("[!] OCR 条目过少 (<3), 检查是否整图空白/遮挡", flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[X] OCR 异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        out["failures"].append("ocr")

    # ---- 2. YOLO 棋子 -----------------------------------------------------
    _sec("2. YOLO 棋子 (功能点 units)")
    if not use_yolo:
        print("[!] 已用 --no-yolo 跳过", flush=True)
    else:
        t = time.time()
        try:
            print(f"[yolo] feature_models={yolo_units.feature_models()} "
                  f"available={yolo_units.available()}", flush=True)
            y = yolo_units.extract_units(img)
            out["timings"]["yolo"] = round(time.time() - t, 2)
            out["yolo_board"] = len(y["board"])
            out["yolo_bench"] = len(y["bench"])
            print(f"[OK] YOLO 棋子 board={len(y['board'])} bench={len(y['bench'])} "
                  f"({out['timings']['yolo']}s)", flush=True)
            print(f"     stats={yolo_units.last_stats()}", flush=True)
            print(f"     timing={ {k: round(v, 1) for k, v in yolo_units.last_timing().items()} }",
                  flush=True)
            for u in y["board"][:8]:
                print(f"     B {str(u['name']):<6} score={u['score']:<6} "
                      f"{_fmt_loc(u.get('location'))}", flush=True)
            for u in y["bench"][:8]:
                print(f"     S {str(u['name']):<6} score={u['score']:<6} "
                      f"{_fmt_loc(u.get('location'))}", flush=True)
            names = [u["name"] for u in y["board"] + y["bench"]]
            if sum(1 for n in names if n) < 1:
                print("[X] YOLO 一个具名棋子都没有", flush=True)
                out["failures"].append("yolo_names")
            out["yolo_truth"] = {
                (u["center"][0], u["center"][1]): u["name"]
                for a in ("board", "bench") for u in y[a]
                if u.get("center") and u.get("name")
            }
            out["yolo_balls"] = len(yolo_units.last_balls())
            out["yolo_legends"] = len(yolo_units.last_legends())
            print(f"     丢弃: balls={out['yolo_balls']} legends={out['yolo_legends']}", flush=True)
        except Exception as exc:                              # noqa: BLE001
            print(f"[X] YOLO 异常: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            out["failures"].append("yolo")

    # ---- 3. 血条棋子路径 --------------------------------------------------
    _sec("3. 血条棋子 (board.extract_units)")
    t = time.time()
    try:
        b = board.extract_units(img)
        out["timings"]["board"] = round(time.time() - t, 2)
        out["bar_board"] = len(b["board"])
        out["bar_bench"] = len(b["bench"])
        out["bars"] = len(b["board"]) + len(b["bench"])
        print(f"[OK] 血条棋子 board={len(b['board'])} bench={len(b['bench'])} "
              f"({out['timings']['board']}s)", flush=True)
        for u in b["board"]:
            print(f"     B {str(u['name']):<6} score={u['score']:<6} stars={u['stars']} "
                  f"{_fmt_loc(u.get('location'))}", flush=True)
        for u in b["bench"]:
            print(f"     S {str(u['name']):<6} score={u['score']:<6} "
                  f"{_fmt_loc(u.get('location'))}", flush=True)
        if out["bars"] < 1:
            print("[!] 血条 0 根 (该帧可能不是对局画面, 或血条配色与基准不符)", flush=True)
        # 小小英雄 (名牌召回天然低, 只报数字)
        lg = board.extract_units(img, include_legend=True)
        legends = [u for a in ("board", "bench") for u in lg[a] if u.get("legend")]
        out["legends"] = len(legends)
        print(f"     include_legend=True -> legends={len(legends)} "
              f"(已知: 白色名牌召回低, 只当参考)", flush=True)
        # ---- 星级徽记 (2026-11-14 用户口径: 棋子血条左侧必有星级, 小英雄左侧是等级数字) ----
        st = board.last_stats()
        dropped = st.get("dropped_no_badge") or []
        print(f"[badge] 星级徽记: 无徽记丢弃 {len(dropped)} 根 · require_badge={st.get('require_badge')}",
              flush=True)
        for d in dropped[:5]:
            print(f"     丢弃 bar={d['bar']} (颜色法星级={d.get('stars_color')}) -- 不是棋子血条",
                  flush=True)
        badge_stars = [u.get("stars") for u in b["board"] + b["bench"]]
        print(f"[badge] 星级分布: {badge_stars}", flush=True)
        bad_legend = [u for u in legends if u.get("stars")]
        if bad_legend:
            print(f"[X] 小小英雄被当成棋子星级: {[(u['bar'], u['stars']) for u in bad_legend]}",
                  flush=True)
            out["failures"].append("legend_stars")
        missing = [u["bar"] for u in b["board"] + b["bench"] if u.get("stars") is None]
        if missing and st.get("require_badge"):
            print(f"[X] 有血条但读不到星级徽记: {missing}", flush=True)
            out["failures"].append("badge_stars")
        names = [u["name"] for u in b["board"] + b["bench"] if u.get("name")]
        print(f"[matcher] 库={matcher.library_source()} names={matcher.library_size()} "
              f"variants={matcher.variants()}", flush=True)
        if not names:
            print("[!] 血条路径 0 个具名棋子: 头像模板与实机 3D 模型相关度低 "
                  "(身份交给 YOLO, 见报告)", flush=True)
        # 头像匹配 vs YOLO 真值 (按最近的中心对齐): 命中数/正确数/错误数
        truth = out.get("yolo_truth") or {}
        if truth:
            ok = wrong = none = 0
            for a in ("board", "bench"):
                for u in b[a]:
                    tname, (tx, ty) = min(
                        ((n, c) for c, n in truth.items()),
                        key=lambda kv: abs(kv[1][0] - u["center"][0]) + abs(kv[1][1] - u["center"][1]),
                        default=(None, (0, 0)))
                    d = abs(tx - u["center"][0]) + abs(ty - u["center"][1])
                    gm = tname if d <= 80 else None
                    if u.get("name") and gm and (u["name"] == gm or {u["name"], gm} == ALIAS_TREE):
                        ok += 1
                    elif u.get("name"):
                        wrong += 1
                        print(f"     [!] 匹配名={u['name']!r}@{u['score']} 与 YOLO 真值={gm!r} 不符",
                              flush=True)
                    else:
                        none += 1
            out["match_ok"], out["match_wrong"], out["match_none"] = ok, wrong, none
            prec = (ok / (ok + wrong)) if (ok + wrong) else 0.0
            print(f"[matcher] 头像匹配命中(>=阈值) = {ok + wrong}/{ok + wrong + none} "
                  f"其中正确 {ok} / 错误 {wrong} / 未命名 {none} 准确率={prec:.0%}", flush=True)
            if wrong:
                print("[!] 头像匹配存在误命名: 身份以 YOLO 为准, matcher 只作交叉校验", flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[X] 血条棋子异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        out["failures"].append("board")

    # ---- 4. 装备(左下库存 + 场上棋子身上) ---------------------------------
    _sec("4. 装备")
    t = time.time()
    try:
        slots = equipment.detect_slots(img)
        out["slots"] = len(slots)
        eq = equipment.extract_equipment(img)
        out["timings"]["equipment"] = round(time.time() - t, 2)
        out["equipment"] = len(eq)
        print(f"[OK] 库存槽 = {len(slots)} 件, 识别出装备 = {len(eq)} 件 "
              f"({out['timings']['equipment']}s)", flush=True)
        for s in eq[:8]:
            print(f"     {str(s.get('name')):<8} score={s.get('score')} "
                  f"center={s.get('center')}", flush=True)
        if not slots:
            print("[!] 库存槽 0 个: 该帧左侧装备栏为空 (正常空槽帧)", flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[X] 装备库存异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        out["failures"].append("equipment")

    if rows:
        t = time.time()
        try:
            be = equipment.extract_board_equips(img)
            out["timings"]["board_equips"] = round(time.time() - t, 2)
            out["board_equips"] = len(be)
            print(f"[OK] 场上棋子装备 = {len(be)} 件 "
                  f"({out['timings']['board_equips']}s)", flush=True)
            for e in be[:8]:
                print(f"     {str(e.get('name')):<8} score={e.get('score')} "
                      f"box={e.get('box')}", flush=True)
            if not be:
                print("[!] 场上 0 件装备 (该帧棋子身上确实没挂装备 = 空槽)", flush=True)
        except Exception as exc:                              # noqa: BLE001
            print(f"[X] 场上装备异常: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            out["failures"].append("board_equips")

    # ---- 5. 奖励球 -----------------------------------------------------
    _sec("5. 奖励球 (HSV 三色)")
    t = time.time()
    try:
        balls = reward_ball.find_balls(img)
        out["timings"]["balls"] = round(time.time() - t, 2)
        out["balls"] = len(balls)
        colors = {}
        for b in balls:
            colors[b.get("color")] = colors.get(b.get("color"), 0) + 1
        print(f"[OK] 球 = {len(balls)} 个 颜色={colors} ({out['timings']['balls']}s)", flush=True)
        for b in balls[:8]:
            print(f"     {str(b.get('color')):<7} center={b.get('center')} "
                  f"box={tuple(int(v) for v in b.get('box', ())) or '-'}", flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[X] 奖励球异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        out["failures"].append("balls")

    # ---- 6. 阶段判定 ---------------------------------------------------
    _sec("6. 阶段判定 (phase.detect)")
    t = time.time()
    try:
        st = phase.detect(img)
        out["timings"]["phase"] = round(time.time() - t, 2)
        out["phase"] = st.get("phase")
        out["stage"] = st.get("stage")
        print(f"[OK] phase={st.get('phase')} stage={st.get('stage')} "
              f"round={st.get('stage_round')} ({out['timings']['phase']}s)", flush=True)
        print(f"     sword={st.get('sword_score')} card={st.get('card_score')} "
              f"eye={st.get('eye_score')} refresh={st.get('refresh_score')} "
              f"yellow={st.get('yellow')} reason={st.get('reason')}", flush=True)
        if st.get("phase") is None:
            print("[X] phase 判定为 None", flush=True)
            out["failures"].append("phase")
    except Exception as exc:                                  # noqa: BLE001
        print(f"[X] 阶段判定异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        out["failures"].append("phase")

    # ---- 7. 海克斯 -----------------------------------------------------
    _sec("7. 海克斯三选一 (augment.extract)")
    t = time.time()
    try:
        au = augment.extract(img)
        out["timings"]["augment"] = round(time.time() - t, 2)
        out["is_augment"] = au["is_augment"]
        out["chosen"] = au["chosen"]
        print(f"[OK] is_augment={au['is_augment']} chosen={au['chosen']} "
              f"cards={len(au['cards'])} ({out['timings']['augment']}s)", flush=True)
        print(f"     eye={au['eye_score']} card={au['card_score']} "
              f"refresh={au['refresh_score']} green={au['green']} "
              f"overlay={au['overlay']} stage={au['stage']}", flush=True)
        for i, c in enumerate(au["cards"]):
            print(f"     卡{i + 1} name={c['name']!r} refresh_left={c['refresh_left']} "
                  f"center={c['center']}", flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[X] 海克斯异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        out["failures"].append("augment")

    # ---- 8. 小小英雄 (等级数字块 + 右侧红/绿小血段) ----------------------
    _sec("8. 小小英雄 (等级块法, 用户 m01362)")
    t = time.time()
    try:
        hs = hero.detect(img)
        out["timings"]["hero"] = round(time.time() - t, 3)
        out["heroes"] = len(hs)
        print(f"[OK] 英雄 {len(hs)} 处 ({out['timings']['hero']}s)", flush=True)
        for h in hs:
            who = "我方" if h.get("kind") == "mine" else "敌方"
            print(f"     {who} Lv{h.get('level')} score={h.get('level_score')} "
                  f"numblk={tuple(int(v) for v in h.get('numblk') or ())} "
                  f"落点=({h.get('x'):.1f},{h.get('y'):.1f})", flush=True)
        lv_bad = [h for h in hs if h.get("level") is None]
        if lv_bad:
            print(f"[!] {len(lv_bad)} 处英雄没读到等级 (模板缺失/遮挡)", flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print(f"[X] 英雄定位异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        out["failures"].append("hero")

    return out


def check_badge_gate() -> list[str]:
    """星级徽记门禁的回归用例 (2026-11-14).

    非对局帧 `board_green_model.png` 曾经被颜色法刷出 **25 根假血条**;
    加"无徽记即非棋子血条"规则后必须 0 根。这一项只依赖纯图色, 不计入单图耗时统计。
    """
    fails: list[str] = []
    p = PROJECT_ROOT / "tests" / "images" / "board_green_model.png"
    if not p.exists():
        print("[!] 跳过徽记门禁回归: 缺 tests/images/board_green_model.png", flush=True)
        return fails
    _sec("4. 星级徽记门禁 (非对局帧必须 0 根)")
    try:
        img = phase.normalize_1080(Image.open(p))
        t = time.time()
        res = board.extract_units(img, include_legend=True)
        st = board.last_stats()
        n = len(res["board"]) + len(res["bench"])
        dropped = len(st.get("dropped_no_badge") or [])
        print(f"[OK] board_green_model: 保留 {n} 根, 无徽记丢弃 {dropped} 根 "
              f"({time.time() - t:.2f}s)", flush=True)
        if n != 0:
            print(f"[X] 非对局帧仍检出 {n} 根血条 (徽记门禁失效)", flush=True)
            fails.append("badge_gate")
    except Exception as exc:                                  # noqa: BLE001
        print(f"[X] 徽记门禁用例异常: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        fails.append("badge_gate")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="纯图色识别核心自测 (OCR/YOLO/血条/装备/球/阶段/海克斯)")
    ap.add_argument("--img", action="append", default=None,
                    help="图片路径 (可重复); 缺省跑 tests/images 下 board_ingame + augment_choose")
    ap.add_argument("--no-yolo", action="store_true", help="跳过 YOLO 段 (只测纯图色)")
    ap.add_argument("--no-rows", action="store_true", help="跳过场上棋子装备段 (较慢)")
    args = ap.parse_args(argv)

    imgs = [Path(p) for p in (args.img or [PROJECT_ROOT / "tests" / "images" / n
                                           for n in DEFAULT_IMAGES])]
    missing = [p for p in imgs if not p.exists()]
    for p in missing:
        print(f"[X] 图片不存在: {p}", flush=True)
    if missing and len(missing) == len(imgs):
        return 2

    t_all = time.time()
    results = []
    for p in imgs:
        if p.exists():
            results.append(run_one(p, use_yolo=not args.no_yolo, rows=not args.no_rows))
    gate_fails = check_badge_gate()

    # ---- 汇总 ---------------------------------------------------------
    print("\n" + "=" * 72, flush=True)
    print(f"汇总 ({time.time() - t_all:.2f}s, {len(results)} 张图)", flush=True)
    bad = 0
    for r in results:
        flag = "[OK]" if not r["failures"] else "[X]"
        bad += 1 if r["failures"] else 0
        print(f"{flag} {Path(r['path']).name}: ocr={r.get('ocr_items', '-')} "
              f"yolo(board/bench)={r.get('yolo_board', '-')}/{r.get('yolo_bench', '-')} "
              f"bar={r.get('bars', '-')} slots={r.get('slots', '-')} "
              f"equip={r.get('equipment', '-')} ball={r.get('balls', '-')} "
              f"phase={r.get('phase')} stage={r.get('stage')} "
              f"augment={r.get('is_augment')} chosen={r.get('chosen')} "
              f"failures={r['failures'] or '无'}", flush=True)
    print(f"总计: {len(results) - bad}/{len(results)} 张无失败段", flush=True)
    if gate_fails:
        print(f"[X] 星级徽记门禁: {'/'.join(gate_fails)}", flush=True)
        bad += len(gate_fails)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
