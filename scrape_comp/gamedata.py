"""datatft.com 静态游戏数据: URL 发现 -> 下载 -> AES 解密 -> 查询封装.

站点把英雄/装备/羁绊/海克斯数据打进 `tft-base/languages/cn.<hash>.json` (base64 + AES-128-ECB),
文件名带构建哈希, 发版即变, 所以先按 intent-wheel 页面上的 staticAssets chunk 动态发现 URL,
失败再退回 config.LANG_DATA_URL_FALLBACK.

**必须用这份数据**: teamUrl 的密文码表(英雄码/装备码/海克斯码)是从这份数据里按顺序生成的,
换一份来源顺序不同就会解出错误结果.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import requests

from scrape_info.decrypt import decrypt_payload  # 与 scrape_info 同一套 AES-128-ECB 数据集

from . import config

_LANG_URL_RE = re.compile(r"https://static\.datatft\.com/tft-base/languages/cn\.[0-9a-f]+\.json")
_STATIC_ASSETS_RE = re.compile(r'href="\./assets/(staticAssets-[A-Za-z0-9_-]+\.js)"')


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Referer": config.SITE_BASE + "/"})
    return s


def resolve_language_url(session: requests.Session | None = None) -> str:
    """动态发现中文静态数据 URL; 失败时返回 config 里的兜底值."""
    s = session or make_session()
    try:
        html = s.get(config.TOOL_PAGE, timeout=config.REQUEST_TIMEOUT).text
        m = _STATIC_ASSETS_RE.search(html)
        if m:
            js = s.get(f"{config.SITE_BASE}/service/tool/assets/{m.group(1)}",
                       timeout=config.REQUEST_TIMEOUT).text
            found = _LANG_URL_RE.search(js)
            if found:
                return found.group(0)
    except requests.RequestException:
        pass
    return config.LANG_DATA_URL_FALLBACK


def fetch_dataset(session: requests.Session | None = None, url: str | None = None) -> dict:
    """下载并解密静态数据集, 返回 {heros18, equips18, hexs18, races18, jobs18, ...}."""
    s = session or make_session()
    resp = s.get(url or resolve_language_url(s), timeout=config.LANG_REQUEST_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    payload = body.get("data")
    if not isinstance(payload, str) or not payload:
        raise RuntimeError("静态数据响应缺少 data 字段")
    return json.loads(decrypt_payload(payload).decode("utf-8"))


class GameData:
    """静态数据查询封装: 英雄/装备/羁绊/海克斯, 以及码表需要的"带兜底假棋子"的英雄序列."""

    def __init__(self, dataset: dict, season: str = config.SEASON):
        self.dataset = dataset
        self.season = str(season)
        self.heroes = _hero_sequence(dataset, self.season)
        self.equips = list(dataset.get(f"equips{self.season}") or [])
        self.hex_groups = _hex_groups(dataset, self.season)
        self.traits = _traits(dataset, self.season)
        self.anomalies = list(dataset.get(f"anomalies{self.season}") or [])
        self._hero_by_id = {str(h.get("chessId")): h for h in self.heroes}
        self._equip_by_id = {e.get("equipId"): e for e in self.equips}
        self._trait_by_name = {t["name"]: t for t in self.traits}
        self._trait_by_key = {t["key"]: t for t in self.traits}

    # ---------- 加载 ----------
    @classmethod
    def load(cls, season: str = config.SEASON, cache_dir: str | Path | None = None,
             refresh: bool = False, session: requests.Session | None = None) -> "GameData":
        """带磁盘缓存的加载 (数据集约 2MB, 默认缓存到 <缓存目录>/game_data_cn.json)."""
        cache_file = None
        if cache_dir:
            cache_file = Path(cache_dir) / config.CACHE_FILE_NAME
            if cache_file.exists() and not refresh:
                try:
                    return cls(json.loads(cache_file.read_text(encoding="utf-8")), season)
                except (json.JSONDecodeError, OSError):
                    pass  # 缓存坏了就当没有, 重新下载
        dataset = fetch_dataset(session)
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
        return cls(dataset, season)

    # ---------- 查询 ----------
    def hero(self, chess_id) -> dict | None:
        return self._hero_by_id.get(str(chess_id))

    def equip(self, equip_id) -> dict | None:
        return self._equip_by_id.get(int(equip_id)) if str(equip_id).lstrip("-").isdigit() else None

    def equip_by_key(self, key: str) -> dict | None:
        for e in self.equips:
            if e.get("key") == key:
                return e
        return None

    def trait(self, name: str = None, key: str = None) -> dict | None:
        if key:
            return self._trait_by_key.get(key)
        return self._trait_by_name.get(name)

    def hex(self, hex_id: str) -> tuple[int, dict] | None:
        """按 hexId 找海克斯, 返回 (第几组 1~3, 词条)."""
        for gi, group in enumerate(self.hex_groups, start=1):
            for h in group:
                if h.get("hexId") == hex_id or h.get("img") == hex_id:
                    return gi, h
        return None

    # ---------- 便捷信息 ----------
    def hero_name(self, chess_id) -> str:
        h = self.hero(chess_id)
        return h.get("displayName") if h else str(chess_id)

    def equip_name(self, equip_id) -> str:
        e = self.equip(equip_id)
        return e.get("name") if e else str(equip_id)

    def items_with_keyword(self, keywords) -> list[dict]:
        """按 effect 里的关键词找装备 (用于破甲/破魔抗/重伤这类功能装)."""
        out = []
        for e in self.equips:
            text = (e.get("effect") or "") + (e.get("effectAttr") or "")
            if any(k in text for k in keywords):
                out.append(e)
        return out


# ---------- 内部: 复刻 staticAssets 里的列表构造 ----------
def _hero_sequence(dataset: dict, season: str) -> list[dict]:
    """h0(): 主线英雄按 displayName 去重后, 追加该赛季的野怪/召唤物假棋子.

    码表按这个顺序给"没有 code 的英雄"发兜底码 (z + 字母), 所以顺序必须一致.
    """
    raw = list(dataset.get(f"heros{season}") or [])
    seen, heroes = set(), []
    for h in raw:
        name = h.get("displayName")
        if name in seen:
            continue
        seen.add(name)
        heroes.append(dict(h))
    for chess_id in config.SPECIAL_HEROES.get(str(season), []):
        name = config.SPECIAL_HERO_NAMES.get(chess_id)
        heroes.append({
            "chessId": chess_id,
            "key": str(chess_id),
            # 站点自己的名称表里也缺这几个 id -> 不编名字, 直接标未命名
            "displayName": name or f"未命名({chess_id})",
            "price": 99,
            "isSpecial": True,
            "unnamed": not name,
        })
    return heroes


def _hex_groups(dataset: dict, season: str) -> list[list[dict]]:
    """v0(): hexs18 是 3 组(银/金/棱彩), 元素可能是 [hexId,name,desc,round] 数组."""
    groups = []
    for group in dataset.get(f"hexs{season}") or []:
        items = []
        for p in group:
            if isinstance(p, list):
                p = {"hexId": p[0], "img": p[0], "name": p[1], "desc": p[2], "round": p[3]}
            elif not p.get("hexId") and p.get("img"):
                p = {**p, "hexId": p["img"]}
            desc = p.get("desc") or ""
            if "当前版本已禁用" in desc or "Current version disabled" in desc:
                continue   # 与站点同一套过滤
            items.append(p)
        groups.append(items)
    return groups


def _traits(dataset: dict, season: str) -> list[dict]:
    """races + jobs 合并, 标出 kind (race/job), 便于区分前排/后排羁绊时按来源归类."""
    traits = []
    for kind, key, id_field in (("race", f"races{season}", "raceId"), ("job", f"jobs{season}", "jobId")):
        for t in dataset.get(key) or []:
            traits.append({
                "kind": kind,
                "id": t.get(id_field),
                "key": t.get("key"),
                "name": t.get("name"),
                "introduce": t.get("introduce"),
                "levels": t.get("level") or [],
                "heros": t.get("heros") or [],
            })
    return traits


def trait_heroes(game: GameData, trait_name: str) -> set:
    """某个羁绊的棋子集合(静态数据里的 heros 是 chessId 列表)."""
    t = game.trait(name=trait_name)
    return {str(x) for x in (t or {}).get("heros") or []}
