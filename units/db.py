"""棋子/装备/羁绊数据库: 加载 scrape_info 爬取数据, 供名称反查详细信息.

数据来源: workspace/scrape_info/<版本>/ 下的 JSON (由 scrape_info 爬虫生成),
自动选用版本号最大的目录. 名称查找支持 精确名 → 别名 → 模糊匹配 三级.
"""

import difflib
import json
from functools import lru_cache
from pathlib import Path

from config.config import default_config

FUZZY_THRESHOLD = 0.6  # 模糊匹配最低相似度 (OCR 错别字兜底)


def _version_dir() -> Path:
    """返回爬取数据中版本号最大的目录, 如 .../scrape_info/v18.3b."""
    base = default_config.SCRAPE_INFO_DIR
    versions = sorted(p for p in base.glob("v*") if p.is_dir())
    if not versions:
        raise FileNotFoundError(f"未找到爬取数据目录: {base}/v*")
    return versions[-1]


@lru_cache(maxsize=1)
def load_db() -> dict:
    """加载全部爬取数据 (进程内缓存一次)."""
    root = _version_dir()

    def _load(rel: str) -> list:
        return json.loads((root / rel).read_text(encoding="utf-8"))

    units = _load("chess_info/units.json")["units"]
    by_name = {u["name"]: u for u in units}
    aliases = {}  # 别名 → 正式名
    for u in units:
        for alias in u.get("otherNames") or []:
            aliases[alias] = u["name"]

    items = _load("chess_info/items.json")["items"]
    traits = _load("chess_info/traits.json")["traits"]
    charm_path = root / "chess_info" / "charms.json"
    charms = json.loads(charm_path.read_text(encoding="utf-8"))["charms"] if charm_path.exists() else []

    return {
        "version": root.name,
        "root": root,
        "units": units,
        "unit_by_name": by_name,
        "unit_alias": aliases,
        "items": items,
        "item_by_name": {i["name"]: i for i in items},
        "traits": traits,
        "trait_by_name": {t["name"]: t for t in traits},
        "charms": charms,
        "charm_by_name": {c["name"]: c for c in charms},
    }


def find_unit(name: str) -> dict | None:
    """按名称查棋子: 精确名 → 别名 → 模糊匹配, 查不到返回 None."""
    if not name:
        return None
    data = load_db()
    if name in data["unit_by_name"]:
        return data["unit_by_name"][name]
    if name in data["unit_alias"]:
        return data["unit_by_name"][data["unit_alias"][name]]
    pool = list(data["unit_by_name"]) + [
        a for a in data["unit_alias"] if len(a) >= 2
    ]
    best = difflib.get_close_matches(name, pool, n=1, cutoff=FUZZY_THRESHOLD)
    if best:
        return data["unit_by_name"].get(
            data["unit_alias"].get(best[0], best[0])
        )
    return None


def unit_attack_range(unit: dict | None) -> int | None:
    """棋子攻击距离 (格数); 读不到返回 None. 这是攻击距离的唯一访问点.

    units.json 把攻击距离嵌在 `stats.attackRange` (顶层**没有** attack_range 键).
    历史调用方曾直接读顶层 attack_range → 恒为 None → 1-1/1-2 固定开局
    "找后排奕子上场"永不生效 (而单测把 find_unit mock 成扁平结构, 掩盖了这一点).
    统一走本函数, 避免再次读错层级或各处重复拼路径.
    """
    stats = (unit or {}).get("stats") or {}
    rng = stats.get("attackRange")
    return int(rng) if isinstance(rng, (int, float)) else None


def find_item(name: str) -> dict | None:
    """按名称查装备, 查找逻辑同 find_unit."""
    if not name:
        return None
    data = load_db()
    if name in data["item_by_name"]:
        return data["item_by_name"][name]
    pool = [i["name"] for i in data["items"]]
    best = difflib.get_close_matches(name, pool, n=1, cutoff=FUZZY_THRESHOLD)
    return data["item_by_name"].get(best[0]) if best else None


def find_trait(name: str) -> dict | None:
    """按名称查羁绊, 查找逻辑同 find_unit."""
    if not name:
        return None
    data = load_db()
    if name in data["trait_by_name"]:
        return data["trait_by_name"][name]
    pool = list(data["trait_by_name"])
    best = difflib.get_close_matches(name, pool, n=1, cutoff=FUZZY_THRESHOLD)
    return data["trait_by_name"].get(best[0]) if best else None


def find_charm(name: str) -> dict | None:
    """按名称查仙灵法杖 (商店法杖卡), 查找逻辑同 find_item."""
    if not name:
        return None
    data = load_db()
    if name in data["charm_by_name"]:
        return data["charm_by_name"][name]
    pool = list(data["charm_by_name"])
    best = difflib.get_close_matches(name, pool, n=1, cutoff=FUZZY_THRESHOLD)
    return data["charm_by_name"].get(best[0]) if best else None


def unit_image(unit: dict) -> Path:
    """棋子头像图片路径 (爬取的 96×96 jpg)."""
    return load_db()["root"] / unit["image"]
