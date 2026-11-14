"""装备合成表: 用 items.json 里的 `formula` 字段生成 "A + B -> ?" 的查询文档.

数据基础 (实测 v18.3b): `formula` 形如 "501,502", 两段都是**基础装备(组件)的 equipId**;
10 个组件两两组合 = 55 条唯一配方(含自己+自己), 正好铺满 10×10 的对称矩阵。
没有 `formula` 的装备(光明装备/奥恩神器/特殊纹章/药水等)不能由两件基础装备合成, 文档里单列。

产出:
    item_recipes.json   结构化配方(矩阵 + 双向索引, 供程序查询)
    装备合成表.md        人读文档(矩阵表 + 成品反查 + 组件正查)

单独重生成(不重新爬站):
    python -m scrape_info --recipes-only
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

# type2 -> 中文分类 (文档里分节用; 站点自己的页签分法是 普通装备/转职纹章/奥恩神器/光明装备/其他装备)
TYPE_LABEL = {
    "part": "基础装备(组件)",
    "common": "普通装备",
    "zhuanzhi": "转职纹章·金铲铲系",
    "zhuanzhiJgg": "转职纹章·金锅锅系",
    "zhuanzhi2": "特殊纹章(不可合成)",
    "guangming": "光明装备",
    "aoen": "奥恩神器",
    "other": "药水/消耗品",
    "remove": "宝箱/特殊道具",
    "daoju": "峡谷道具",
}
# 文档里"没有配方"的分类展示顺序
NO_FORMULA_ORDER = ("guangming", "aoen", "zhuanzhi2", "other", "remove", "daoju")


def _ref(item: dict, image_prefix: str | None = None) -> dict:
    """装备摘要(去掉 liveStats 这类大字段, 只留文档/查询要用的)."""
    ref = {
        "equipId": item.get("equipId"),
        "key": item.get("key"),
        "name": item.get("name"),
        "nickname": item.get("nickname"),
        "otherNames": item.get("otherNames") or [],
        "type2": item.get("type2"),
        "typeLabel": TYPE_LABEL.get(item.get("type2"), item.get("type2")),
        "effect": item.get("effect"),
        "effectAttr": item.get("effectAttr"),
        "formula": item.get("formula"),
        "image": item.get("image"),
    }
    if image_prefix:
        ref["icon"] = f"{image_prefix.rstrip('/')}/{item.get('equipId')}.png"
    return ref


def build_recipes(items: list[dict], version: str | None = None,
                  image_prefix: str | None = None) -> dict:
    """把 items.json 的 items 列表转成合成查询结构."""
    by_id = {i["equipId"]: i for i in items}
    components = [i for i in items if i.get("type2") == "part"]
    component_ids = {i["equipId"] for i in components}

    recipes, lookup, by_result, bad = [], {}, {}, []
    for item in items:
        formula = item.get("formula")
        if not formula:
            continue
        part_ids = [int(x) for x in str(formula).split(",") if str(x).strip()]
        unknown = [p for p in part_ids if p not in component_ids]
        if unknown:
            # 站点数据结构变了才会走到这里: 显式报出来, 不要静默生成半截配方
            bad.append({"result": item.get("name"), "equipId": item.get("equipId"), "unknown": unknown})
            continue
        parts = [_ref(by_id[p], image_prefix) for p in part_ids]
        recipe = {
            "result": _ref(item, image_prefix),
            "parts": parts,
            "partIds": part_ids,
            "formula": str(formula),
        }
        recipes.append(recipe)
        lookup[",".join(str(p) for p in sorted(part_ids))] = item["equipId"]
        by_result[item["equipId"]] = part_ids

    # 组件 -> 能合成的成品(去重后按名排序; 自己+自己只算一个成品)
    can_make: dict[int, list[str]] = {cid: [] for cid in component_ids}
    for recipe in recipes:
        for p in recipe["parts"]:
            name = recipe["result"]["name"]
            if name not in can_make[p["equipId"]]:
                can_make[p["equipId"]].append(name)
    for cid in can_make:
        can_make[cid].sort()

    comp_refs = []
    for c in sorted(components, key=lambda x: x["equipId"]):
        ref = _ref(c, image_prefix)
        ref["canMake"] = can_make[c["equipId"]]
        ref["canMakeCount"] = len(ref["canMake"])
        comp_refs.append(ref)
    no_formula = [_ref(i, image_prefix) for i in items
                  if not i.get("formula") and i.get("type2") != "part"]

    recipes.sort(key=lambda r: (r["result"]["type2"] or "", r["result"]["name"] or ""))

    return {
        "version": version,
        "source": "datatft.com",
        "counts": {
            "items": len(items),
            "components": len(components),
            "recipes": len(recipes),
            "noFormula": len(no_formula),
        },
        "components": comp_refs,
        "matrixOrder": [c["equipId"] for c in comp_refs],
        "recipes": recipes,
        "lookup": lookup,          # "501,502" -> 成品 equipId
        "byResult": by_result,     # 成品 equipId -> [组件 equipId, 组件 equipId]
        "noFormula": no_formula,
        "dataWarnings": bad,       # 公式里出现非组件 id 时才有内容
    }


# ---------------------------------------------------------------- 查询
def _name_index(recipes: dict) -> dict:
    """组件名/别名 -> equipId."""
    index = {}
    for c in recipes.get("components") or []:
        for name in [c.get("name"), c.get("nickname")] + list(c.get("otherNames") or []):
            if name:
                index.setdefault(name, c["equipId"])
    return index


def find_recipe(recipes: dict, a, b) -> dict | None:
    """查「A + B 能合成什么」, 参数可以是 equipId、组件名或别名(如 "大剑"/"反手"/"拳套").

    返回成品装备摘要, 查不到(含输入不是基础装备)返回 None。
    """
    index = _name_index(recipes)
    ids = []
    for x in (a, b):
        if isinstance(x, int) or (isinstance(x, str) and x.isdigit()):
            ids.append(int(x))
            continue
        cid = index.get(str(x).strip())
        if cid is None:
            return None
        ids.append(cid)
    result_id = (recipes.get("lookup") or {}).get(",".join(str(i) for i in sorted(ids)))
    if result_id is None:
        return None
    for recipe in recipes.get("recipes") or []:
        if recipe["result"]["equipId"] == result_id:
            return recipe["result"]
    return None


# ---------------------------------------------------------------- Markdown
def render_markdown(recipes: dict, image_prefix: str = "../images/items") -> str:
    comps = recipes["components"]
    order = recipes["matrixOrder"]
    counts = recipes["counts"]
    out = [
        f"# 装备合成表 (版本 {recipes.get('version') or '-'})",
        "",
        f"> 数据来源: `items.json` 的 `formula` 字段 (datatft.com); "
        f"共 {counts['items']} 件装备, 其中基础装备 {counts['components']} 种, "
        f"可合成配方 {counts['recipes']} 条。",
        "> 行列都是基础装备, 交叉格就是这两件合成的成品; 想查「A + B = ?」在矩阵里找 A 行与 B 列的交叉即可。",
        "",
        "## 一、基础装备(组件)",
        "",
        "| 图标 | 组件 | 别名 | 可合成数 |",
        "|:---|:---|:---|:---|",
    ]
    for c in comps:
        out.append(f"| ![{c['name']}]({image_prefix}/{c['equipId']}.png) | **{c['name']}** | "
                   f"{_alias_text(c)} | {c['canMakeCount']} |")
    out.append("")

    out.append("## 二、合成矩阵")
    out.append("")
    out.append("| 合成 | " + " | ".join(f"**{c['name']}**" for c in comps) + " |")
    out.append("|:---|" + ":---|" * len(comps))
    cell_index = {}
    for recipe in recipes["recipes"]:
        a, b = recipe["partIds"]
        cell_index[(a, b)] = recipe["result"]["name"]
        cell_index[(b, a)] = recipe["result"]["name"]
    for row in comps:
        cells = []
        for col in comps:
            name = cell_index.get((row["equipId"], col["equipId"]), "—")
            cells.append(f"**{name}**" if row["equipId"] == col["equipId"] else name)
        out.append(f"| **{row['name']}** | " + " | ".join(cells) + " |")
    out.append("")

    out.append("## 三、成品反查(成品 → 配方)")
    out.append("")
    grouped: dict[str, list] = {}
    for recipe in recipes["recipes"]:
        grouped.setdefault(recipe["result"]["type2"], []).append(recipe)
    for type2, group in grouped.items():
        out.append(f"### {TYPE_LABEL.get(type2, type2)} ({len(group)})")
        out.append("")
        out.append("| 图标 | 成品 | 配方 | 别名 | 效果 |")
        out.append("|:---|:---|:---|:---|:---|")
        for recipe in sorted(group, key=lambda r: r["result"]["name"] or ""):
            r = recipe["result"]
            parts = " + ".join(p["name"] for p in recipe["parts"])
            out.append(f"| ![{r['name']}]({image_prefix}/{r['equipId']}.png) | **{r['name']}** | {parts} | "
                       f"{_alias_text(r)} | {_short(r.get('effect'))} |")
        out.append("")

    out.append("## 四、组件正查(一件组件能合出什么)")
    out.append("")
    for c in comps:
        out.append(f"**{c['name']}** ({c['canMakeCount']}): " + "、".join(c["canMake"]))
        out.append("")

    out.append("## 五、没有配方的装备(不能由两件基础装备合成)")
    out.append("")
    no_formula = recipes["noFormula"]
    by_type: dict[str, list] = {}
    for item in no_formula:
        by_type.setdefault(item.get("type2"), []).append(item)
    for type2 in list(NO_FORMULA_ORDER) + [t for t in by_type if t not in NO_FORMULA_ORDER]:
        group = by_type.get(type2)
        if not group:
            continue
        out.append(f"- **{TYPE_LABEL.get(type2, type2)}** ({len(group)}): {_name_list(group)}")
    out.append("")
    out.append("> 光明装备来自光明武器库, 奥恩神器来自奥恩/神器武器库, 特殊纹章与药水来自强化符文或事件, "
               "都不是「两件组件」合出来的, 所以没有配方。")
    out.append("")
    out.append("## 六、程序查询")
    out.append("")
    out.append("`item_recipes.json` 里 `lookup` 的键是「两个组件 equipId 排序后用逗号连接」, 值就是成品 equipId;"
               " `byResult` 是反向索引。用 `scrape_info.recipes.find_recipe` 可以直接按名字/别名查:")
    out.append("")
    out.append("```python")
    out.append("import json")
    out.append("from scrape_info.recipes import find_recipe")
    out.append("")
    out.append("recipes = json.load(open('item_recipes.json', encoding='utf-8'))")
    out.append("print(find_recipe(recipes, '大剑', '拳套')['name'])   # 无尽之刃")
    out.append("print(find_recipe(recipes, '反曲弓', '反曲弓')['name'])  # 红霸符")
    out.append("```")
    out.append("")

    if recipes.get("dataWarnings"):
        out.append("## 六、数据告警")
        out.append("")
        for w in recipes["dataWarnings"]:
            out.append(f"- {w['result']}(id={w['equipId']}) 的公式里出现非组件 id: {w['unknown']}")
        out.append("")
    return "\n".join(out)


def _alias_text(ref: dict) -> str:
    """别名: 取 nickname + otherNames, 去重后顿号连接."""
    names = [ref.get("nickname")] + list(ref.get("otherNames") or [])
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return "、".join(out) or "—"


def _name_list(items: list[dict]) -> str:
    """同名条目(如两个不同 id 的「绝命花妖纹章」)补上 id, 避免看起来像重复。"""
    from collections import Counter
    counts = Counter(i["name"] for i in items)
    parts = []
    for i in sorted(items, key=lambda x: (x["name"] or "", x["equipId"])):
        parts.append(f"{i['name']}({i['equipId']})" if counts[i["name"]] > 1 else i["name"])
    return "、".join(parts)


def _short(text, limit: int = 60) -> str:
    plain = re.sub(r"<[^>]+>", " ", str(text or ""))
    plain = " ".join(plain.split())
    if not plain:
        return "—"
    return plain[:limit] + ("…" if len(plain) > limit else "")


# ---------------------------------------------------------------- 读写
def latest_items_json(output_dir: str | Path | None = None) -> Path:
    """找版本号最大的 scrape_info 目录下的 items.json (与 units/db.py 的选版规则一致)."""
    base = Path(output_dir) if output_dir else Path.cwd() / config.DEFAULT_OUTPUT_DIR
    versions = sorted(p for p in base.glob("v*") if p.is_dir())
    if not versions:
        raise FileNotFoundError(f"未找到爬取数据目录: {base}/v*")
    items_path = versions[-1] / config.DIR_CHESS_INFO / "items.json"
    if not items_path.exists():
        raise FileNotFoundError(f"未找到 {items_path}")
    return items_path


def write_recipes(items_path: str | Path, output_dir: str | Path | None = None,
                  image_prefix: str = "../images/items") -> dict:
    """读 items.json -> 写 item_recipes.json + 装备合成表.md, 返回 {文件路径, 统计}."""
    items_path = Path(items_path)
    doc = json.loads(items_path.read_text(encoding="utf-8"))
    target = Path(output_dir) if output_dir else items_path.parent
    version = doc.get("version") or items_path.parent.parent.name
    recipes = build_recipes(doc.get("items") or [], version=version, image_prefix=image_prefix)
    recipes["generatedFrom"] = str(items_path)

    target.mkdir(parents=True, exist_ok=True)
    json_path = target / config.FILE_ITEM_RECIPES
    doc_path = target / config.FILE_RECIPE_DOC
    json_path.write_text(json.dumps(recipes, ensure_ascii=False, indent=1), encoding="utf-8")
    doc_path.write_text(render_markdown(recipes, image_prefix=image_prefix), encoding="utf-8")
    return {
        "version": version,
        "json": str(json_path),
        "markdown": str(doc_path),
        "counts": recipes["counts"],
        "warnings": recipes["dataWarnings"],
    }
