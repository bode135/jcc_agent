"""把抓到的原始数据整理成"能照着打"的结构化分析.

输入: strategy(/team/strategy) + extra(/team/strategy/extra) + rank(榜单条目, 可缺) + 静态数据
输出: 一个 dict, 覆盖 运营思路 / 搜牌节奏 / 装备分级 / 主C主T副C副T / 羁绊(前排后排) / 场上站位

刻意把"事实"与"推断"分开:
  - 棋盘、羁绊计数、装备来源都来自解出来的数据, 属于事实;
  - 主C/主T 的角色打标优先用站点榜单的 heroId, 其次用启发式评分, 并在 reason 里写清依据;
  - 攻略文本里的结论原样引用, 不替用户改写.
"""
from __future__ import annotations

import json
import re

from . import config
from .gamedata import GameData
from .codec import Codec, ascii_board, board_positions

_STAGE_RE = re.compile(r"(\d-\d)|(\d)阶段")
_SENTENCE_SPLIT_RE = re.compile(r"[。；\n]+")
# 攻略文本里的"必做/关键"信号, 用于把某件装备提到必做档
_MUST_HINTS = ("必做", "必出", "最关键", "核心装", "优先级高", "优先抢")
_MUST_FORBIDDEN_HINTS = ("优先级低",)


# ---------------------------------------------------------------- 入口
def analyze(strategy: dict, extra: dict | None, rank: dict | None,
            game: GameData, codec: Codec, source_url: str = "") -> dict:
    board = _decode_board(strategy, codec)
    early_slots = codec.decode(strategy.get("earlyTeamUrl") or "").get("slots") or [None] * config.MAP_SLOTS
    early_board = board_positions(early_slots)
    rank_units = _rank_units(rank)
    author_equips = _author_equips(strategy, game)
    unit_sources = _unit_item_sources(board, rank_units, author_equips, game)

    units = _merge_units(board, rank_units, game)
    roles = _assign_roles(units, rank)
    equipment = _build_equipment(units, roles, unit_sources, author_equips, strategy, game)
    traits = _build_traits(units, game)
    stats = _build_stats(extra, game, roles)

    return {
        "comp": _comp_info(strategy, rank, source_url),
        "plan": _build_plan(strategy, game, units, stats),
        "board": {
            "units": units,
            "grid": _grid(board),
            "ascii": ascii_board(board["slots"]),
            "rows": config.BOARD_ROWS,
            "cols": config.BOARD_COLS,
            "early": {
                "names": [(u.get("hero") or {}).get("displayName") for u in early_board],
                "ascii": ascii_board(early_slots),
                "positions": [{"name": (u.get("hero") or {}).get("displayName"),
                               "row": u["row"], "col": u["col"]} for u in early_board],
            },
            "augments": _build_augments(board["hexs"], game),
        },
        "roles": roles,
        "equipment": equipment,
        "traits": traits,
        "stats": stats,
        "notes": _build_notes(units, traits, strategy),
    }


# ---------------------------------------------------------------- 抓取数据整形
def _rank_units(rank: dict | None) -> dict:
    """榜单条目里的 heros -> {chessId: {stars, equips[]}}."""
    out = {}
    for h in (rank or {}).get("heros") or []:
        cid = str(h.get("id"))
        out[cid] = {
            "stars": h.get("stars"),
            "keys": list(h.get("tftEquips") or []),
        }
    return out


def _author_equips(strategy: dict, game: GameData) -> dict:
    """攻略里的 equips: [{heroId, equips: [[..],[..],[..]]}] -> {chessId: {schemes, union}}.

    三个子数组是站点给的三套出装方案(有些阵容只填了其中一套), 这里既保留方案也给出并集,
    并集用于和棋盘/榜单做交叉比对.
    """
    out = {}
    for entry in strategy.get("equips") or []:
        cid = str(entry.get("heroId"))
        schemes, union, seen = [], [], set()
        for group in entry.get("equips") or []:
            refs = []
            for eid in group or []:
                e = game.equip(eid)
                if not e:
                    continue
                ref = _equip_ref(game, equip=e)
                refs.append(ref)
                if ref["key"] not in seen:
                    seen.add(ref["key"])
                    union.append(ref)
            schemes.append(refs)
        if union:
            out[cid] = {"schemes": schemes, "union": union}
    return out


def _decode_board(strategy: dict, codec: Codec) -> dict:
    url = strategy.get("teamUrl") or ""
    return codec.decode(url)


def _merge_units(board: dict, rank_units: dict, game: GameData) -> list[dict]:
    """棋盘棋子 + 榜单星级/成装, 统一成一个列表 (按 行/列 排序)."""
    units = []
    for pos in board_positions(board["slots"]):
        hero = pos.get("hero") or game.hero(pos["id"]) or {}
        cid = str(pos["id"])
        rk = rank_units.get(cid, {})
        stars = pos.get("stars") or rk.get("stars")
        units.append({
            "chessId": cid,
            "name": hero.get("displayName") or cid,
            "title": hero.get("title"),
            "price": hero.get("price"),
            "role": hero.get("role"),
            "attackRange": hero.get("attackRange"),
            "hp": hero.get("lifeData"),
            "races": hero.get("races") or [],
            "jobs": hero.get("jobs") or [],
            "skillName": hero.get("skillName"),
            "recEquip": _rec_equips(hero, game),
            "row": pos["row"],
            "col": pos["col"],
            "zone": pos["zone"],
            "lcr": pos["lcr"],
            "index": pos["index"],
            "stars": stars,
            "isMain": bool(pos.get("isMain")),
            "equips": [_equip_ref(game, equip=e) for e in pos.get("equips") or []],
            "rankEquips": [_equip_ref(game, key=k) for k in rk.get("keys") or []],
            "extraTraits": pos.get("extraTraits") or [],
        })
    units.sort(key=lambda u: (u["row"], u["col"]))
    return units


def _rec_equips(hero: dict, game: GameData) -> list[dict]:
    out = []
    for eid in str(hero.get("recEquip") or "").split(","):
        e = game.equip(eid.strip()) if eid.strip().isdigit() else None
        if e:
            out.append(_equip_ref(game, equip=e))
    return out


def _equip_ref(game: GameData, equip: dict = None, id=None, key: str = None) -> dict | None:
    """统一装备引用: {id, key, name, nickname, type2, tags, effect}."""
    e = equip or (game.equip(id) if id is not None else None) or (game.equip_by_key(key) if key else None)
    if not e:
        return {"id": None, "key": key, "name": key or "?", "tags": [], "type2": None, "effect": ""}
    return {
        "id": e.get("equipId"),
        "key": e.get("key"),
        "name": e.get("name"),
        "nickname": e.get("nickname"),
        "otherNames": e.get("otherNames"),
        "type2": e.get("type2"),
        "effect": e.get("effect") or "",
        "tags": classify_equip(e),
    }


def classify_equip(equip: dict) -> list[str]:
    """功能标签: 破甲 / 破魔抗 / 重伤 / 纹章."""
    text = (equip.get("effect") or "") + (equip.get("effectAttr") or "")
    tags = []
    if any(k in text for k in config.SHRED_ARMOR_KEYWORDS):
        tags.append("破甲")
    if any(k in text for k in config.SHRED_MAGIC_KEYWORDS):
        tags.append("破魔抗")
    if any(k in text for k in config.WOUND_KEYWORDS):
        tags.append("重伤")
    if str(equip.get("type2") or "").startswith("zhuanzhi"):
        tags.append("纹章")
    return tags


# ---------------------------------------------------------------- 角色定位
def _assign_roles(units: list[dict], rank: dict | None) -> dict:
    """主C/主T/副C/副T. 主C 优先用榜单 heroId; 其余按 站位 + 星级 + 装备数 + 血量 打分."""
    main_carry_id = str((rank or {}).get("heroId") or "")
    carry_pool = [u for u in units if u["zone"] == "后排"] or list(units)
    tank_pool = [u for u in units if u["zone"] == "前排"] or list(units)

    def carry_score(u):
        return (len(u["equips"]) + len(u["rankEquips"])) * 100 + (u["stars"] or 0) * 50 \
            + (u["price"] or 0) * 10 + (u["attackRange"] or 0)

    def tank_score(u):
        hp = _first_number(u.get("hp"))
        return (len(u["equips"]) + len(u["rankEquips"])) * 100 + (u["stars"] or 0) * 50 \
            + (u["price"] or 0) * 10 + hp / 100

    main_carry = next((u for u in units if u["chessId"] == main_carry_id), None)
    if main_carry is None:
        main_carry = max(carry_pool, key=carry_score)
    main_tank = max([u for u in tank_pool if u is not main_carry] or tank_pool, key=tank_score)

    rest = [u for u in units if u not in (main_carry, main_tank)]
    sub_carry = None
    back_rest = [u for u in rest if u["zone"] == "后排"]
    if back_rest:
        sub_carry = max(back_rest, key=carry_score)
    sub_tank = None
    front_rest = [u for u in rest if u["zone"] == "前排" and u is not sub_carry]
    if front_rest:
        sub_tank = max(front_rest, key=tank_score)

    def brief(u, role, reason):
        if not u:
            return None
        return {
            "role": role, "chessId": u["chessId"], "name": u["name"], "price": u["price"],
            "stars": u["stars"], "row": u["row"], "col": u["col"], "zone": u["zone"],
            "heroRole": u["role"], "attackRange": u["attackRange"], "hp": u["hp"],
            "skillName": u["skillName"],
            "equips": [e["name"] for e in u["equips"]] or [e["name"] for e in u["rankEquips"]],
            "traits": list(u["races"]) + list(u["jobs"]),
            "reason": reason,
        }

    return {
        "main_carry": brief(
            main_carry, "主C",
            f"站位 第{main_carry['row']}行第{main_carry['col']}列(后排); 榜单 heroId 指定" if main_carry_id
            else f"后排中装备/星级/费用最高 (第{main_carry['row']}行第{main_carry['col']}列)"),
        "main_tank": brief(
            main_tank, "主T",
            f"前排承伤位, 装备 {len(main_tank['equips'])} 件 / 血量 {main_tank['hp']}"),
        "sub_carry": brief(sub_carry, "副C", "后排第二输出点"),
        "sub_tank": brief(sub_tank, "副T", "前排第二承伤位"),
        "others": [
            {"name": u["name"], "role": _slot_role(u), "row": u["row"], "col": u["col"],
             "price": u["price"], "traits": list(u["races"]) + list(u["jobs"])}
            for u in units if u not in (main_carry, main_tank, sub_carry, sub_tank)
        ],
    }


def _slot_role(u: dict) -> str:
    if u["zone"] == "后排":
        return "后排输出/功能"
    return "前排承伤/功能"


def _first_number(text) -> float:
    m = re.search(r"\d+", str(text or ""))
    return float(m.group(0)) if m else 0.0


# ---------------------------------------------------------------- 装备
def _unit_item_sources(board: dict, rank_units: dict, author_equips: dict,
                       game: GameData) -> dict:
    """{chessId: {equipKey: {"ref": 装备, "sources": set(...)}}} 三个来源对照."""
    out: dict[str, dict] = {}

    def add(cid, ref, source):
        if not ref or not ref.get("key"):
            return
        entry = out.setdefault(str(cid), {}).setdefault(ref["key"], {"ref": ref, "sources": set()})
        entry["sources"].add(source)

    for pos in board_positions(board["slots"]):
        for e in pos.get("equips") or []:
            add(pos["id"], _equip_ref(game, equip=e), "棋盘")
    for cid, info in rank_units.items():
        for k in info.get("keys") or []:
            add(cid, _equip_ref(game, key=k), "榜单")
    for cid, info in author_equips.items():
        for ref in info["union"]:
            add(cid, ref, "攻略")
    return out


def _build_equipment(units, roles, unit_sources, author_equips, strategy, game) -> dict:
    def unit_by_role(role):
        r = roles.get(role)
        return next((u for u in units if r and u["chessId"] == r["chessId"]), None)

    main_c = unit_by_role("main_carry")
    main_t = unit_by_role("main_tank")
    sub_c = unit_by_role("sub_carry")
    sub_t = unit_by_role("sub_tank")

    def items_of(unit, min_sources=1):
        if not unit:
            return []
        rows = []
        for key, info in (unit_sources.get(unit["chessId"]) or {}).items():
            if len(info["sources"]) >= min_sources:
                rows.append({**info["ref"], "sources": sorted(info["sources"]),
                             "n_sources": len(info["sources"])})
        rows.sort(key=lambda r: (-r["n_sources"], r["name"] or ""))
        return rows

    core_items = items_of(main_c, 3) + items_of(main_t, 3)
    # 攻略文本里明确点名的必做装(如"女警最关键的羊刀必做")
    text_must = _text_required_items(strategy, game)

    shred_used, wound_used = [], []
    for unit in (main_c, main_t, sub_c, sub_t):
        for row in items_of(unit):
            for tag in row["tags"]:
                entry = {**row, "owner": unit["name"], "owner_role": _role_of(unit, roles)}
                if tag in ("破甲", "破魔抗") and not _dupe(shred_used, entry):
                    shred_used.append(entry)
                if tag == "重伤" and not _dupe(wound_used, entry):
                    wound_used.append(entry)

    shred_options = [o for o in _option_items(game, config.SHRED_ARMOR_KEYWORDS + config.SHRED_MAGIC_KEYWORDS)
                     if not _used(shred_used, o)]
    wound_options = [o for o in _option_items(game, config.WOUND_KEYWORDS)
                     if not _used(wound_used, o)]

    priority = []
    for row in core_items:
        priority.append({"order": 1, "tier": "必做核心装", "name": row["name"],
                         "owner": _owner_of(row["key"], unit_sources, game),
                         "reason": f"{row['n_sources']} 个来源一致(棋盘/榜单/攻略)"})
    for row in text_must:
        hit = next((p for p in priority if p["name"] == row["name"]), None)
        if hit:
            hit["highlight"] = True
            if row.get("alias"):
                hit["reason"] += f"; 攻略原文写作「{row['alias']}」并写明必做"
        else:
            priority.insert(0, {"order": 1, "tier": "必做核心装", "name": row["name"],
                                "owner": row.get("owner", "主C"), "highlight": True,
                                "reason": f"攻略文本明确要求(写作「{row.get('alias')}」)"})
    for row in shred_used:
        priority.append({"order": 2, "tier": "功能装·破抗", "name": row["name"],
                         "owner": row["owner"], "reason": " / ".join(row["tags"])})
    if not shred_used:
        for row in shred_options[:2]:
            priority.append({"order": 2, "tier": "功能装·破抗", "name": row["name"],
                             "owner": "前排或物理输出位", "reason": "阵容缺少破抗手段, 需补"})
    for row in wound_used:
        priority.append({"order": 2, "tier": "功能装·重伤", "name": row["name"],
                         "owner": row["owner"], "reason": " / ".join(row["tags"])})
    if not wound_used:
        for row in wound_options[:2]:
            priority.append({"order": 2, "tier": "功能装·重伤", "name": row["name"],
                             "owner": "前排或技能范围伤害位", "reason": "阵容缺少重伤手段, 需补"})
    for row in items_of(main_c):
        priority.append({"order": 3, "tier": "主C装备", "name": row["name"],
                         "owner": main_c["name"], "reason": "主C成型装"})
    for row in items_of(main_t):
        priority.append({"order": 4, "tier": "主T装备", "name": row["name"],
                         "owner": main_t["name"], "reason": "主T成型装"})
    for row in items_of(sub_c):
        priority.append({"order": 5, "tier": "副C装备", "name": row["name"],
                         "owner": sub_c["name"], "reason": "副C成型装"})
    for row in items_of(sub_t):
        priority.append({"order": 5, "tier": "副T装备", "name": row["name"],
                         "owner": sub_t["name"], "reason": "副T成型装"})
    priority = _dedupe_priority(priority)

    return {
        "must_core": core_items,
        "text_must": text_must,
        "damage_profile": _damage_profile(main_c, sub_c),
        "shred": {"used": shred_used, "options": shred_options},
        "wound": {"used": wound_used, "options": wound_options},
        "carry": {"unit": main_c["name"] if main_c else None, "items": items_of(main_c),
                  "schemes": _schemes_of(main_c, author_equips)},
        "tank": {"unit": main_t["name"] if main_t else None, "items": items_of(main_t),
                 "schemes": _schemes_of(main_t, author_equips)},
        "sub_carry": {"unit": sub_c["name"] if sub_c else None, "items": items_of(sub_c),
                      "schemes": _schemes_of(sub_c, author_equips)},
        "sub_tank": {"unit": sub_t["name"] if sub_t else None, "items": items_of(sub_t),
                     "schemes": _schemes_of(sub_t, author_equips)},
        "other_units": [
            {"unit": u["name"], "items": items_of(u), "schemes": _schemes_of(u, author_equips)}
            for u in units if u not in (main_c, main_t, sub_c, sub_t)
            and (items_of(u) or _schemes_of(u, author_equips))
        ],
        "priority": sorted(priority, key=lambda p: (p["order"], 0 if p.get("highlight") else 1)),
    }


def _damage_profile(main_c, sub_c) -> dict:
    """按主C/副C的英雄定位推断伤害类型, 用来决定破抗该优先破甲还是破魔抗.

    这是**推断**(站点没有直接给"阵容伤害类型"字段), 所以文档里会标明是推断.
    """
    def kind(unit):
        role = str((unit or {}).get("role") or "")
        if "物理" in role:
            return "物理"
        if "魔法" in role:
            return "魔法"
        return "未知"

    main, sub = kind(main_c), kind(sub_c)
    types = {t for t in (main, sub) if t != "未知"}
    if types == {"物理"}:
        advice = ("主C/副C 都是物理伤害 → 破抗优先「护甲击碎」(最后的轻语、薄暮法袍); "
                  "「魔抗击碎」(虚空之杖、离子火花)只在对手堆魔抗时再考虑")
    elif types == {"魔法"}:
        advice = ("主C/副C 都是魔法伤害 → 破抗优先「魔抗击碎」(虚空之杖、离子火花); "
                  "「护甲击碎」交给前排(薄暮法袍)即可")
    elif types:
        advice = "物理与魔法输出都有 → 破甲与破魔抗各留一件更稳"
    else:
        advice = ""
    return {"main_carry": main, "sub_carry": sub, "advice": advice}


def _schemes_of(unit, author_equips: dict) -> list[list[str]]:
    if not unit:
        return []
    info = author_equips.get(str(unit["chessId"])) or {}
    out = []
    for refs in info.get("schemes") or []:
        if refs:
            out.append([r["name"] for r in refs])
    return out


def _role_of(unit, roles) -> str:
    for key in ("main_carry", "main_tank", "sub_carry", "sub_tank"):
        r = roles.get(key)
        if r and r["chessId"] == unit["chessId"]:
            return r["role"]
    return "功能位"


def _owner_of(equip_key, unit_sources, game) -> str:
    for cid, items in unit_sources.items():
        if equip_key in items:
            return game.hero_name(cid)
    return "?"


def _dupe(rows, entry) -> bool:
    return any(r["key"] == entry["key"] and r.get("owner") == entry.get("owner") for r in rows)


def _used(rows, ref) -> bool:
    return any(r["key"] == ref.get("key") for r in rows)


def _dedupe_priority(rows) -> list:
    """同一件装备给同一个人只保留最靠前的一档 (必做核心装优先)."""
    seen, out = set(), []
    for r in rows:
        sig = (r["name"], r["owner"])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out


def _option_items(game: GameData, keywords) -> list[dict]:
    """站内所有带该功能关键词的成装(去掉光明版), 作为补装备选."""
    out = []
    for e in game.items_with_keyword(keywords):
        if e.get("type2") != "common":
            continue
        out.append(_equip_ref(game, equip=e))
    return out


def _text_required_items(strategy: dict, game: GameData) -> list[dict]:
    """攻略文本里"必做/最关键"那句点到的装备 -> 装备.

    文本里既可能写全名(死亡之刃)也可能写外号(羊刀/杀人剑), 所以 name/nickname/otherNames 都要试.
    """
    text = " ".join(str(strategy.get(k) or "") for k in ("open", "core", "earlyDesc", "midDesc", "finalDesc"))
    hits = []
    for e in game.equips:
        if e.get("type2") != "common":
            continue
        aliases = [e.get("name")] + [e.get("nickname")] + list(e.get("otherNames") or [])
        for alias in aliases:
            if not alias or alias not in text:
                continue
            idx = text.find(alias)
            window = text[max(0, idx - 24): idx + len(alias) + 24]
            if any(h in window for h in _MUST_HINTS) and not any(h in window for h in _MUST_FORBIDDEN_HINTS):
                hits.append({"name": e.get("name"), "alias": alias, "owner": "主C",
                             "quote": window.strip()})
                break
    return hits


# ---------------------------------------------------------------- 羁绊
def _build_traits(units: list[dict], game: GameData) -> dict:
    counts: dict[str, list[dict]] = {}
    emblem_counts: dict[str, list[dict]] = {}
    for u in units:
        for name in list(u["races"]) + list(u["jobs"]):
            counts.setdefault(name, []).append(u)
        for e in list(u["equips"]) + list(u["rankEquips"]):
            if "纹章" in (e.get("tags") or []):
                granted = _emblem_trait(e, game)
                if granted:
                    emblem_counts.setdefault(granted, []).append(u)

    def build(name, members, emblems=None):
        t = game.trait(name=name) or {}
        level_rows = t.get("levels") or []
        levels = [lv.get("level") for lv in level_rows if lv.get("level")]
        active = [lv for lv in levels if lv <= len(members)]
        nxt = next((lv for lv in levels if lv > len(members)), None)
        target = active[-1] if active else (levels[0] if levels else None)
        desc = ""
        for lv in level_rows:
            if lv.get("level") == target:
                desc = lv.get("levelDesc")
        front = [u for u in members if u["row"] in config.FRONT_ROWS]
        back = [u for u in members if u["row"] in config.BACK_ROWS]
        return {
            "name": name,
            "kind": "race" if t.get("kind") == "race" else ("job" if t else "?"),
            "count": len(members),
            "level": active[-1] if active else 0,
            "next": nxt,
            "allLevels": [{"level": lv.get("level"), "desc": lv.get("levelDesc")} for lv in level_rows],
            "desc": desc,
            "introduce": t.get("introduce"),
            "members": [{"name": u["name"], "row": u["row"], "col": u["col"],
                         "zone": u["zone"], "price": u["price"], "stars": u["stars"]} for u in members],
            "front": [u["name"] for u in front],
            "back": [u["name"] for u in back],
            "frontCount": len(front),
            "backCount": len(back),
            "side": _trait_side(front, back),
            "emblems": [u["name"] for u in (emblems or [])],
        }

    active, inactive = [], []
    for name, members in counts.items():
        info = build(name, members, emblem_counts.get(name))
        (active if info["level"] > 0 else inactive).append(info)
    active.sort(key=lambda t: (-t["level"], -t["count"], t["name"]))
    inactive.sort(key=lambda t: (-t["count"], t["name"]))

    # 纹章带来的额外计数: 不管该羁绊是否已由棋子提供, 都单独列出来 (棋盘上一般不显示纹章)
    emblem_granted = []
    for name, bearers in emblem_counts.items():
        base_members = list(counts.get(name, []))
        total = list(base_members)
        for u in bearers:
            if u not in total:
                total.append(u)
        emblem_granted.append({
            "name": name,
            "base": len(base_members),
            "total": len(total),
            "emblems": [u["name"] for u in bearers],
        })
    emblem_granted.sort(key=lambda x: -x["total"])

    return {
        "active": active,
        "inactive": inactive,
        "front_traits": [t for t in active if t["side"] == "前排"],
        "back_traits": [t for t in active if t["side"] == "后排"],
        "mixed_traits": [t for t in active if t["side"] == "前后排混合"],
        "emblem_granted": emblem_granted,
    }


def _trait_side(front, back) -> str:
    if front and not back:
        return "前排"
    if back and not front:
        return "后排"
    return "前后排混合"


def _emblem_trait(equip: dict, game: GameData) -> str | None:
    """纹章 effect: "携带者获得【XX】羁绊" -> XX."""
    m = re.search(r"获得【(.+?)】", equip.get("effect") or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- 运营/搜牌
def _build_plan(strategy: dict, game: GameData, units: list[dict], stats: dict) -> dict:
    sections = [
        ("核心思路", strategy.get("core")),
        ("开局条件", strategy.get("open")),
        ("前期(2阶段)", strategy.get("earlyDesc")),
        ("中期(3~4阶段)", strategy.get("midDesc")),
        ("后期(成型)", strategy.get("finalDesc")),
    ]
    nodes = []
    for title, text in sections:
        for part in _SENTENCE_SPLIT_RE.split(str(text or "")):
            part = part.strip()
            if not part:
                continue
            for m in _STAGE_RE.finditer(part):
                nodes.append({"phase": title, "node": m.group(0), "text": part})
    level_plan = _level_plan(strategy)
    return {
        "sections": [{"title": t, "text": (x or "").strip()} for t, x in sections],
        "timeline": nodes,
        "search": {
            "tags": strategy.get("tags") or [],
            "level_plan": level_plan,
            "roll_targets": _roll_targets(units, strategy),
            "shop_odds": _shop_odds(game, level_plan),
            "pool": _pool_size(game),
            "avg_level": ((stats or {}).get("base") or {}).get("level"),
            "notes": _search_notes(strategy),
        },
        "conditions": _conditions(strategy),
    }


def _level_plan(strategy: dict) -> list[dict]:
    """"3-2拉6" / "8人口" 这类节点抽成表."""
    text = " ".join(str(strategy.get(k) or "") for k in ("open", "earlyDesc", "midDesc", "finalDesc"))
    plan = []
    for m in re.finditer(r"(\d)-(\d)\s*(?:拉|上|升)\s*(\d)", text):
        plan.append({"node": f"{m.group(1)}-{m.group(2)}", "action": f"拉{int(m.group(3))}人口",
                     "target": int(m.group(3))})
    for m in re.finditer(r"(\d+)\s*人口", text):
        lvl = int(m.group(1))
        if not any(p.get("target") == lvl for p in plan):
            plan.append({"node": "", "action": f"{lvl}人口", "target": lvl})
    if not plan:
        for m in re.finditer(r"(\d)-(\d)", text):
            plan.append({"node": m.group(0), "action": "", "target": None})
    return plan


def _roll_targets(units: list[dict], strategy: dict) -> list[dict]:
    """追三目标: 优先用站点榜单星级(=3), 再用攻略文本里"三星<棋子名>"补齐."""
    targets = []
    for u in units:
        if (u.get("stars") or 0) >= 3:
            targets.append({"name": u["name"], "price": u["price"], "chessId": u["chessId"],
                            "row": u["row"], "col": u["col"], "source": "站点榜单星级=3"})
    text = " ".join(str(strategy.get(k) or "") for k in ("open", "core", "earlyDesc", "midDesc", "finalDesc"))
    for u in units:
        if any(t["chessId"] == u["chessId"] for t in targets):
            continue
        if f"三星{u['name']}" in text or f"{u['name']}三星" in text:
            targets.append({"name": u["name"], "price": u["price"], "chessId": u["chessId"],
                            "row": u["row"], "col": u["col"], "source": "攻略文本点名"})
    return targets


def _search_notes(strategy: dict) -> list[str]:
    notes = []
    mid = str(strategy.get("midDesc") or "")
    if "利息" in mid:
        notes.append("文本要求卡利息慢D(不要一次性梭哈)")
    if "不买经验" in str(strategy.get("earlyDesc") or ""):
        notes.append("2阶段不买经验, 纯攒钱走连败经济")
    return notes


def _pool_size(game: GameData) -> list[dict]:
    """各费卡池张数 (quantitys18: [1费,2费,3费,4费,5费]), 追三星要吃 9 张."""
    q = game.dataset.get(f"quantitys{game.season}") or []
    return [{"cost": i + 1, "copies": int(q[i])} for i in range(len(q))]


def _shop_odds(game: GameData, level_plan: list[dict]) -> list[dict]:
    """按等级给商店各费卡概率 (rates18: 9 级 × 5 费, 每级 5 项)."""
    rates = game.dataset.get(f"rates{game.season}") or []
    if len(rates) < 45:
        return []
    levels = sorted({p["target"] for p in level_plan if p.get("target")})
    out = []
    for lvl in levels:
        base = (lvl - 1) * 5
        row = rates[base:base + 5]
        if len(row) == 5:
            out.append({"level": lvl, "odds": {i + 1: row[i] for i in range(5)}})
    return out


def _conditions(strategy: dict) -> list[dict]:
    raw = strategy.get("conditions")
    if not raw:
        return []
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out = []
    for item in doc.get("filterOptionResults") or []:
        out.append({"name": item.get("name"), "type": item.get("type"),
                    "level": item.get("levelValue"), "equip": item.get("equipValue")})
    return out


# ---------------------------------------------------------------- 其他
def _comp_info(strategy: dict, rank: dict | None, source_url: str) -> dict:
    difficulty = strategy.get("difficulty")
    name = _jcc_code_name(strategy.get("jccCode"))
    return {
        "id": strategy.get("id"),
        "title": strategy.get("title"),
        "titleApi": strategy.get("titleApi") or [],
        "tier": (rank or {}).get("tier"),
        "heroId": (rank or {}).get("heroId"),
        "emblemId": (rank or {}).get("equipId"),
        "difficulty": difficulty,
        "difficultyText": config.DIFFICULTY_TEXT.get(difficulty, "-"),
        "tags": strategy.get("tags") or [],
        "playRate": strategy.get("playRate"),
        "won": strategy.get("won"),
        "top4": strategy.get("top4"),
        "place": strategy.get("place"),
        "updateTime": strategy.get("updateTime"),
        "jccCode": strategy.get("jccCode"),
        "jccName": name,
        "tftCode": (rank or {}).get("tftCode") or strategy.get("tftCode"),
        "sourceUrl": source_url,
    }


def _jcc_code_name(code: str | None) -> str | None:
    if not code:
        return None
    m = re.search(r"#(.+?)#", code)
    return m.group(1) if m else None


def _grid(board: dict) -> list[list[dict | None]]:
    grid = [[None] * config.BOARD_COLS for _ in range(config.BOARD_ROWS)]
    for pos in board_positions(board["slots"]):
        hero = pos.get("hero") or {}
        grid[pos["row"] - 1][pos["col"] - 1] = {
            "name": hero.get("displayName") or str(pos["id"]),
            "price": hero.get("price"),
            "equips": [e.get("name") for e in (pos.get("equips") or [])],
        }
    return grid


def _build_augments(hex_ids: list[str], game: GameData) -> list[dict]:
    out = []
    for hid in hex_ids:
        found = game.hex(hid)
        if not found:
            out.append({"hexId": hid, "group": None, "name": hid, "desc": "", "round": None})
            continue
        gi, h = found
        out.append({"hexId": hid, "group": gi, "name": h.get("name"),
                    "desc": h.get("desc"), "round": h.get("round")})
    return out


def _build_stats(extra: dict | None, game: GameData, roles: dict) -> dict:
    """实战装备统计.

    注意: `/team/strategy/extra` 的 explorerDataList 按"关注棋子"各给一份, 但里面
    `unitEquips` / `equipBuilds` 是整个阵容混在一起的流水, 必须按 `unitKey` 再过滤,
    否则三个弈子会打印出同一张表. `base` 则是阵容级统计.
    """
    if not extra:
        return {"units": [], "base": None}
    out = []
    base = None
    for entry in extra.get("explorerDataList") or []:
        cid = str(entry.get("heroId"))
        hero = game.hero(cid) or {}
        key = hero.get("key")
        data = entry.get("data") or {}
        base = base or data.get("base")
        builds = [b for b in (data.get("equipBuilds") or []) if _same_unit(b, key)]
        singles = [s for s in (data.get("unitEquips") or []) if _same_unit(s, key)]
        builds = sorted(builds, key=lambda x: -(x.get("count") or 0))[:6]
        singles = sorted(singles, key=lambda x: -(x.get("count") or 0))[:8]
        out.append({
            "chessId": cid,
            "name": game.hero_name(cid),
            "key": key,
            "role": _role_by_id(roles, cid),
            "builds": [{
                "items": [_equip_ref(game, key=k)["name"] for k in (b.get("key") or [])],
                "count": b.get("count"), "top4": b.get("top4"), "place": b.get("place"),
                "won": b.get("won"),
            } for b in builds],
            "singles": [{
                "name": _equip_ref(game, key=s.get("key"))["name"],
                "key": s.get("key"),
                "count": s.get("count"), "top4": s.get("top4"), "place": s.get("place"),
                "won": s.get("won"),
                "tags": _equip_ref(game, key=s.get("key"))["tags"],
            } for s in singles],
        })
    return {"units": out, "base": base}


def _same_unit(row: dict, key: str | None) -> bool:
    if not row.get("unitKey"):
        return True   # 没有 unitKey 的行无法区分, 保守保留
    return str(row.get("unitKey")) == str(key)


def _role_by_id(roles: dict, cid: str) -> str:
    for key in ("main_carry", "main_tank", "sub_carry", "sub_tank"):
        r = roles.get(key)
        if r and str(r["chessId"]) == cid:
            return r["role"]
    return "功能位"


def _build_notes(units: list[dict], traits: dict, strategy: dict) -> list[str]:
    notes = []
    n = len(units)
    if n > 8:
        notes.append(f"网站给出的最终棋盘共 {n} 个棋子, 比 8 人口多 {n - 8} 个, "
                     f"对照攻略文本的等级规划时注意这点")
    text = str(strategy.get("finalDesc") or "")
    active = {t["name"]: t["count"] for t in traits.get("active") or []}
    mismatch = []
    for m in re.finditer(r"(\d)([\u4e00-\u9fa5]{2,6})", text):
        want, name = int(m.group(1)), m.group(2)
        got = None
        for tname, count in active.items():
            if tname == name or tname in name or name in tname:
                got = count
                break
        if got is not None and got != want:
            mismatch.append(f"攻略文本写「{want}{name}」, 实测棋盘为 {got}{name}")
    for item in dict.fromkeys(mismatch):
        notes.append(item)
    unnamed = [u["name"] for u in units if str(u["name"]).startswith("未命名(")]
    if unnamed:
        notes.append("棋盘上有站点自身也没有起名的单位(" + "、".join(unnamed)
                     + "): 这些 id 在站点的名称表里就缺失, 这里按 id 原样标出, 不做猜测")
    return notes
