"""把一帧画面组合成 Agent 可直接喂的 Markdown 局面快照 (等价 runner 里那几段"当前局面").

纯图色口径: 所有数字都来自识别结果 (OCR / YOLO / 模板 / 颜色), 不读游戏内存。
本模块只做**取数 + 排版**, 不做任何决策, 也不改任何游戏状态。

四张核心表 (用户 m02246 指定):
    装备栏 / 场上棋子 / 备战席棋子 / 商店
另加一节玩家血量 (runner 的 prompt 里也带着, 用来判断该不该冲名次)。

取数来源:
    game / shop / players  <- extract_info.extract()      (OCR + 区域切片)
    board / bench          <- recognize.board.extract_units()  (血条定位 + YOLO 名字 + 星级徽记)
    equip_bar              <- recognize.equipment.extract_equipment()   (装备栏图标 vs 图标库)
    board_equips           <- recognize.equipment.extract_board_equips() (棋子身上的装备)
    羁绊/职业/定位/攻距     <- units.db.find_unit()  (本地爬虫库, 查不到就省略, 不猜)

用法 (命令行见 scripts/frame_prompt.py):
    from extract_info.prompt import snapshot, to_markdown
    snap = snapshot(img)
    print(to_markdown(snap))
"""

from __future__ import annotations

from config.config import InfoTypes  # noqa: F401  (保持与 extract 层同一套常量入口)

# 表格里"没读到"的占位符: 用 - 而不是空, 让 Agent 一眼看出是缺数据
_EMPTY = "-"


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #
def _unit_brief(detail: dict | None) -> dict:
    """本地棋子库 -> 表格里要用的几列 (羁绊/职业/定位/攻距); 查不到返回空 dict."""
    if not detail:
        return {}
    races = "/".join(detail.get("races") or []) or _EMPTY
    jobs = "/".join(detail.get("jobs") or []) or _EMPTY
    out = {"races": races, "jobs": jobs}
    role = (detail.get("role") or "").strip()
    if role:
        out["role"] = role
    stats = detail.get("stats") or {}
    rng = stats.get("attackRange")
    if isinstance(rng, int):
        out["range"] = f"{rng} ({'后排' if rng > 1 else '前排'})"
    price = detail.get("price")
    if isinstance(price, int):
        out["db_price"] = price
    return out


def _lookup(name: str | None) -> dict:
    """按名字查本地棋子库; 库坏了/查不到都返回空 dict (绝不打断快照)."""
    if not name:
        return {}
    try:
        from units import db as units_db
        return _unit_brief(units_db.find_unit(name))
    except Exception:  # noqa: BLE001 - 库不可用不应打断组合
        return {}


def _equip_names(equips: list | None) -> list[str]:
    """棋子携带装备 -> 名字列表 (没认出名但有 equipId 就显示 ID, 不让它静默消失)."""
    out = []
    for e in equips or []:
        name = (e.get("name") or "").strip()
        if name:
            out.append(name)
        elif e.get("equipId") is not None:
            out.append(f"ID:{e['equipId']}")
    return out


def _cell_text(unit: dict) -> str:
    """格位标签: 己方 r1~r4c1~c7 / 敌方 -r1~-r4 / 备战席 slot1~slot9; 认不出就 ?.

    格位在 `unit["location"]` 里 (recognize.board.extract_units 的口径):
    bench 是 {"slot": n}, board 是 {"row": 绝对行 1-8, "column": c}。
    """
    loc = unit.get("location") or {}
    if loc.get("slot") is not None:
        return f"slot{loc['slot']}"
    row, col = loc.get("row"), loc.get("column")
    if row is None or col is None:
        return "?"
    try:
        from recognize.grid import cell_label
        return cell_label(int(row), int(col))
    except Exception:  # noqa: BLE001
        return f"r{row}c{col}"


def _assign_equips(units: dict, anchors: list[tuple[float, float, list[str]]],
                   *, max_dist: float = 70.0) -> dict[tuple[str, int], list[str]]:
    """把"血条锚点上的装备"贪心分配给棋子 (一锚点只给最近的一只, 一只棋子只拿一组).

    装备是**血条口径**采到的 (`equipment.extract_board_equips` 用 `bar` 框定位), 棋子是
    YOLO 框; 血条就在棋子脚下, 两者中心距 ~40px, 所以用"中心距 <=70px"关联 —— 与
    `overlay.live_draw.merge_stars` 同一判据。必须**一对一**: 两根血条挨得近时若各算各的,
    同一件装备会同时挂到两只棋子上 (实测见过"鬼索的狂暴之刃"挂了两只)。

    返回 {(area, 棋子下标): [装备名...]}; 没装备的棋子不出现在返回里 (调用方填 [])。
    """
    if not anchors:
        return {}
    cand: list[tuple[float, str, int, int]] = []
    for area in ("board", "bench"):
        for idx, u in enumerate(units.get(area) or []):
            c = u.get("center") or u.get("box")
            if not c or len(c) < 2:
                continue
            ux, uy = float(c[0]), float(c[1])
            for ai, (ax, ay, _names) in enumerate(anchors):
                d = ((ax - ux) ** 2 + (ay - uy) ** 2) ** 0.5
                if d <= max_dist:
                    cand.append((d, area, idx, ai))
    cand.sort(key=lambda x: x[0])
    used_anchor: set[int] = set()
    used_unit: set[tuple[str, int]] = set()
    out: dict[tuple[str, int], list[str]] = {}
    for _d, area, idx, ai in cand:
        key = (area, idx)
        if key in used_unit or ai in used_anchor:
            continue
        out[key] = list(anchors[ai][2])
        used_unit.add(key)
        used_anchor.add(ai)
    return out


def _dedup_by_cell(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """同一格位被检出多个 (棋子重叠/移动/重复框) 时只留置信度最高的一个.

    返回 (保留的行, 被丢掉的行)。被丢的交给调用方记进 errors, 不静默吞掉 ——
    Agent 看到"这一格原本还有谁"才知道画面当时不干净。
    """
    best: dict[str, dict] = {}
    dropped: list[dict] = []
    for row in rows:
        key = str(row.get("cell") or "?")
        cur = best.get(key)
        if cur is None:
            best[key] = row
        elif (row.get("score") or 0) > (cur.get("score") or 0):
            dropped.append(cur)
            best[key] = row
        else:
            dropped.append(row)
    return list(best.values()), dropped


def snapshot(img, *, only: set[str] | None = None, timings: dict | None = None,
             errors: dict | None = None) -> dict:
    """一帧 -> 结构化局面快照.

    only: 只取其中几项 (默认全取)。可选键:
        game / shop / players / board / bench / equip_bar / board_equips
    timings/errors: 传入 dict 时按步骤记耗时与异常文本 (与 extract_info.extract 同风格)。

    返回:
        {
          "game": {stage, round_time, level, xp, gold, xp_buy_cost, refresh_cost, free_refresh, phase},
          "shop": [{"slot", "name", "price", "races", "jobs", "role", "range"}],
          "players": [{"id", "hp"}],
          "board": [{"cell", "name", "stars", "races", "jobs", "role", "range", "equips"}],
          "bench": [{"slot", "name", "stars", "races", "jobs", "role", "range", "equips"}],
          "equip_bar": [{"slot", "name", "count", "kind"}],
        }
    单个子项失败只写 errors, 不影响其它项。
    """
    import time

    from PIL import Image  # noqa: F401  (调用方可能传 Image, 这里只做类型宽容)

    want = set(only) if only else {"game", "shop", "players", "board", "bench",
                                   "equip_bar", "board_equips"}
    timings = timings if timings is not None else {}
    errors = errors if errors is not None else {}
    out: dict = {}

    def run(label: str, fn):
        t0 = time.perf_counter()
        try:
            val = fn()
        except Exception as exc:  # noqa: BLE001 - 单步失败不能拖垮整次快照
            errors[label] = f"{type(exc).__name__}: {exc}"
            val = None
        timings[label] = round(time.perf_counter() - t0, 3)
        return val

    # 1) OCR 那一路 (game / shop / players 共享一次整图 OCR)
    ocr_groups = want & {"game", "shop", "players"}
    extracted: dict = {}
    if ocr_groups:
        from extract_info import extractor

        def _extract():
            return extractor.extract(img, only=ocr_groups, timings=timings, errors=errors)

        extracted = run("extract", _extract) or {}

    if "game" in want:
        game: dict = {}
        for rec in extracted.get("game") or []:
            if isinstance(rec, dict) and rec.get("key"):
                game[rec["key"]] = rec.get("value")
        out["game"] = game

    if "shop" in want:
        shop = []
        for i, s in enumerate(extracted.get("shop") or [], 1):
            name = s.get("name")
            row = {"slot": i, "name": name or "?", "price": s.get("price")}
            row.update(_lookup(name))
            shop.append(row)
        out["shop"] = shop

    if "players" in want:
        out["players"] = [{"id": p.get("id"), "hp": p.get("hp")}
                          for p in (extracted.get("players") or [])]

    # 2) 棋盘/备战席棋子
    #    主力是 YOLO 检测 (名字/置信度/格位准, ~7ms), 星级由血条法补 (徽记锚点),
    #    血条法本身名字召回差, 只当"星级+装备锚点"用。两条路靠 merge_stars 合并。
    det: dict = {}
    bars: dict = {}
    if want & {"board", "bench", "board_equips"}:
        from recognize import yolo_units

        det = run("yolo_units.extract_units",
                  lambda: yolo_units.extract_units(img)) or {}

    if want & {"board", "bench"}:
        from overlay import live_draw
        from recognize import board as board_mod

        bars = run("board.extract_units",
                   lambda: board_mod.extract_units(img, include_legend=True,
                                                   match_cells=set())) or {}
        det = run("merge_stars",
                  lambda: live_draw.merge_stars({"units": det, "bars": bars})) or det

    if "board" in want or "bench" in want:
        if "board" in want:
            out["board"] = []
        if "bench" in want:
            out["bench"] = []
        for area in ("board", "bench"):
            if area not in want:
                continue
            rows = []
            for u in det.get(area) or []:
                name = u.get("name")
                row = {
                    "cell": _cell_text(u),
                    "name": name or "?",
                    "stars": u.get("stars"),
                    "score": u.get("score"),
                }
                row.update(_lookup(name))
                if u.get("legend"):
                    row["legend"] = True
                rows.append(row)
            rows, dup = _dedup_by_cell(rows)
            if dup:
                errors[f"{area}.duplicate"] = "; ".join(
                    f"{d.get('cell')}:{d.get('name')}({d.get('score')})" for d in dup)
            out[area] = rows

    # 3) 装备栏 (图标库匹配, 含消耗品)
    if "equip_bar" in want:
        from recognize import equipment

        bar = run("equipment.extract_equipment",
                  lambda: equipment.extract_equipment(img)) or []
        out["equip_bar"] = [{"slot": e.get("slot"), "name": e.get("name") or "?",
                             "count": e.get("count"), "kind": e.get("kind")}
                            for e in bar]

    # 4) 棋子携带装备 (血条方框 -> 装备图标 -> 按血条中心对齐回 YOLO 棋子)
    if "board_equips" in want:
        from recognize import equipment

        equip_units = bars or det
        equips = run("equipment.extract_board_equips",
                     lambda: equipment.extract_board_equips(img, units=equip_units)) or []
        # bar 框 -> 装备名列表
        by_bar: dict[tuple, list[str]] = {}
        for e in equips:
            bar = e.get("bar")
            if not bar:
                continue
            names = _equip_names(e.get("equips"))
            if names:
                by_bar.setdefault(tuple(int(v) for v in bar), []).extend(names)
        out["_equips_by_bar"] = by_bar
        # 血条 unit 中心 -> 装备名 (与 merge_stars 同口径: 中心距 <=70 才算同一只)
        anchors: list[tuple[float, float, list[str]]] = []
        for area in ("board", "bench"):
            for b in (bars.get(area) or []):
                c = b.get("center")
                bar = b.get("bar")
                if not c or not bar:
                    continue
                names = by_bar.get(tuple(int(v) for v in bar))
                if names:
                    anchors.append((float(c[0]), float(c[1]), names))
        if bars and not anchors:
            errors["board_equips.link"] = (f"采到 {len(by_bar)} 组装备但没挂到任何血条 unit 上")
        linked = _assign_equips(det, anchors)
        for area in ("board", "bench"):
            if area not in want:
                continue
            for idx, row in enumerate(out.get(area) or []):
                row["equips"] = linked.get((area, idx), [])

    out.pop("_equips_by_bar", None)
    out["_timings"] = timings
    out["_errors"] = errors
    return out


# --------------------------------------------------------------------------- #
# 排版 (Markdown)
# --------------------------------------------------------------------------- #
def _md_table(headers: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    """拼一张 Markdown 表 (aligns 用 markdown 的 :--- / ---: / :---:)."""
    if not rows:
        return "  (空)"
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(aligns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _val(v, empty: str = _EMPTY) -> str:
    if v is None or v == "":
        return empty
    return str(v)


def _conf(unit: dict) -> str:
    """棋子名置信度; 未匹配到名字时写 `-低-` 提醒 Agent 这行不可靠."""
    score = unit.get("score")
    if unit.get("name"):
        return f"{score:.2f}" if isinstance(score, (int, float)) else _EMPTY
    if isinstance(score, (int, float)):
        return f"?{score:.2f}"
    return "?"


def to_markdown(snap: dict, *, title: str | None = None,
                with_players: bool = True) -> str:
    """快照 -> Markdown 文本 (四张主表 + 标题区 + 可选玩家血量)."""
    game = snap.get("game") or {}
    stage = game.get("stage")
    phase = game.get("phase")
    blocks: list[str] = []

    blocks.append(f"# 对局快照{(' · ' + str(stage)) if stage else ''}"
                  f"{(' · ' + str(phase)) if phase else ''}")

    head = []
    if game.get("round_time") is not None:
        head.append(f"倒计时 {game['round_time']}s")
    if game.get("level") is not None:
        xp = game.get("xp")
        xp_txt = f" (经验 {xp[0]}/{xp[1]})" if isinstance(xp, (list, tuple)) and len(xp) == 2 else ""
        head.append(f"等级 {game['level']}{xp_txt}")
    if game.get("gold") is not None:
        head.append(f"金币 {game['gold']}")
    if game.get("xp_buy_cost") is not None:
        head.append(f"买经验 {game['xp_buy_cost']}")
    if game.get("refresh_cost") is not None:
        head.append(f"刷新 {game['refresh_cost']}")
    if game.get("free_refresh"):
        head.append(f"免费刷新 {game['free_refresh']}")
    if head:
        blocks.append("- " + " · ".join(head))

    counts = []
    for key, label in (("board", "场上"), ("bench", "备战席"),
                       ("equip_bar", "装备栏"), ("shop", "商店")):
        if key in snap:
            counts.append(f"{label} {len(snap.get(key) or [])}")
    if counts:
        blocks.append("- 识别到: " + " / ".join(counts))

    if "shop" in snap:
        rows = []
        for s in snap["shop"]:
            price = _val(s.get("price"))
            db_price = s.get("db_price")
            if db_price and s.get("price") and s["price"] == 3 * db_price:
                price += " (疑似2星)"
            rows.append([_val(s.get("slot")), _val(s.get("name")), price,
                         _val(s.get("range")), _val(s.get("races")),
                         _val(s.get("jobs")), _val(s.get("role"))])
        blocks.append("## 商店 (自左向右槽位 1-5)\n\n" + _md_table(
            ["槽位", "棋子", "价格", "攻距", "羁绊", "职业", "定位"],
            [":---", ":---", "---:", ":---", ":---", ":---", ":---"], rows))

    if "board" in snap:
        rows = []
        for u in snap["board"]:
            star = f"{u['stars']}★" if u.get("stars") else _EMPTY
            rows.append([_val(u.get("cell")), _val(u.get("name")), _conf(u),
                         star, _val(u.get("races")), _val(u.get("jobs")),
                         _val(u.get("role")),
                         " / ".join(u.get("equips") or []) or _EMPTY])
        blocks.append("## 场上棋子 (格位 r1~r4 = 己方, -rN = 敌方)\n\n" + _md_table(
            ["格位", "棋子", "置信度", "星级", "羁绊", "职业", "定位", "携带装备"],
            [":---", ":---", "---:", ":---:", ":---", ":---", ":---", ":---"], rows))

    if "bench" in snap:
        rows = []
        for u in snap["bench"]:
            star = f"{u['stars']}★" if u.get("stars") else _EMPTY
            rows.append([_val(u.get("cell")), _val(u.get("name")), _conf(u),
                         star, _val(u.get("races")), _val(u.get("jobs")),
                         " / ".join(u.get("equips") or []) or _EMPTY])
        blocks.append("## 备战席棋子 (slot1~slot9)\n\n" + _md_table(
            ["槽位", "棋子", "置信度", "星级", "羁绊", "职业", "携带装备"],
            [":---", ":---", "---:", ":---:", ":---", ":---", ":---"], rows))

    if "equip_bar" in snap:
        rows = []
        for e in snap["equip_bar"]:
            rows.append([_val(e.get("slot")), _val(e.get("name")),
                         _val(e.get("count"), "1"), _val(e.get("kind"))])
        blocks.append("## 装备栏\n\n" + _md_table(
            ["槽位", "装备", "件数", "类型"],
            [":---", ":---", "---:", ":---"], rows))

    if with_players and "players" in snap:
        rows = [[_val(p.get("id")), _val(p.get("hp"))] for p in snap["players"]]
        blocks.append("## 各玩家血量\n\n" + _md_table(
            ["玩家", "血量"], [":---", "---:"], rows))

    return "\n\n".join(blocks) + "\n"


def to_json(snap: dict) -> dict:
    """去掉下划线开头的内部键, 返回可 json.dump 的纯数据."""
    return {k: v for k, v in snap.items() if not str(k).startswith("_")}