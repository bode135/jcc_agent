"""棋子名白名单: 区分「真棋子」与备席里的消耗品道具。

## 为什么需要 (用户 m01156 现场)

备席格里除了棋子, 还放**消耗品道具** —— 「散件锻造器」「成装锻造器」「装备锻造器」
「基础装备锻造器」这类。它们在内存里**也是一条 UnitData**, 有 heroName、有 1xxxx 的
tabId、`star` 算出来是 1、连 `_isRealHero` 都是 True (2026-10-29 实测 bench,2/bench,3
两颗「散件锻造器」: `tabId=18996 heroId=11/12 hp=250 isRealHero=True`), 光靠内存字段
**分不出来**。

而道具**只能卖掉, 拖不上棋盘**。下游若把它当棋子拖, 就是连拖数次全 NG ——
2026-10-29 验收实机日志里 `place place bench2 -> (1,1)` 连拖 6 次, 每次都
`执行后 board,1,1 仍是空的 → 棋子没上去 (起点 bench,2 还是 散件锻造器)`,
白烧 prep 窗口, 用户当场看到「你怎么一直试图拖装备锻造器上场」。

## 真值源

`train_yolo/classes.json` —— 项目**冻结**的棋子类别表 (口径见该文件 `_comment`:
units.json 的 65 个 unit + extraUnits 的 9 个, 皮肤已合并)。它的 `classes` 数组
**全是棋子** (含 苍蓝哨戒/绯红树怪/魔沼蛙 这类野怪棋子), `extra` 数组**全是非棋子**
(基础装备锻造器/成装锻造器/ball/小小英雄/unknown/石皮树/生命花)。

判据取「名字在 `classes` 里 = 棋子」。**不在表里的一律当非棋子** (不拖它) ——
这是"宁可漏采, 不可采错"的取向: 万一游戏出了新棋子没进表, 代价是"这次不拿它上阵"
(漏), 而不是"把道具拖上场连烧窗口 + 断言全 NG" (错)。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_CLASSES_PATH = Path(__file__).resolve().parents[1] / "train_yolo" / "classes.json"


@lru_cache(maxsize=1)
def _load() -> tuple[frozenset[str], frozenset[str]]:
    """读 `classes.json` -> (棋子名集合, 非棋子名集合)。读不到就返回两个空集 (不猜)。"""
    try:
        with open(_CLASSES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:      # noqa: BLE001 - 表缺失不该让采集/验收崩掉, 退回"全当棋子"的旧行为
        return frozenset(), frozenset()
    units = frozenset(str(x).strip() for x in (raw.get("classes") or []) if str(x).strip())
    extras = frozenset(str(x).strip() for x in (raw.get("extra") or []) if str(x).strip())
    return units, extras


def unit_classes() -> frozenset[str]:
    """棋子名集合 (`classes.json` 的 `classes`)。"""
    return _load()[0]


def extra_classes() -> frozenset[str]:
    """明确非棋子的类别 (`classes.json` 的 `extra`)。"""
    return _load()[1]


def is_playable_unit(name: str | None) -> bool:
    """这个名字能不能当棋子用 (拖上场 / 卖 / 换位)。

    Returns:
        True  = 是棋子 (在 `classes` 白名单里)。
        False = 不是棋子 (道具 / 未收录 / 表缺失时的保守取值见下)。

    表读不到时 (`classes` 为空) **返回 True** —— 那是"没有白名单可用", 退回加过滤前的
    老行为, 不能因为一个 json 缺失就让整个补人口/造前提瘫痪。
    """
    nm = (name or "").strip()
    if not nm:
        return False
    units, _extras = _load()
    if not units:
        return True
    return nm in units


def not_playable_why(name: str | None) -> str:
    """给日志用的说法: 为什么这颗不能当棋子用。"""
    nm = (name or "").strip()
    if not nm:
        return "名字没读出来"
    units, extras = _load()
    if not units:
        return f"{nm}: 棋子白名单缺失 (train_yolo/classes.json 读不到), 按老行为放行"
    if nm in units:
        return f"{nm}: 是棋子"
    if nm in extras:
        return f"{nm}: 是非棋子类别 (classes.json 的 extra), 拖不上场, 只能卖"
    return f"{nm}: 不在棋子白名单里 (道具? 新棋子没进表?), 不拿它上场"
