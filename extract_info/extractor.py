"""对局信息提取实现: 整图一次 OCR, 按 regions 坐标切片解析三组信息.

纯图色方案: 只依赖截图像素 (OCR + 区域切片 + 颜色/模板判定), 不读游戏进程内存.

输出结构 (字段解析失败时为 None/空, 不抛异常; 单个子项异常记进 errors 而非中断):
每条记录均带 rect 位置方框 [x1, y1, x2, y2], 供后续自动化操作定位:
    {
      "players": [{"id": "飞驰的钻石", "hp": 80, "rect": [x1, y1, x2, y2]}, ...],
      "shop":    [{"name": "岩阿娜", "price": 2, "rect": [x1, y1, x2, y2]}, ...],
      "game":    [{"key": "level", "value": 5, "rect": [x1, y1, x2, y2]}, ...],
    }
game 组字段 (键序固定): stage 回合号 / round_time 回合剩余秒数 / level 等级 / xp 经验 /
gold 金币 / xp_buy_cost 购买经验费用 / refresh_cost 刷新费用 / free_refresh 免费刷新次数

选择性提取 (only=None 全量):
    extract(img, only={"shop"}) 只抽商店 —— 先走 recognize.extract 的商店窄带快路径
    (只裁底部商店横带, 不跑整图 OCR), 读不全 (缺卡名/缺价格) 时自动回落整图 OCR 一次重抽;
    含 players/game 时仍需整图一次 OCR (多字段共用).

耗时: 传入 timings dict 时按步骤写入秒数 (ocr.recognize / players / shop / game.<key>),
      便于 CLI 打印"OCR 一次多久"; 传入 errors dict 时记录失败步骤的错误文本.
"""

import time

from config.config import InfoTypes
from recognize import extract as base
from recognize import ocr, regions

# game 组字段顺序 (对外契约, CLI 依赖该顺序; phase/hex 由 CLI 在 stage 之后插入)
GAME_KEYS = ("stage", "round_time", "level", "xp", "gold",
             "xp_buy_cost", "refresh_cost", "free_refresh")

# game 各字段对应的截图区域 (与 recognize/regions.py 同源)
GAME_RECTS = {
    "stage": regions.STAGE,
    "round_time": regions.ROUND_TIME,
    "level": regions.LEVEL,
    "xp": regions.XP,
    "gold": regions.GOLD,
    "xp_buy_cost": regions.XP_BUY_COST,
    "refresh_cost": regions.REFRESH_COST,
    "free_refresh": regions.FREE_REFRESH,
}

ALL_GROUPS = ("game", "players", "shop")


def _sec(t0: float) -> float:
    """perf_counter 差值 -> 秒 (3 位小数, 便于日志紧凑)."""
    return round(time.perf_counter() - t0, 3)


def _rect(rect):
    """rect 统一成 list (JSON 友好; tuple 会被 json 转成 list, 这里提前统一便于后续代码处理)."""
    if rect is None:
        return None
    if isinstance(rect, (list, tuple)):
        return [int(v) if isinstance(v, float) and v.is_integer() else v for v in rect]
    return rect


def _run(label: str, fn, timings: dict, errors: dict):
    """执行一个识别步骤: 成功返回结果并记耗时, 失败返回 None 并记错误 (不向外抛)."""
    t0 = time.perf_counter()
    try:
        out = fn()
        timings[label] = _sec(t0)
        return out
    except Exception as exc:  # noqa: BLE001 - 单步失败不能拖垮整次抽取
        timings[label] = _sec(t0)
        errors[label] = f"{type(exc).__name__}: {exc}"
        return None


def _players(records) -> list[dict]:
    return [{"id": p.get("name"), "hp": p.get("hp"), "rect": _rect(p.get("rect"))}
            for p in (records or [])]


def _shop(records) -> list[dict]:
    return [{"name": s.get("name"), "price": s.get("price"), "rect": _rect(s.get("rect"))}
            for s in (records or [])]


def _shop_complete(records: list[dict]) -> bool:
    """窄带商店结果是否可采信: 5 槽全无卡名 -> 商店空/被遮罩, 采信; 有卡名但缺名或缺价 -> 需回落."""
    if not records:
        return False
    if all(s["name"] is None for s in records):
        return True
    return all(s["name"] is not None and s["price"] is not None for s in records)


def _game_value(key: str, items, img):
    """按 key 取值: 前四个来自整图 OCR items, 后四个走各自裁剪区域."""
    if key == "stage":
        return base.extract_stage(items) if items is not None else None
    if key == "round_time":
        return base.extract_round_time(items, img) if items is not None else None
    if key == "level":
        return base.extract_level(items) if items is not None else None
    if key == "xp":
        return base.extract_xp(items) if items is not None else None
    if key == "gold":
        return base.extract_gold(img)
    if key == "xp_buy_cost":
        return base.extract_xp_buy_cost(img)
    if key == "refresh_cost":
        return base.extract_refresh_cost(img)
    if key == "free_refresh":
        return base.extract_free_refresh(img)
    raise KeyError(key)


def extract(img, only=None, timings: dict | None = None,
            errors: dict | None = None) -> dict:
    """从整张截图中提取 玩家/商店/全局 三组对局信息.

    Args:
        img: 1920×1080 截图 (PIL Image; 其它尺寸由调用方先归一化).
        only: 只抽取部分组, 如 {"shop"} / {"game","players"}; None = 三组全抽.
        timings: 可选 dict, 按步骤写入耗时秒数 (OCR 一次多久看 "ocr.recognize").
        errors: 可选 dict, 记录失败步骤的错误文本 (值为 None/空的字段即解析失败).

    Returns:
        {"players": [...], "shop": [...], "game": [...]}, 只含 only 中请求的组.
    """
    timings = {} if timings is None else timings
    errors = {} if errors is None else errors
    groups = set(ALL_GROUPS) if only is None else (set(only) & set(ALL_GROUPS))
    if not groups:
        raise ValueError(f"only 必须包含 {ALL_GROUPS} 中的至少一项, 收到 {only!r}")

    # 只抽商店: 先走窄带快路径 (只裁底部商店横带, 不跑整图 OCR; 实测快 3~4s),
    # 但窄带区域 OCR 在实机样本上会丢价格/截断卡名 (实测 board_ingame.png: 价格全 None,
    # 卡名 '芸阿娜'->'芸阿'), 所以读不全就回落整图 OCR 一次重抽 —— 数据正确性优先 (不猜, 宁慢不错).
    # 商店空/被遮罩 (5 槽全无卡名) 时直接采信窄带结果, 不为空商店白跑一次整图 OCR.
    if groups == {"shop"}:
        res = _run("shop", lambda: base.extract_info(img, [InfoTypes.Shop]), timings, errors)
        shop = _shop((res or {}).get("shop"))
        if _shop_complete(shop):
            return {"shop": shop}
        items = _run("ocr.recognize(商店回落)", lambda: ocr.recognize(img), timings, errors)
        full = _run("shop(回落整图)", lambda: base.extract_shop(items, img), timings, errors)
        return {"shop": _shop(full)}

    # 整图一次 OCR, players / game(前四项) / shop 共用同一份 items
    items = _run("ocr.recognize", lambda: ocr.recognize(img), timings, errors)
    out: dict = {}

    if "players" in groups:
        out["players"] = _players(_run(
            "players", lambda: base.extract_players(items, img), timings, errors))
    if "shop" in groups:
        out["shop"] = _shop(_run(
            "shop", lambda: base.extract_shop(items, img), timings, errors))
    if "game" in groups:
        records = []
        for key in GAME_KEYS:
            val = _run(f"game.{key}", lambda k=key: _game_value(k, items, img),
                       timings, errors)
            records.append({"key": key, "value": val, "rect": _rect(GAME_RECTS[key])})
        out["game"] = records
    return out
