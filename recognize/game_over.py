"""对局结束判断 (策略模式, config.GAME_OVER_STRATEGY 选择).

InfoTypes.IsOver 与 Game 组合的底层实现 (recognize/extract.py 派发).

    - "players": 玩家列表中自己条目 (name=None 的行, 见 extract.extract_players)
      判死亡, 满足任一即结束:
        a) 血量 <= 0 (基本准则, 含致命一击 "-N" 的负值);
        b) 血量读不到 (hp=None) 且行带内红色淘汰标记 × (实测死亡行 ~96 纯红像素,
           存活行/噪声 0-2; 仅对 hp=None 兜底, hp>0 行的红色是低血量红血条).
      局限: 低血量存活 (红血条) 曾致 b 误判 (2-3/3-2 实机取证), 已改为 hp=None 才判红.
      未传 players 时对 regions.PLAYERS 做裁剪区域 OCR (~0.8s), 传参复用免重复.
    - "end_screen": 结算画面 [现在退出]/[继续观看] 按钮灰度模板匹配, 任一命中
      即结束 (实测结束画面 1.00 / 对局中 ≤0.48; 不依赖 OCR 服务, ~0.05s).
    - "both": 上述任一策略命中即结束 (判定最及时).
"""

import time

import cv2
import numpy as np
from PIL import Image

from config.config import default_config
from recognize import ocr, regions

TEMPLATE_DIR = default_config.PROJECT_ROOT / "recognize" / "templates" / "game_over"
_TEMPLATES: list[np.ndarray] = []  # 结算按钮灰度模板懒加载缓存
ELIMINATION_RED_MIN = 40    # 行带内纯红像素数 ≥ 此值判为淘汰标记 × (实测死亡行 96, 存活行/噪声 0-2)
_RED_BAND_X = (1670, 1760)  # × 标记所在的行内横带 (血量框左侧固定位置)

# "上次刚判过未结束"的时刻 (monotonic)。只对**非结束(False)**结果计时 ——
# True 一律实时复核 (见 is_over_cached)。
_NO_OVER_T: float = -1e9


def reset_cache() -> None:
    """清空 is_over 缓存 (重开新局/单测隔离用): 下一次调用必然实时判定。"""
    global _NO_OVER_T
    _NO_OVER_T = -1e9


def is_over_cached(img, players: list[dict] | None = None) -> bool:
    """带短 TTL 缓存的 is_over —— **轮询链路专用** (config.GAME_OVER_CACHE, 用户口径 2026-10-17).

    为什么需要: 未传 ``players`` 时 is_over 要做玩家列表区域 OCR (实测 ~0.5s), 而动作前阶段检查
    与回合轮询每 2~3 秒就调一次 —— 实测它占 poll_state 总耗时的一半。

    缓存口径 (**只缓存 False**): "对局中"的判定在 GAME_OVER_CACHE_SECONDS 内直接复用, 不再付那趟
    OCR; 而 "已结束(True)" 一律实时复核 —— 缓存 True 会把一次瞬时误判放大成连续多次 (True 会触发
    丢帧/停写), 代价远高于收益。开关关闭时逐字节退回 ``is_over`` 原行为。
    """
    global _NO_OVER_T
    if not getattr(default_config, "GAME_OVER_CACHE", False):
        return is_over(img, players)
    ttl = float(getattr(default_config, "GAME_OVER_CACHE_SECONDS", 2.0) or 0.0)
    if ttl > 0 and time.monotonic() - _NO_OVER_T < ttl:
        return False                     # 上次刚判过"未结束", 直接复用 (True 从不进这条分支)
    value = bool(is_over(img, players))
    _NO_OVER_T = time.monotonic() if not value else -1e9   # 只给 False 计时; True 清掉缓存
    return value


def is_over(img, players: list[dict] | None = None) -> bool:
    """对局是否已结束; 策略见模块 docstring / config.GAME_OVER_STRATEGY.

    players: extract_info 本轮已提取的玩家列表 (复用免重复区域 OCR), 可为 None.
    """
    strategy = default_config.GAME_OVER_STRATEGY
    if strategy == "players":
        return _by_players(img, players)
    if strategy == "end_screen":
        return _by_end_screen(img)
    if strategy == "both":
        # 按钮模板 ~0.05s 先判, 命中即免 ~0.5s 的玩家区域 OCR
        return _by_end_screen(img) or _by_players(img, players)
    raise ValueError(f"未知 GAME_OVER_STRATEGY: {strategy!r}, 可选 'players' / 'end_screen' / 'both'")


def _by_players(img, players: list[dict] | None) -> bool:
    """策略1: 自己条目 (name=None) 判死.

    a) hp <= 0: 含致命一击 "-N" (extract_players 保留负号, 如 "-6"→-6);
    b) hp 读不到 (None) 且行带内有红色淘汰标记 ×.
       注意 b 仅对 hp=None 兜底: hp>0 的行带红色是低血量红血条
       (实测 hp=18 存活行行带 311 纯红像素, 远超死亡 × 的 96, 不能按红像素判死).

    自己行由 `extract.own_row_index` 唯一判定 (无名字 + 血量数字大字号左移), 它同时承担
    面板视图守卫 (2-6 实机误判教训): 右侧面板可切成"已造成伤害"视图, 此时标题被读成玩家行、
    伤害数字 (含 0) 全部无名且同样左移, 但"左移的无名行必须恰好一个"这一条会把它们排除;
    判不出自己行时一律跳过判定 (漏判由 end_screen 策略与 is_over 防抖兜底).
    """
    if players is None:
        from recognize.extract import extract_players  # 延迟导入: extract.py 顶层导入本模块, 避免循环
        try:
            players = extract_players(ocr.recognize_region(img, regions.PLAYERS, upscale=2))
        except Exception:  # noqa: BLE001 - 行数不足 8 被守卫拒绝: 当前帧看不到自己行, 当作未结束 (both 策略由 end_screen 兜底)
            players = []
    from recognize.extract import own_row_index  # 自己行判据的唯一实现 (含伤害视图守卫)
    idx = own_row_index(players)
    if idx is None:
        return False
    me = players[idx]
    if me.get("hp") is not None and me["hp"] <= 0:
        return True
    return me.get("hp") is None and _elimination_mark(_to_rgb(img), me["rect"])


def _elimination_mark(arr: np.ndarray, rect: tuple) -> bool:
    """自己行带内红色淘汰标记 × 判死 (阈值与实测值见 ELIMINATION_RED_MIN)."""
    x1, y1, x2, y2 = rect
    band = arr[max(0, int(y1) - 12):int(y2) + 12, _RED_BAND_X[0]:_RED_BAND_X[1]]
    if band.size == 0:
        return False
    r, g, b = (band[:, :, i].astype(int) for i in range(3))
    return int(((r > 170) & (r - g > 80) & (r - b > 80)).sum()) >= ELIMINATION_RED_MIN


def _by_end_screen(img) -> bool:
    """策略2: 结算画面 [现在退出]/[继续观看] 按钮模板匹配, 任一命中即结束."""
    g = cv2.cvtColor(_to_rgb(img), cv2.COLOR_RGB2GRAY)
    th = default_config.GAME_OVER_TEMPLATE_THRESHOLD
    for tpl in _templates():
        if tpl.shape[0] <= g.shape[0] and tpl.shape[1] <= g.shape[1]:
            if float(cv2.matchTemplate(g, tpl, cv2.TM_CCOEFF_NORMED).max()) >= th:
                return True
    return False


def _templates() -> list[np.ndarray]:
    if not _TEMPLATES:
        for path in sorted(TEMPLATE_DIR.glob("*.png")):
            tpl = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if tpl is not None:
                _TEMPLATES.append(tpl)
    return _TEMPLATES


def _to_rgb(img) -> np.ndarray:
    """PIL / numpy 输入统一为 RGB numpy 数组."""
    if isinstance(img, Image.Image):
        return np.asarray(img.convert("RGB"))
    return np.asarray(img)


# ---- 结算页名次 (用户 2026-10-25 要求: 这是唯一权威来源, 不靠推断) ----

# 中文数字 -> 阿拉伯数字。游戏结算页写的是"第八名"这种汉字大写。
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_cn_rank(text: str) -> int | None:
    """从 OCR 文本里解析名次数字; 认不出返回 None (**绝不猜**)。

    实测样本: "第八名" -> 8; 也兼容 "第8名" / "第 8 名" / "第一名" 这类写法。
    只接受 1~10 (金铲铲 8 人局, 留一点容差), 超出范围视作识别错误。
    """
    if not text:
        return None
    s = str(text)
    # 阿拉伯数字优先 (更不容易错): 第8名 / 第 8 名
    import re
    m = re.search(r"第\s*(\d{1,2})\s*名", s)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 10 else None
    # 汉字数字: 第八名 / 第十名
    m = re.search(r"第\s*([一二三四五六七八九十])\s*名", s)
    if m:
        return _CN_NUM.get(m.group(1))
    # 冠军/胜利结算页: 拿到第一名时游戏写的是"胜利/冠军 / 大获全胜", 不一定写"第X名"
    # (用户 m04073: 拿了第一名却因为没解析出"第一名"而没被记入名次)。这几个词 = 第 1 名。
    if ("冠军" in s) or ("胜利" in s) or ("吃鸡" in s) or ("全胜" in s):
        return 1
    return None


def my_rank(img) -> int | None:
    """从结算画面读"您获得了 第X名", 返回 int; 读不到返回 None。

    **为什么必须走这条路** (2026-10-25 实机教训):
    那一局我们用内存 `isDead` 观察出局顺序算名次, 把"我们自己"排成第 7 名, 而结算页
    明明白白写着"第八名" —— 原因是同一轮里我们和另一家同时出局 (观察间隔仅 3s, 落在
    内存采样噪声内), 先后顺序根本分不出来; 而游戏按"该轮结束时的血量"排名 (我们 1 血
    < 对方 5 血, 所以我们更低)。结算页是游戏自己算的, 是唯一权威来源, 直接用。

    识别失败只返回 None, 由调用方标记"名次未知" —— 宁可没有, 不许编一个。
    """
    from recognize import ocr, regions

    try:
        items = ocr.recognize_region(img, regions.GAME_OVER_RANK, upscale=2)
    except Exception as exc:      # noqa: BLE001 - OCR 服务抖动不能影响重开流程
        print(f"[名次] 结算页 OCR 失败({type(exc).__name__}): {exc!r}", flush=True)
        return None
    if not items:
        return None
    # 按 y 排序还原阅读顺序, 便于打印原文排查
    items = sorted(items, key=lambda it: (it.get("box") or (0, 0, 0, 0))[1])
    texts = [str(it.get("text") or "") for it in items]
    # 优先在含"名"的那一行里找; 找不到再全量拼起来找 (OCR 可能把两行并成一条)
    for t in texts:
        if "名" in t:
            n = _parse_cn_rank(t)
            if n is not None:
                return n
    n = _parse_cn_rank(" ".join(texts))
    if n is None:
        print(f"[名次] 结算页名次文字没解析出来 (OCR 原文: {texts!r})", flush=True)
    return n
