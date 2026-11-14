"""把 analyze.py 的结果渲染成中文 Markdown 文档.

文档结构对应用户要理清的要点:
    运营思路 / 搜牌节奏 / 所需装备(必做·破抗·重伤·主C·主T·副C副T) / 主C主T副C副T /
    阵容羁绊(前排·后排) / 场上阵容(逐子第几行第几列)
"""
from __future__ import annotations

import re

from . import config


def render(analysis: dict) -> str:
    out = []
    comp = analysis["comp"]
    out.append(f"# {comp['title']} 阵容分析")
    out.append("")
    out.append(_summary_table(comp))
    out.append("")
    out.append("> 数据由 `python -m scrape_comp` 从 " + config.SITE_NAME + " 抓取并解密生成; "
               "站位/装备/羁绊为解密棋盘后的实测值, 攻略文字原样引用站点内容。")
    out.append("")
    out.append(_plan_section(analysis["plan"]))
    out.append(_search_section(analysis["plan"], analysis["comp"]))
    out.append(_board_section(analysis["board"]))
    out.append(_roles_section(analysis["roles"]))
    out.append(_equipment_section(analysis["equipment"], analysis["stats"]))
    out.append(_traits_section(analysis["traits"]))
    out.append(_augment_section(analysis["board"]["augments"]))
    out.append(_appendix(analysis))
    return "\n".join(out)


# ---------------------------------------------------------------- 各部分
def _summary_table(comp: dict) -> str:
    rows = [
        ("阵容名", comp["title"]),
        ("官网评级", comp.get("tier") or "-"),
        ("难度", f"{comp.get('difficultyText')} (difficulty={comp.get('difficulty')})"),
        ("标签", "、".join(comp.get("tags") or []) or "-"),
        ("登场率", _pct(comp.get("playRate"))),
        ("前四率", _pct(comp.get("top4"))),
        ("登顶率", _pct(comp.get("won"))),
        ("平均名次", comp.get("place")),
        ("阵容码名", comp.get("jccName") or "-"),
        ("更新于", comp.get("updateTime")),
        ("数据来源", comp.get("sourceUrl") or "-"),
    ]
    lines = ["| 项 | 值 |", "|:---|:---|"]
    lines += [f"| {k} | {v if v not in (None, '') else '-'} |" for k, v in rows]
    code = comp.get("jccCode") or ""
    if code:
        lines.append("")
        lines.append(f"**游戏内阵容码**: `{code}`")
    if comp.get("tftCode"):
        lines.append("")
        lines.append(f"**TFT 阵容码**: `{comp['tftCode']}`")
    return "\n".join(lines)


def _pct(v):
    if v is None:
        return "-"
    try:
        return f"{float(v):.2f}%"
    except (TypeError, ValueError):
        return str(v)


def _plan_section(plan: dict) -> str:
    out = ["## 一、运营思路", ""]
    for sec in plan["sections"]:
        if not sec["text"]:
            continue
        out.append(f"**{sec['title']}**: {sec['text']}")
        out.append("")
    if plan.get("conditions"):
        out.append("**开局/成型条件**(站点筛选项):")
        out.append("")
        out.append("| 对象 | 类型 | 星级要求 | 装备要求 |")
        out.append("|:---|:---|:---|:---|")
        for c in plan["conditions"]:
            out.append(f"| {c['name']} | {c['type']} | {c['level']} | {c['equip']} |")
        out.append("")
    if plan.get("timeline"):
        out.append("**阶段要点**(从上面文字里抽出的带回合节点句):")
        out.append("")
        out.append("| 阶段 | 节点 | 要点 |")
        out.append("|:---|:---|:---|")
        for n in plan["timeline"]:
            out.append(f"| {n['phase']} | {n['node']} | {n['text']} |")
        out.append("")
    return "\n".join(out)


def _search_section(plan: dict, comp: dict) -> str:
    search = plan.get("search") or {}
    out = ["## 二、搜牌节奏", ""]
    if comp.get("tags"):
        out.append(f"- 站点标签: {'、'.join(comp['tags'])}")
    if search.get("level_plan"):
        nodes = []
        for p in search["level_plan"]:
            nodes.append(f"{p['node'] + ' ' if p['node'] else ''}{p['action']}".strip())
        out.append("- 等级规划: " + " → ".join(nodes))
    if search.get("avg_level"):
        out.append(f"- 站点实战数据的平均结束等级: {search['avg_level']}(说明该阵容通常打到 8 级)")
    for note in search.get("notes") or []:
        out.append(f"- {note}")
    out.append("")

    targets = search.get("roll_targets") or []
    if targets:
        out.append("**追三目标**:")
        out.append("")
        out.append("| 棋子 | 费用 | 站位 | 依据 |")
        out.append("|:---|:---|:---|:---|")
        for t in targets:
            pos = f"第{t['row']}行第{t['col']}列" if t.get("row") else "-"
            out.append(f"| {t['name']} | {t['price']}费 | {pos} | {t['source']} |")
        out.append("")
    if search.get("shop_odds"):
        out.append("**对应等级的商店刷新概率**(站点 rates18 数据):")
        out.append("")
        out.append("| 等级 | 1费 | 2费 | 3费 | 4费 | 5费 |")
        out.append("|:---|:---|:---|:---|:---|:---|")
        for row in search["shop_odds"]:
            odds = row["odds"]
            out.append(f"| {row['level']} | " + " | ".join(str(odds.get(i, "-")) for i in range(1, 6)) + " |")
        out.append("")
    if search.get("pool"):
        pools = "、".join(f"{p['cost']}费 {p['copies']} 张" for p in search["pool"])
        out.append(f"**卡池数量**: {pools}; 任一棋子三星需要 9 张(卡池被抢得越多越难追)。")
        out.append("")
    return "\n".join(out)


def _board_section(board: dict) -> str:
    out = ["## 三、场上阵容与站位", ""]
    rows, cols = board["rows"], board["cols"]
    out.append(f"棋盘为 {rows} 行 × {cols} 列; **第 1 行是最靠近敌方的前排, 第 {rows} 行是我方底线**; "
               f"列按地图从左到右编号 1~{cols}。")
    out.append("")
    out.append("| 行\\列 | " + " | ".join(f"第{c}列" for c in range(1, cols + 1)) + " |")
    out.append("|" + ":---|" * (cols + 1))
    for r in range(rows):
        cells = []
        for c in range(cols):
            cell = board["grid"][r][c]
            cells.append(cell["name"] if cell else "—")
        out.append(f"| **第{r + 1}行** | " + " | ".join(cells) + " |")
    out.append("")
    out.append("```")
    out.append(board["ascii"])
    out.append("```")
    out.append("")
    out.append("**逐子位置**:")
    out.append("")
    out.append("| 棋子 | 费用 | 星级 | 位置 | 区域 | 站位 | 携带装备 |")
    out.append("|:---|:---|:---|:---|:---|:---|:---|")
    for u in board["units"]:
        pos = f"第{u['row']}行第{u['col']}列"
        equips = "、".join(e["name"] for e in u["equips"]) or "—"
        out.append(f"| {u['name']} | {u['price']}费 | {_stars(u['stars'])} | {pos} | {u['zone']} | "
                   f"{u['lcr']} | {equips} |")
    out.append("")
    early = board.get("early") or {}
    if early.get("ascii") and any(early.get("names") or []):
        out.append("**前期过渡棋盘** (站点 earlyTeamUrl 解出):")
        out.append("")
        out.append("```")
        out.append(early["ascii"])
        out.append("```")
        out.append("")
    return "\n".join(out)


def _stars(v):
    if not v:
        return "—"
    return "★" * int(v)


def _roles_section(roles: dict) -> str:
    out = ["## 四、主C / 主T / 副C / 副T", ""]
    order = [("main_carry", "主C(输出核心)"), ("main_tank", "主T(抗伤核心)"),
             ("sub_carry", "副C(第二输出)"), ("sub_tank", "副T(第二承伤)")]
    out.append("| 定位 | 棋子 | 费用 | 星级 | 站位 | 英雄定位 | 攻击距离 | 血量 | 装备 | 判定依据 |")
    out.append("|:---|:---|:---|:---|:---|:---|:---|:---|:---|:---|")
    for key, label in order:
        r = roles.get(key)
        if not r:
            out.append(f"| {label} | — | | | | | | | | 本阵容无 |")
            continue
        out.append(f"| {label} | **{r['name']}** | {r['price']}费 | {_stars(r['stars'])} | "
                   f"第{r['row']}行第{r['col']}列 | {r['heroRole']} | {r['attackRange']} | {r['hp']} | "
                   f"{'、'.join(r['equips']) or '—'} | {r['reason']} |")
    out.append("")
    if roles.get("others"):
        out.append("**其余棋子(功能位, 一般不占装备)**:")
        out.append("")
        out.append("| 棋子 | 定位 | 站位 | 费用 | 羁绊 |")
        out.append("|:---|:---|:---|:---|:---|")
        for o in roles["others"]:
            out.append(f"| {o['name']} | {o['role']} | 第{o['row']}行第{o['col']}列 | {o['price']}费 | "
                       f"{'、'.join(o['traits'])} |")
        out.append("")
    return "\n".join(out)


def _equipment_section(equip: dict, stats: dict) -> str:
    out = ["## 五、所需装备", ""]
    out.append("### 5.1 优先级总表")
    out.append("")
    out.append("| 优先级 | 档位 | 装备 | 给谁 | 依据 |")
    out.append("|:---|:---|:---|:---|:---|")
    for i, p in enumerate(equip["priority"], start=1):
        out.append(f"| {i} | {p['tier']} | {p['name']} | {p['owner']} | {p['reason']} |")
    out.append("")

    out.append("### 5.2 必须装备")
    out.append("")
    out.append("判定口径: **主C / 主T** 的装备里, 「棋盘快照」「榜单阵容」「攻略推荐」三个来源"
               "都出现的算必做核心装(下方 5.4~5.6 还会给出各自的完整装备)。")
    out.append("")
    if equip["must_core"]:
        out.append("| 装备 | 功能 | 来源 |")
        out.append("|:---|:---|:---|")
        for row in equip["must_core"]:
            out.append(f"| {row['name']} | {'/'.join(row['tags']) or '属性装'} | "
                       f"{'、'.join(row['sources'])} |")
    else:
        out.append("(无三来源一致的装备, 见下方分档)")
    out.append("")
    if equip.get("text_must"):
        out.append("攻略正文里点名的必做装: ")
        for row in equip["text_must"]:
            out.append(f"- **{row['name']}** —— 原文: ……{row['quote']}……")
        out.append("")

    out.append("### 5.3 功能性装备: 破抗 / 重伤")
    out.append("")
    profile = equip.get("damage_profile") or {}
    if profile.get("advice"):
        out.append(f"伤害类型(推断: 主C={profile.get('main_carry')}, 副C={profile.get('sub_carry')}): "
                   f"{profile['advice']}。")
        out.append("")
    out.append("| 功能 | 状态 | 装备 | 归属 | 说明 |")
    out.append("|:---|:---|:---|:---|:---|")
    for tag, bucket in (("破抗(击碎)", equip["shred"]), ("重伤(减疗)", equip["wound"])):
        used = bucket["used"]
        if used:
            for row in used:
                out.append(f"| {tag} | 阵容已覆盖 | {row['name']} | {row['owner']}({row.get('owner_role')}) | "
                           f"{'、'.join(row['tags'])} |")
        else:
            out.append(f"| {tag} | **阵容未覆盖, 需补** | — | — | 见下方备选 |")
        for row in bucket["options"][:5]:
            out.append(f"| {tag} | 备选可合成 | {row['name']} | 前排/输出位 | "
                       f"{_short(row['effect'])} |")
    out.append("")

    out.append("### 5.4 主C装备")
    out.append("")
    out.extend(_item_table(equip["carry"]))
    out.append("")
    out.append("### 5.5 主T装备")
    out.append("")
    out.extend(_item_table(equip["tank"]))
    out.append("")
    out.append("### 5.6 副C / 副T 装备")
    out.append("")
    out.extend(_item_table(equip["sub_carry"]))
    out.append("")
    out.extend(_item_table(equip["sub_tank"]))
    out.append("")
    if equip.get("other_units"):
        out.append("**其他棋子的装备(数据里出现过)**:")
        out.append("")
        for row in equip["other_units"]:
            names = "、".join(i["name"] for i in row["items"]) or "—"
            schemes = f" (攻略方案: {' / '.join(' + '.join(s) for s in row['schemes'])})" \
                if row.get("schemes") else ""
            out.append(f"- {row['unit']}: {names}{schemes}")
        out.append("")

    units = (stats or {}).get("units") or []
    base = (stats or {}).get("base")
    if units:
        out.append("### 5.7 实战装备数据(站点排行榜统计)")
        out.append("")
        if base:
            out.append(f"> 阵容级样本: {base.get('count')} 局, 前四率 {base.get('top4')}%, "
                       f"登顶率 {base.get('won')}%, 平均名次 {base.get('place')}, "
                       f"平均等级 {base.get('level')}。名次列越小越好。")
            out.append("")
        for u in units:
            out.append(f"**{u['name']}** ({u['role']})")
            out.append("")
            if u["builds"]:
                out.append("| 成型三件套 | 场次 | 前四率 | 登顶率 | 名次 |")
                out.append("|:---|:---|:---|:---|:---|")
                for b in u["builds"]:
                    names = " + ".join(x for x in b["items"] if x) or "—"
                    out.append(f"| {names} | {b['count']} | {b['top4']}% | {b['won']}% | {b['place']} |")
                out.append("")
            if u["singles"]:
                out.append("| 单件装备 | 功能 | 场次 | 前四率 | 名次 |")
                out.append("|:---|:---|:---|:---|:---|")
                for s in u["singles"]:
                    out.append(f"| {s['name']} | {'/'.join(s['tags']) or '-'} | {s['count']} | "
                               f"{s['top4']}% | {s['place']} |")
                out.append("")
    return "\n".join(out)


def _item_table(bucket: dict) -> list[str]:
    unit = bucket.get("unit")
    if not unit or not bucket.get("items"):
        return [f"- {unit or '该位置'}: 数据里没有装备记录(纯羁绊/功能位)"]
    lines = []
    for i, scheme in enumerate(bucket.get("schemes") or [], start=1):
        lines.append(f"- 攻略推荐方案{i}: {' + '.join(scheme)}")
    if lines:
        lines.append("")
    lines += [f"**{unit}** 数据里出现过的装备:", "", "| 装备 | 功能 | 来源 |", "|:---|:---|:---|"]
    for row in bucket["items"]:
        lines.append(f"| {row['name']} | {'/'.join(row['tags']) or '属性装'} | {'、'.join(row['sources'])} |")
    return lines


def _short(text: str, limit: int = 60) -> str:
    plain = re.sub(r"<[^>]+>", " ", str(text or ""))
    plain = plain.replace("&nbsp;", " ")
    plain = " ".join(plain.split())
    return plain[:limit] + ("…" if len(plain) > limit else "")


def _traits_section(traits: dict) -> str:
    out = ["## 六、阵容羁绊", ""]
    out.append("### 6.1 已激活羁绊总览")
    out.append("")
    out.append("| 羁绊 | 类型 | 数量 | 激活档位 | 下一档 | 效果 | 站位归属 | 贡献棋子 |")
    out.append("|:---|:---|:---|:---|:---|:---|:---|:---|")
    for t in traits["active"]:
        kind = "种族" if t["kind"] == "race" else ("职业" if t["kind"] == "job" else "?")
        effect = _short(t["introduce"] or "", 60)
        cur = _short(t["desc"] or "", 40)
        cell = effect or ""
        if cur:
            cell = (cell + "<br>" if cell else "") + f"当前档: {cur}"
        out.append(f"| **{t['name']}** | {kind} | {t['count']} | {t['level']} | "
                   f"{t['next'] if t['next'] else '满'} | {cell or '-'} | "
                   f"{t['side']}(前{t['frontCount']}/后{t['backCount']}) | "
                   f"{'、'.join(m['name'] for m in t['members'])} |")
    out.append("")
    out.append("### 6.2 前排羁绊(由前排棋子提供)")
    out.append("")
    if traits["front_traits"]:
        out.extend(_trait_block(traits["front_traits"], "前排"))
    else:
        out.append("本阵容没有「全部由前排棋子提供」的羁绊; "
                   "主宰/峡谷野怪/重装战士/野兽之灵都是前后排混合(见 6.4)。")
    out.append("")
    out.append("### 6.3 后排羁绊(由后排棋子提供)")
    out.append("")
    out.extend(_trait_block(traits["back_traits"], "后排", empty=(
        "本阵容没有「全部由后排棋子提供」的羁绊。")))
    out.append("")
    if traits.get("mixed_traits"):
        out.append("### 6.4 前后排混合羁绊")
        out.append("")
        out.extend(_trait_block(traits["mixed_traits"], "混合"))
        out.append("")
    if traits.get("emblem_granted"):
        out.append("### 6.5 纹章可额外补的羁绊")
        out.append("")
        out.append("| 羁绊 | 棋盘计数 | 带纹章后 | 纹章携带者 |")
        out.append("|:---|:---|:---|:---|")
        for t in traits["emblem_granted"]:
            out.append(f"| {t['name']} | {t['base']} | **{t['total']}** | {'、'.join(t['emblems'])} |")
        out.append("")
        out.append("> 说明: 6.1 的「已激活羁绊总览」只按棋盘上的棋子计算; "
                   "站点榜单阵容里有的棋子带了纹章(棋盘快照里看不到), 上表给出补上纹章后的计数。")
        out.append("")
    if traits.get("inactive"):
        out.append("### 6.6 未激活羁绊(差几件)")
        out.append("")
        out.append("| 羁绊 | 现有 | 最近档位 | 该档效果 |")
        out.append("|:---|:---|:---|:---|")
        for t in traits["inactive"]:
            rows = t.get("allLevels") or []
            need = rows[0]["level"] if rows else None
            desc = _short(rows[0]["desc"] or "", 50) if rows else ""
            out.append(f"| {t['name']} | {t['count']} | {need if need else '-'} | {desc} |")
        out.append("")
    return "\n".join(out)


def _trait_block(items: list[dict], label: str, empty: str = "") -> list[str]:
    if not items:
        return [empty or f"本阵容没有纯{label}羁绊。"]
    lines = []
    for t in items:
        members = "、".join(f"{m['name']}({m['zone']})" for m in t["members"])
        desc = _short(t["introduce"] or "", 70)
        cur = _short(t["desc"] or "", 40)
        lines.append(f"- **{t['name']} {t['count']}** (激活档 {t['level']}; "
                     f"前排 {t['frontCount']} 个 / 后排 {t['backCount']} 个): 由 {members} 提供; "
                     f"效果: {desc or '-'}" + (f"; 当前档 {cur}" if cur and cur != desc else ""))
    return lines


def _augment_section(augments: list[dict]) -> str:
    out = ["## 七、强化符文(海克斯)", ""]
    if not augments:
        out.append("(站点该阵容的 teamUrl 里没有强化符文段)")
        out.append("")
        return "\n".join(out)
    out.append("下面这些是从阵容 teamUrl 的密文里解出来的, 站点把它和棋盘存在一起, "
               "属于该阵容的强化符文记录(不是每个都能自选, 仅作参考)。")
    out.append("")
    out.append("| 组(1银/2金/3棱彩) | 名称 | 可出回合 | 效果 |")
    out.append("|:---|:---|:---|:---|")
    for a in augments:
        rounds = a.get("round")
        rounds = "、".join(rounds) if isinstance(rounds, list) else (rounds or "-")
        out.append(f"| {a.get('group') or '-'} | {a.get('name')} | {rounds} | {_short(a.get('desc') or '', 80)} |")
    out.append("")
    return "\n".join(out)


def _appendix(analysis: dict) -> str:
    comp = analysis["comp"]
    out = ["## 八、数据来源与说明", "",
           f"- 阵容页: {comp.get('sourceUrl') or '-'}",
           f"- 阵容 id: `{comp.get('id')}`; 榜单主C heroId: `{comp.get('heroId')}`; "
           f"阵容纹章 equipId: `{comp.get('emblemId')}`",
           "- 接口: `api.datatft.com` 的 `/team/strategy`(攻略/teamUrl/阵容码)、"
           "`/team/strategy/extra`(实战装备统计)、`/team/rank`(评级/主C/星级成装)",
           "- 棋盘: `teamUrl` 自研码表解密, 56 格中前 28 格为 4×7 我方棋盘(第 1 行最靠敌方)",
           "- 英雄/装备/羁绊中文名: `static.datatft.com/tft-base/languages/cn.*.json` (AES-128-ECB)",
           ]
    for note in analysis.get("notes") or []:
        out.append(f"- 提示: {note}")
    out.append("")
    out.append("> 已知局限: 站位为网站记录的快照, 游戏内视角可能左右镜像; "
               "密文里未编码星级时, 星级取自站点榜单数据。")
    out.append("")
    return "\n".join(out)
