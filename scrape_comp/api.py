"""datatft.com 阵容相关 API 客户端 (api.datatft.com, POST + JSON).

实测端点 (2026-10, 见 config.py 头部注释):
    /team/strategy        {id}                    -> 攻略正文(核心/开局/前中后期)、推荐出装、teamUrl、阵容码
    /team/strategy/extra  {id}                    -> 实战装备统计(每个弈子每件装备的出场/前四/名次)
    /team/rank            {season}                -> 榜单(评级/主C heroId/三星与成装)
    /team/list            {version,season,tier,time} -> 分档榜单(需要版本号)
"""
from __future__ import annotations

import requests

from . import config


class DatatftError(RuntimeError):
    pass


class DatatftClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Referer": config.SITE_BASE + "/",
            "Origin": config.SITE_BASE,
            "Content-Type": "application/json",
        })

    def _post(self, path: str, payload: dict) -> dict:
        resp = self.session.post(config.API_BASE + path, json=payload, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 1 or body.get("data") is None:
            raise DatatftError(f"{path} 返回异常: code={body.get('code')} msg={body.get('msg')}")
        return body["data"]

    # ---------- 端点 ----------
    def strategy(self, comp_id: int) -> dict:
        """阵容攻略详情 (核心/开局/前中后期/推荐出装/teamUrl/阵容码)."""
        return self._post("/team/strategy", {"id": int(comp_id)})

    def strategy_extra(self, comp_id: int) -> dict:
        """实战数据补充: tips + explorerDataList(每个弈子的装备统计)."""
        return self._post("/team/strategy/extra", {"id": int(comp_id)})

    def team_rank(self, season: str = config.SEASON) -> dict:
        """赛季榜单: {updateTime, list:[{id,tier,heroId,equipId,title,heros:[...]}]}."""
        return self._post("/team/rank", {"season": str(season)})

    def team_list(self, version: str, season: str = config.SEASON,
                  tier: str = "challenger", days: int = 5) -> dict:
        """分档热门阵容榜 (需要游戏版本号, 如 18.3b)."""
        return self._post("/team/list", {
            "version": version, "season": str(season), "tier": tier, "time": days,
        })

    # ---------- 组合 ----------
    def rank_entry(self, comp_id: int, season: str = config.SEASON) -> dict | None:
        """从榜单里取该阵容条目 (评级/主C/三星/成装), 取不到返回 None."""
        try:
            data = self.team_rank(season)
        except (requests.RequestException, DatatftError):
            return None
        for entry in data.get("list") or []:
            if int(entry.get("id", 0)) == int(comp_id):
                return entry
        return None

    def fetch_all(self, comp_id: int, season: str = config.SEASON) -> dict:
        """一次抓齐: strategy + extra + rank 条目."""
        return {
            "strategy": self.strategy(comp_id),
            "extra": self.strategy_extra(comp_id),
            "rank": self.rank_entry(comp_id, season),
        }
