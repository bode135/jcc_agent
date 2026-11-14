"""datatft.com 数据抓取编排: 版本解析 -> API 抓取 -> 构建 JSON -> 下载图片."""
import json
import time
from pathlib import Path

import requests

from . import config
from .decrypt import (
    fetch_bootstrap_js,
    fetch_static_dataset,
    get_latest_version,
    make_session,
)
from .recipes import write_recipes


def _post_json(s: requests.Session, url: str, payload: dict) -> dict:
    r = s.post(url, json=payload, timeout=config.REQUEST_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    data = body.get("data")
    if data is None:
        raise RuntimeError(f"{url} 未返回数据: {str(body)[:200]}")
    return data


def resolve_version(session: requests.Session, version: str | None) -> str:
    return version or get_latest_version(session)


def fetch_explore(session: requests.Session, version: str) -> dict:
    """实时统计, 返回 {units: {key: stats}, equips: {...}, traits: {...}} 索引 dict."""
    data = _post_json(session, f"{config.API_BASE}/data/explore", {"version": version})
    return {
        kind: {item["key"]: item for item in data.get(kind, [])}
        for kind in ("units", "equips", "traits")
    }


def build_units(dataset: dict, live: dict) -> dict:
    """units.json: heros18 静态数据 + explore 实时统计 + 拉克丝变体 extraUnits."""
    hero_by_key = {}
    for h in dataset[config.DATASET_HERO_KEY]:
        hero_by_key[h["key"]] = {
            "chessId": h["chessId"],
            "key": h["key"],
            "name": h["displayName"],
            "title": h.get("title"),
            "otherNames": h.get("otherNames"),
            "price": h.get("price"),
            "races": h.get("races"),
            "jobs": h.get("jobs"),
            "role": h.get("role"),
            "stats": {
                "armor": h.get("armor"),
                "magic": h.get("magic"),
                "spellBlock": h.get("spellBlock"),
                "attackRange": h.get("attackRange"),
                "attackSpeed": h.get("attackSpeed"),
                "crit": h.get("crit"),
                "startMagic": h.get("startMagic"),
                "lifeData": h.get("lifeData"),
                "attackData": h.get("attackData"),
            },
            "skillName": h.get("skillName"),
            "skillDetail": h.get("skillDetail2"),
            "recEquip": h.get("recEquip"),
            "image": f"{config.DIR_IMAGE_UNITS}/{h['chessId']}{config.IMAGE_EXT_UNIT}",
            "liveStats": live["units"].get(h["key"]),
        }

    extra_units = []
    for stats in live["units"].values():
        key = stats["key"]
        if key in hero_by_key or key not in config.LUX_VARIANTS:
            continue
        extra_units.append({
            "chessId": config.LUX_CHESS_ID,
            "key": key,
            "name": config.LUX_VARIANTS[key],
            "image": f"{config.DIR_IMAGE_UNITS}/{config.LUX_CHESS_ID}{config.IMAGE_EXT_UNIT}",
            "liveStats": stats,
            "isMonster": False,
        })

    units = list(hero_by_key.values())
    return {
        "version": None,  # 由 scrape() 填充
        "source": "datatft.com",
        "count": len(units),
        "units": units,
        "extraUnits": extra_units,
    }


def build_items(dataset: dict, live: dict) -> dict:
    """items.json: equips18 静态数据 + explore 实时统计."""
    items = []
    for e in dataset[config.DATASET_EQUIP_KEY]:
        items.append({
            "equipId": e["equipId"],
            "key": e["key"],
            "name": e["name"],
            "nickname": e.get("nickname"),
            "otherNames": e.get("otherNames"),
            "type2": e.get("type2"),
            "effect": e.get("effect"),
            "effectAttr": e.get("effectAttr"),
            "formula": e.get("formula"),
            "heroDesc": e.get("heroDesc"),
            "image": f"{config.DIR_IMAGE_ITEMS}/{e['equipId']}{config.IMAGE_EXT_ITEM}",
            "liveStats": live["equips"].get(e["key"]),
        })
    return {"version": None, "source": "datatft.com", "count": len(items), "items": items}


def build_charms(dataset: dict) -> dict:
    """charms.json: charms18 仙灵法杖 (database#charm 页签), name=title, upgrade 为升级版效果."""
    charms = []
    for c in dataset[config.DATASET_CHARM_KEY]:
        charms.append({
            "key": c["key"],
            "name": c["title"],
            "cost": c.get("cost"),
            "tier": c.get("tier"),
            "category": c.get("category"),
            "effect": c.get("effect"),
            "upgrade": (c.get("upgrade") or {}).get("effect"),
            "rounds": c.get("rounds"),
        })
    return {"version": None, "source": "datatft.com", "count": len(charms), "charms": charms}


def build_traits(dataset: dict, live: dict) -> dict:
    """traits.json: races18 + jobs18 (36 个羁绊) + explore 实时统计."""
    traits = []
    for kind, dataset_key, id_field in (
        ("race", config.DATASET_RACE_KEY, "raceId"),
        ("job", config.DATASET_JOB_KEY, "jobId"),
    ):
        for t in dataset[dataset_key]:
            traits.append({
                "type": kind,
                "id": t[id_field],
                "key": t["key"],
                "name": t["name"],
                "otherNames": t.get("otherNames"),
                "introduce": t.get("introduce"),
                "levels": t.get("level"),
                "heros": t.get("heros"),
                "liveStats": live["traits"].get(t["key"]),
            })
    return {"version": None, "count": len(traits), "traits": traits}


def fetch_comps(session: requests.Session, version: str) -> tuple[str, list]:
    """team/list 热门阵容, tier 按 TIER_PRIORITY 依次尝试直到非空."""
    last_data = {}
    for tier in config.TIER_PRIORITY:
        data = _post_json(session, f"{config.API_BASE}/team/list", {
            "version": version,
            "season": config.SEASON,
            "tier": tier,
            "time": config.TIME_RANGE_DAYS,
        })
        lst = data.get("list") or []
        if lst:
            return tier, lst
        last_data = data
    return "none", []


def build_comps(version: str, tier: str, raw_list: list, hero_name: dict, equip_info: dict) -> dict:
    """comps.json: 解析 tftEquips(list 或空格分隔字符串) 并补充棋子/装备名称."""
    comps = []
    for raw in raw_list:
        heros = []
        for h in raw.get("heros") or []:
            equips = []
            te = h.get("tftEquips")
            equip_keys = te if isinstance(te, list) else (str(te).split() if te else [])
            for ek in equip_keys:
                info = equip_info.get(ek)
                equips.append({"id": info["equipId"], "name": info["name"]} if info else {"key": ek})
            heros.append({
                "chessId": h.get("id"),
                "key": h.get("key"),
                "name": hero_name.get(h.get("key"), h.get("key")),
                "stars": h.get("stars"),
                "equips": equips,
            })
        comps.append({
            "id": raw.get("id"),
            "title": raw.get("title"),
            "titleApi": raw.get("titleApi"),
            "tier": raw.get("tier"),
            "difficulty": raw.get("difficulty"),
            "tags": raw.get("tags"),
            "score": raw.get("score"),
            "playRate": raw.get("playRate"),
            "top4": raw.get("top4"),
            "won": raw.get("won"),
            "place": raw.get("place"),
            "heros": heros,
            "tftCode": raw.get("tftCode"),
        })
    return {
        "version": version,
        "season": config.SEASON,
        "tier": tier,
        "timeDays": config.TIME_RANGE_DAYS,
        "source": "datatft.com",
        "count": len(comps),
        "comps": comps,
    }


def _download(s: requests.Session, url: str, dst: Path) -> str:
    if dst.exists() and dst.stat().st_size > 0:
        return "skipped"
    r = s.get(url, timeout=config.REQUEST_TIMEOUT)
    if r.status_code != 200:
        return "failed"
    dst.write_bytes(r.content)
    return "ok"


def download_images(session: requests.Session, root: Path, units: dict, items: dict) -> dict:
    """下载棋子/装备图片到 root 下, 已存在则跳过."""
    unit_dir = root / config.DIR_IMAGE_UNITS
    item_dir = root / config.DIR_IMAGE_ITEMS
    unit_dir.mkdir(parents=True, exist_ok=True)
    item_dir.mkdir(parents=True, exist_ok=True)

    counts = {"ok": 0, "skipped": 0, "failed": 0}

    def _bump(status: str):
        counts[status] += 1

    chess_ids = {u["chessId"] for u in units["units"]}
    chess_ids |= {u["chessId"] for u in units["extraUnits"]}
    for chess_id in sorted(chess_ids):
        url = f"{config.CDN_BASE}/heros/default/{chess_id}{config.IMAGE_EXT_UNIT}"
        _bump(_download(session, url, unit_dir / f"{chess_id}{config.IMAGE_EXT_UNIT}"))
    for it in items["items"]:
        url = f"{config.CDN_BASE}/equips/{it['equipId']}{config.IMAGE_EXT_ITEM}"
        _bump(_download(session, url, item_dir / f"{it['equipId']}{config.IMAGE_EXT_ITEM}"))
    return counts


def scrape(version: str | None = None, output_dir: str | None = None,
           with_images: bool = True, session: requests.Session | None = None) -> dict:
    """抓取指定版本(默认最新)的棋子/装备/羁绊/热门阵容数据.

    output_dir 为空时默认 当前工作路径/workspace/scrape_info,
    并按 APPEND_VERSION_SUBDIR 追加 v{version} 子目录.
    """
    s = session or make_session()
    ver = resolve_version(s, version)

    root = Path(output_dir) if output_dir else Path.cwd() / config.DEFAULT_OUTPUT_DIR
    if config.APPEND_VERSION_SUBDIR:
        root = root / f"{config.VERSION_PREFIX}{ver}"
    chess_dir = root / config.DIR_CHESS_INFO
    comps_dir = root / config.DIR_COMPS
    chess_dir.mkdir(parents=True, exist_ok=True)
    comps_dir.mkdir(parents=True, exist_ok=True)

    dataset = fetch_static_dataset(s)
    live = fetch_explore(s, ver)

    units = build_units(dataset, live)
    items = build_items(dataset, live)
    traits = build_traits(dataset, live)
    charms = build_charms(dataset)
    for doc in (units, items, charms):
        doc["version"] = ver
        doc["fetchedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    traits["version"] = ver
    traits["fetchedAt"] = units["fetchedAt"]

    tier, raw_list = fetch_comps(s, ver)
    hero_name = {h["key"]: h["displayName"] for h in dataset[config.DATASET_HERO_KEY]}
    equip_info = {e["key"]: e for e in dataset[config.DATASET_EQUIP_KEY]}
    comps = build_comps(ver, tier, raw_list, hero_name, equip_info)
    comps["fetchedAt"] = units["fetchedAt"]

    files = {
        "units": chess_dir / "units.json",
        "items": chess_dir / "items.json",
        "traits": chess_dir / "traits.json",
        "charms": chess_dir / "charms.json",
        "comps": comps_dir / "comps.json",
    }
    for doc, path in ((units, files["units"]), (items, files["items"]),
                      (traits, files["traits"]), (charms, files["charms"]),
                      (comps, files["comps"])):
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    images = download_images(s, root, units, items) if with_images else None

    # 装备合成表: 只依赖刚写出的 items.json, 顺手生成 JSON + 人读文档
    recipes = write_recipes(files["items"])
    files["itemRecipes"] = Path(recipes["json"])
    files["recipeDoc"] = Path(recipes["markdown"])

    return {
        "version": ver,
        "output_dir": str(root),
        "files": {k: str(v) for k, v in files.items()},
        "counts": {
            "units": units["count"],
            "extraUnits": len(units["extraUnits"]),
            "items": items["count"],
            "traits": traits["count"],
            "charms": charms["count"],
            "comps": comps["count"],
            "recipes": recipes["counts"]["recipes"],
        },
        "recipeWarnings": recipes["warnings"],
        "images": images,
    }
