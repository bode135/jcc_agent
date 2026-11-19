"""从 1920×1080 截图中提取结构化对局信息, 输出可 json 序列化的 info dict.

策略: 整图做一次 OCR (约 2s), 再按 regions 的坐标切片解析各字段;
extract_info(img, info_type) 支持 InfoTypes 按需提取, 跳过未选中的识别步骤.

info 结构 (字段解析失败时为 None/空, 不抛异常):
    {
      "stage": "2-6",                # 回合进度
      "round_time": 20,              # 该回合剩余时间(秒)
      "level": 4,                    # 等级
      "xp": [10, 18],                # 经验 [当前, 升到下一级所需] (等级旁进度条数字)
      "gold": 39,                    # 金币
      "xp_buy_cost": 4,              # 购买经验按钮费用
      "refresh_cost": 0,             # 刷新按钮费用 (有免费刷新时显示 0)
      "free_refresh": 10,            # 免费刷新次数 (无徽章时为 0)
      "traits": [                    # 左侧羁绊列表, 自上而下
          {"count": 2, "name": "护卫", "threshold": [2, 4, 6]}, ...],
      "players": [                   # 右侧玩家血量列表, 自上而下 (含自己); rect=玩家行包围盒
          {"name": "吃素甘宝店", "hp": 100, "rect": (x1, y1, x2, y2)}, ...],
      "shop": [                      # 商店 5 张卡牌; rect=卡牌槽位方框 (恒定, 与识别结果无关)
          {"name": "阿卡丽", "price": 1, "traits": ["地狱火", ...], "type": "unit",
           "chess_id": 918016, "db_price": 1, "races": ["地狱火"], "jobs": ["魔战士"],
           "attack_range": 2, "rect": (...)},
                                        # type: "unit"=奕子 / "charm"=仙灵法杖 / None=名称未匹配;
                                        # unit 的 chess_id/db_price/races/jobs/attack_range 来自爬虫库
                                        # (db_price 为 1 星基准价, 商店直接出现两星时 OCR price≈3×db_price);
                                        # charm 无棋子字段, effect=效果文本 (名称查库, 名称未读出时以
                                        # 卡身 OCR 长句兜底), 如
                                        # {"name": "巨型增长", "price": 4, "type": "charm",
                                        #  "effect": "你生命值最高的那个单位成长，获得750生命值。"}, ...],
      "board": [                     # 场上棋子 (血条定位+模板匹配, 见 recognize/board.py)
          {"name": "金甲剑士", "score": 0.98, "center": [x, y]}, ...],
      "bench": [                     # 备战席棋子, 结构同 board
          {"name": None, "score": 0.41, "center": [x, y]}, ...],
      "equip": [                     # 左侧装备栏 (equipment.extract_equipment, 同 equip 命令)
          {"slot": 1, "row": 1, "col": 1, "equipId": 503, "name": "无用大棒", "type": "part",
           "count": None, "formula": None, "score": 0.87, "center": [x, y]}, ...],
      "phase": "prep 准备阶段",       # 对局阶段 (phase.detect, 见 recognize/phase.py)
      "hex": "hex_0 海克斯阶段-未选择海克斯",  # hex_0 未选择 / hex_1 已选择 / None 非海克斯界面
      "hextech": [                   # 海克斯强化符文卡 (仅未选择时有 3 张; augment.extract)
          {"card_no": 1, "name": "棱彩命运+", "desc": "获得1个随机的棱彩阶强化符文和7金币。",
           "refresh_left": 1, "center": (458, 510), "rect": (...), ...}, ...],
      "little_legend": [             # 小小英雄位置 (棋盘+备战席合并, structure 同 board 项)
          {"name": "小小拉莫斯", "score": 0.95, "center": [x, y], "region": "board"}, ...],
      "is_over": True,               # 对局是否已结束 (game_over.is_over, 策略 config.GAME_OVER_STRATEGY)
    }

说明与已知局限:
    - 当前 UI 无经验值数字 (等级徽章下方为蓝色进度条);
    - 玩家名全是数字时会被误判为血量;
    - 血量偶与头像特效粘连多读一位 (如 "82"→"782"), 超过 130 时取末两位;
    - 价格取最后一位数字 (金币图标偶与数字粘连误读, 如 "3"→"93"),
      因此价格为 0 的道具卡可能识别为 1.
"""

import re
import time

import numpy as np
from PIL import Image
from bdtime import Time
from config.config import InfoTypes, default_config
from recognize import augment, board, equipment, game_over, ocr, phase, regions
from units import db as unit_db

_tt = Time()  # 分步耗时的累计参考 (与 game.py 的 tt 风格一致)


def _timed(label: str, fn, *args, **kw):
    """执行识别步骤; EXTRACT_TIMING_DEBUG=True 时按 bdtime 风格打印该步耗时."""
    t0 = time.perf_counter()
    out = fn(*args, **kw)
    if default_config.EXTRACT_TIMING_DEBUG:
        print(f"--- extract.{label} --- {time.perf_counter() - t0:.2f}s --- tt.now: {_tt.now()}")
    return out


# 按提取策略分组: 决定 extract_info 需要跑哪些底层识别
_ITEMS_KEYS = {InfoTypes.Stage, InfoTypes.RoundTime, InfoTypes.Level, InfoTypes.Xp,
               InfoTypes.Gold, InfoTypes.Traits, InfoTypes.Players, InfoTypes.Shop}
_CROP_KEYS = {InfoTypes.XpBuyCost, InfoTypes.RefreshCost, InfoTypes.FreeRefresh}
_DETECT_KEYS = {InfoTypes.Board, InfoTypes.Bench, InfoTypes.LittleLegend}
_EQUIP_KEYS = {InfoTypes.Equip}  # 装备栏走 equipment.extract_equipment, 与棋盘检测独立
_PHASE_KEYS = {InfoTypes.Phase, InfoTypes.Hex, InfoTypes.Hextech}  # 共用 phase.detect+augment.extract
_ISOVER_KEYS = {InfoTypes.IsOver}  # 结束判断走 game_over.is_over, 策略见 config.GAME_OVER_STRATEGY
# 2026-10-17 用户要求: 纯 OCR 的棋盘检测 (board/bench/little_legend) 错误率高, 会误导复盘,
# **不再并入 InfoTypes.All**; 只支持显式请求 (固定开局/离线回退等内部调用)。
# 日常要场上/备战席奕子信息请走动态探测: python -m scripts.game units --board / --bench。
_ALL_KEYS = _ITEMS_KEYS | _CROP_KEYS | _EQUIP_KEYS | _PHASE_KEYS | _ISOVER_KEYS
_KNOWN_KEYS = _ALL_KEYS | _DETECT_KEYS  # 合法键集合 (含显式请求才有的棋盘检测键)
# 窄带快路径键 (用户口径 2026-10-17: "识别某个特定区域的数值就不要牵扯到其它区域, 甚至要识别全图"):
# 这四项只依赖"顶栏横带 OCR + 模板匹配", 绝不跑整图 OCR —— Stage/RoundTime 共用同一次顶栏横带识别.
_NARROW_KEYS = {InfoTypes.Stage, InfoTypes.RoundTime, InfoTypes.Phase, InfoTypes.IsOver}
# 商店/等级/金币的窄带键: 这三项各有自己的裁剪区域 (商店横带 / 等级区 / 金币数字槽), 同样不需要整图 OCR.
_SHOP_NARROW_KEYS = {InfoTypes.Shop, InfoTypes.Level, InfoTypes.Gold}
_GAME_KEYS = {InfoTypes.Stage, InfoTypes.Phase, InfoTypes.Hex, InfoTypes.RoundTime,
              InfoTypes.Level, InfoTypes.Xp, InfoTypes.Gold,
              InfoTypes.XpBuyCost, InfoTypes.RefreshCost, InfoTypes.FreeRefresh,
              InfoTypes.IsOver}


def _normalize_info_types(info_type) -> set:
    """InfoTypes 字符串或其列表 -> 想要的键集合; 展开组合 All/Game, 未知类型报错."""
    if isinstance(info_type, str):
        info_type = [info_type]
    want: set = set()
    for t in info_type:
        if t == InfoTypes.All:
            want |= _ALL_KEYS
        elif t == InfoTypes.Game:
            want |= _GAME_KEYS
        elif t in _KNOWN_KEYS:
            want.add(t)
        else:
            raise ValueError(f"未知 InfoTypes: {t!r}, 可选 {sorted(_KNOWN_KEYS | {InfoTypes.Game, InfoTypes.All})}")
    return want


def extract_info(img, info_type=InfoTypes.All) -> dict:
    """按 info_type 选择性提取对局信息 (默认 InfoTypes.All 全量).

    传 InfoTypes.Shop 只提取商店; 传列表如 [InfoTypes.Shop, InfoTypes.Game] 提取并集.
    未选中的组不会执行其底层识别 (整图 OCR / 棋盘检测 / 裁剪 OCR), 显著提速.

    注意: InfoTypes.All 不含 board/bench/little_legend (纯 OCR 棋盘检测错误率高, 用户 2026-10-17);
    需要这两项时必须**显式**传 InfoTypes.Board / InfoTypes.Bench (内部调用用), 日常请用动态探测.
    """
    want = _normalize_info_types(info_type)
    if want and want <= _NARROW_KEYS:  # 只读顶栏/模板: 别为了一个数值去跑整图 OCR (用户口径 2026-10-17)
        return _extract_narrow(img, want)
    if InfoTypes.Shop in want and want <= _SHOP_NARROW_KEYS:  # 商店/等级/金币: 各自裁剪区域, 同样不跑整图
        return _extract_shop_narrow(img, want)
    res = {}
    if want & _PHASE_KEYS:  # 阶段/海克斯/符文卡共用一次 phase.detect + augment.extract
        ph = _timed("phase.detect", phase.detect, img)
        aug = _timed("augment.extract", augment.extract, img)
        if InfoTypes.Phase in want:
            res["phase"] = phase.PHASE_NOTES.get(ph["phase"], ph["phase"])
        if InfoTypes.Hex in want:
            res["hex"] = ("hex_1 海克斯阶段-已选择海克斯" if aug["chosen"]
                          else "hex_0 海克斯阶段-未选择海克斯") if aug["is_augment"] else None
        if InfoTypes.Hextech in want:
            res["hextech"] = [{"card_no": i, **card} for i, card in enumerate(aug["cards"], 1)]
    if want & _DETECT_KEYS:  # 模板检测一次拿到 board+bench (需要小小英雄时开启 include_legend)
        units = _timed("board.extract_units", board.extract_units, img,
                       include_legend=InfoTypes.LittleLegend in want)
        if InfoTypes.Board in want:
            res["board"] = units["board"]
        if InfoTypes.Bench in want:
            res["bench"] = units["bench"]
        if InfoTypes.LittleLegend in want:
            res["little_legend"] = [
                {**u, "region": region}
                for region in ("board", "bench") for u in units[region] if u.get("legend")
            ]
    costs = None
    if want & {InfoTypes.XpBuyCost, InfoTypes.RefreshCost}:
        costs = _timed("extract_button_costs", extract_button_costs, img)  # 相邻两按钮费用合并一次 OCR
    if want == {InfoTypes.Gold}:  # 单键 gold: 跳过整图 OCR, 直接数字槽裁剪识别
        res["gold"] = extract_gold(img)
    elif want & _ITEMS_KEYS:  # 整图一次 OCR, 各字段按区域切片
        items = _timed("ocr.recognize(整图)", ocr.recognize, img)
        if InfoTypes.Stage in want:
            res["stage"] = extract_stage(items)
        if InfoTypes.RoundTime in want:
            res["round_time"] = extract_round_time(items, img)
        if InfoTypes.Level in want:
            res["level"] = extract_level(items)
        if InfoTypes.Xp in want:
            res["xp"] = extract_xp(items)
        if InfoTypes.Gold in want:
            res["gold"] = extract_gold(img)
        if InfoTypes.Traits in want:
            res["traits"] = extract_traits(items)
        if InfoTypes.Players in want:
            res["players"] = extract_players(items, img)
        if InfoTypes.Shop in want:
            res["shop"] = _timed("extract_shop(含兜底OCR)", extract_shop, items, img)
    if costs is not None:
        if InfoTypes.XpBuyCost in want:
            res["xp_buy_cost"] = costs["xp_buy_cost"]
        if InfoTypes.RefreshCost in want:
            res["refresh_cost"] = costs["refresh_cost"]
    if InfoTypes.FreeRefresh in want:
        res["free_refresh"] = _timed("extract_free_refresh", extract_free_refresh, img)
    if InfoTypes.Equip in want:  # 装备栏完整识别 (含消耗品/道具), 同 equip 命令
        res["equip"] = _timed("equipment.extract_equipment", equipment.extract_equipment, img)
    if InfoTypes.IsOver in want:  # 对局结束判断 (players 已提取时复用, 免重复区域 OCR;
        # 走 game_over.is_over_cached: 短 TTL 内复用"未结束"结果, 见 config.GAME_OVER_CACHE)
        res["is_over"] = _timed("game_over.is_over", game_over.is_over_cached, img,
                                players=res.get("players"))
    return res


def _extract_narrow(img, want: set) -> dict:
    """窄带快路径: Stage/RoundTime/Phase/IsOver 只读顶栏横带与模板, **不跑整图 OCR**.

    用户口径 (2026-10-17): "识别某个特定区域的数值就不要牵扯到其它区域, 甚至要识别全图"。
    实测根因: POLL 四项 (Stage/Phase/IsOver/RoundTime) 原走整图 OCR 一次 ~1.5s, 每个动作前都付。
    这里 Stage 与 RoundTime 共用**同帧同一次**顶栏横带识别 (STAGE_BAND); Phase 走 phase.detect
    (模板匹配为主 + 一次 stage 小裁剪), IsOver 走 game_over.is_over (区域/模板, 非整图)。
    """
    res = {}
    if want & {InfoTypes.Stage, InfoTypes.RoundTime}:  # 两个字段共用一次顶栏横带识别
        items = _timed("ocr.recognize_region(顶栏横带)", ocr.recognize_region, img, STAGE_BAND, 1)
        if InfoTypes.Stage in want:
            res["stage"] = extract_stage(items)
        if InfoTypes.RoundTime in want:
            res["round_time"] = extract_round_time(items, img)
    if InfoTypes.Phase in want:
        ph = _timed("phase.detect", phase.detect, img)
        res["phase"] = phase.PHASE_NOTES.get(ph["phase"], ph["phase"])
    if InfoTypes.IsOver in want:  # 短 TTL 复用"未结束"结果 (config.GAME_OVER_CACHE)
        res["is_over"] = _timed("game_over.is_over", game_over.is_over_cached, img)
    return res


def _extract_shop_narrow(img, want: set) -> dict:
    """商店/等级/金币的窄带快路径: 各字段只裁自己的区域, **不跑整图 OCR** (用户口径 2026-10-17).

    Shop 只裁底部商店横带 regions.SHOP —— 名称/羁绊/价格/卡身长句全在这一带内 (`extract_shop`
    就是按该带切片, 传裁剪 items 与传整图 items 等价; 缺名/缺价的单槽兜底 OCR 也照常走)。
    Level 只裁 regions.LEVEL; Gold 走 extract_gold 的数字槽裁剪 (本就有快路径)。
    """
    res = {}
    if InfoTypes.Shop in want:
        fast = None
        # 图形学加速路径 (SHOP_GEO_RECOGNIZE, 首选): 固定槽+图形学定文字rect+免det rec+形近字轻校验。
        # 任一槽 name 为 None -> 回落 (不猜, 宁慢不错)。默认关, 用实机样本验证达标后再开。
        if getattr(default_config, "SHOP_GEO_RECOGNIZE", False):
            geo = _timed("extract_shop_geo(图形学+免det)", extract_shop_geo, img)
            if not any(s["name"] is None for s in geo):
                fast = geo
        if fast is None and getattr(default_config, "SHOP_FAST_RECOGNIZE", False):
            fast = _timed("extract_shop_fast(固定格+免det)", extract_shop_fast, img)
            # 数据正确性优先: 有槽名读不出就回落原 extract_shop 兜底补全 (不猜, 宁慢不错)
            if any(s["name"] is None for s in fast):
                fast = None
        if fast is not None:
            # 价格: geo/fast 两条快路径都只读名字 (price=None), 这里用免 det 快路径补上
            # (用户 2026-11-19)。实测补 5 槽 ~28ms, 而原来走 det 合并带要 340~460ms。
            if getattr(default_config, "SHOP_PRICE_GEO_RECOGNIZE", True):
                _prices = _slot_prices(img, list(range(len(fast))))
                for _i, _slot in enumerate(fast):
                    if _slot.get("price") is None and _i in _prices:
                        _slot["price"] = _prices[_i]
            res["shop"] = fast
        else:
            items = _timed("ocr.recognize_region(商店横带)", ocr.recognize_region, img, regions.SHOP, 1)
            res["shop"] = _timed("extract_shop(含兜底OCR)", extract_shop, items, img)
    if InfoTypes.Level in want:
        items = _timed("ocr.recognize_region(等级区)", ocr.recognize_region, img, regions.LEVEL, 1)
        res["level"] = extract_level(items)
    if InfoTypes.Gold in want:
        res["gold"] = extract_gold(img)
    return res


_REMAIN_JITTER = 2.0  # 剩余时间"不降反升"的容差 (秒): 超过它才判加时赛, 挡 OCR 的 ±1 抖动


class RemainTimeCache:
    """回合剩余时间的**本地时间预测缓存** (用户口径 2026-10-17).

    设计 (逐条对齐用户要求):
      * 第一次在某 (回合号 stage, 阶段 phase) 下读到 remain_time 时, 记下该值 + 本地起点时间,
        并把这次读数记为"首次读数" first;
      * 之后同一 (stage, phase) 内不再跑 OCR, 直接用 `基准读数 - 本地已过秒数` 推算剩余时间;
      * 过期门槛: prep = 0 (推算到 0 才重读 —— 用户: "prep 阶段确实必然走完", 不怕被延长);
        combat = config.REMAIN_CACHE_COMBAT_KEEP (默认 5s, "推算到 5 秒才重读") —— combat 可能被
        **突然缩短** (15s->3s) 也可能被**延长** (3-2-1s 后又跳回 20s), 所以不能整段都信;
      * 加时赛: 同一 (stage, phase) 内一旦出现"剩余时间不降反升"(高于推算值一个容差, 如 1s->20s),
        或读数大于"首次读数", 即置位 overtime (粘住直到换 (stage, phase)) —— 供日志/后续逻辑用;
      * 关闭 config.REMAIN_TIME_CACHE 或 stage/phase 缺失时: 一律现读, overtime 恒 False
        (宁多花时间, 不采错).
    """

    def __init__(self) -> None:
        self.key = None       # 当前 (stage, phase)
        self.base = None      # 基准读数
        self.at = None        # 基准读数对应的本地时间 (perf_counter)
        self.first = None     # 第一次进入本 (stage, phase) 时读到的剩余时间
        self.overtime = False

    def reset(self, key=None) -> None:
        """换 (stage, phase) 或手动清空 (如重开对局)。"""
        self.key, self.base, self.at, self.first, self.overtime = key, None, None, None, False

    def read(self, stage, phase, fresh):
        """返回 (remain_time, overtime). fresh() = 现读函数 (返回 int/float/None), **仅在缓存失效时调用**."""
        if not getattr(default_config, "REMAIN_TIME_CACHE", False) or not stage or not phase:
            return fresh(), False
        key = (stage, phase)
        if key != self.key:
            self.reset(key)
        keep = (float(getattr(default_config, "REMAIN_CACHE_COMBAT_KEEP", 5.0))
                if str(phase).strip().lower() == "combat" else 0.0)
        now = time.perf_counter()
        # 缓存失效 (没基准 / 推算已跌到门槛以内) -> 现读一次, 刷新基准与加时赛判定
        if self.base is None or (self.base - (now - self.at)) <= keep:
            v = fresh()
            if v is not None:
                prev = (self.base - (now - self.at)) if self.base is not None else None
                if self.first is None:
                    self.first = float(v)
                elif float(v) > self.first + _REMAIN_JITTER or (
                        prev is not None and float(v) > prev + _REMAIN_JITTER):
                    self.overtime = True   # 不降反升 = 战斗被延长 (加时赛)
                self.base, self.at = float(v), now
        if self.base is None:  # 现读也失败: 无值可给, 保持 overtime 现状
            return None, self.overtime
        pred = self.base - (time.perf_counter() - self.at)
        return max(0, int(pred)), self.overtime


remain_clock = RemainTimeCache()  # 进程级单例 (采集进程 / 决策进程各持一份, 互不干扰)


STAGE_BAND = (600, 0, 1500, 60)  # 顶栏横带 (与 round_time_by_anchor 同带): 回合号漂移兜底搜索区


def extract_stage(items) -> str | None:
    """回合进度, 如 "2-6". 固定区域读不到时在顶部横带内锚定兜底.

    实锤 (2026-10-05): 顶栏居中布局随羁绊图标数量漂移, 开局图标少时回合号右移
    ~80px 跑出固定区域 (1-3 在 x728-804, 固定区域右缘 733) → 横带内 "N-N" 格式
    唯一 (计时器/金币均为纯数字), 逐条目匹配避免跨条目拼接粘连.
    """
    m = re.search(r"\d+\s*-\s*\d+", _join(_region_items(items, regions.STAGE)))
    if m is None:  # 逐条目搜横带, 复用整图 OCR 结果零额外开销
        for it in _region_items(items, STAGE_BAND):
            m = re.search(r"\d+\s*-\s*\d+", it["text"])
            if m:
                return m.group().replace(" ", "")
        return None
    return m.group().replace(" ", "")


def round_time_by_anchor(img) -> int | None:
    """以回合号为锚在顶部横带内找计时器数字 (顶栏居中布局随图标数量漂移, 固定区域不可靠).

    横带内格式唯一的 "N-N" 是回合号, 其右侧 150px 外的唯一 1-3 位纯数字即剩余秒数.
    """
    band = ocr.recognize_region(img, (600, 0, 1500, 60), upscale=1)  # 实测不放大识别质量不降, 首请求更快
    stage_box = next((it["box"] for it in band
                      if re.search(r"\d\s*-\s*\d", it["text"])), None)  # 条目可能带图标噪声前缀, 如 '总 2-6'
    if stage_box is None:
        return None
    cands = [it for it in band
             if re.fullmatch(r"\d{1,3}", it["text"].strip())
             and (it["box"][0] + it["box"][2]) / 2 > stage_box[2] + 150]
    return int(cands[0]["text"]) if len(cands) == 1 else None


def extract_round_time(items, img) -> int | None:
    """该回合剩余时间(秒), 追踪器图标行最右端的数字, 如 20.

    个位小数字整图 OCR 易漏检, 裁剪放大兜底 (与按钮费用同策略);
    顶栏漂移导致固定区域读空时, 再以回合号实际位置为锚兜底.
    """
    v = _to_int(_join(_region_items(items, regions.ROUND_TIME)))
    if v is None:
        picked = ocr.recognize_region(img, regions.ROUND_TIME, upscale=4)
        v = _to_int(_join(picked))
    return v if v is not None else round_time_by_anchor(img)


def extract_level(items) -> int | None:
    """等级."""
    return _to_int(_join(_region_items(items, regions.LEVEL)))


def in_game_features(img, items: list[dict] | None = None) -> dict:
    """**在局特征快判** (用户 2026-10-15): 一帧里查"回合号 / 等级 / 玩家血量", 任一命中即在局.

    用途: 采集脚本**首轮** (本进程还没开过局) 阶段读成 `unknown` 时, 先用同一帧做一次性硬判定 ——
    命中即认定"这一帧确实在局内", 于是只读重试、**绝不按离局处理** (避免把正在打的这一局顶掉);
    三项全读不出才落回首轮的短宽限, 快速判离局并走自动开局。

    为什么这三样可信 (大厅/桌面 vs 对局内):
        * **回合号** `N-N` (regions.STAGE + 顶栏漂移横带): 只在对局顶栏出现;
        * **等级** (regions.LEVEL, 形如 "4级"): 对局左下常驻, 大厅没有这个控件;
        * **玩家血量** (regions.PLAYERS 里自己那一行的血量): 对局右侧玩家列表, 大厅没有。

    `items` 传本帧已有的整图 OCR 结果则零额外开销; 不传则这里自己跑一次整图 OCR (约 2s)。
    三项都读不出 **不等于**"不在对局"(也可能只是 OCR 抖了一下), 所以这里只用于"命中即锁定在局"。
    """
    if items is None:
        items = ocr.recognize(img)
    stage = extract_stage(items)
    level = extract_level(items)
    players = _parse_players(items)
    idx = own_row_index(players)
    hp = players[idx].get("hp") if idx is not None else None
    hits = [f"{k}={v}" for k, v in (("回合号", stage), ("等级", level), ("血量", hp))
            if v is not None]
    return {"hit": bool(hits), "why": " ".join(hits),
            "stage": stage, "level": level, "hp": hp}


def extract_xp(items) -> list | None:
    """经验条 [当前, 升到下一级所需], 如 [10, 18] (等级旁 "10/18" 进度数字)."""
    m = re.search(r"(\d+)\s*/\s*(\d+)",
                  _join(_region_items(items, regions.XP)))
    return [int(m.group(1)), int(m.group(2))] if m else None


_GOLD_ICON_TPL = None  # 金币图标灰度模板懒加载缓存 (recognize/templates/gold_icon.png)


def extract_gold(img) -> int | None:
    """金币数量. 模板匹配定位金币图标 → 图标右侧数字槽放大识别 (upscale=2).

    教训链: 固定数字槽左缘 938 会包住金币图标 (实测核心 x952-966, 淡金色
    (198,174,100)), OCR 会把它读成形似 '9' 的字形且同属一个条目无法后滤
    ('17'→'917' / '2'→'922', 时灵时不灵全看服务状态); 图标右缘与数字 '1' 字形
    仅隔 ~2px 且帧间有 ±6px 抖动 (左缘 966 对 / 970 切 '1'), 固定左缘无安全位
    → 按图标模板匹配动态定位, 数字槽 = 图标核心右缘 +2 起到 1002; 未命中
    (阈值 0.7) 回退 regions.GOLD 固定槽.
    """
    return _to_int(_join(_timed("ocr.recognize_region(gold)",
                                ocr.recognize_region, img,
                                _gold_slot(img) or regions.GOLD, 2)))


def _gold_slot(img) -> tuple | None:
    """灰度模板匹配定位金币图标, 返回其右侧数字槽 (图标右缘+2, 801, 1002, 855).

    模板裁剪自带 2px 外扩, 故 loc + 模板宽 = 图标核心右缘 + 2, 恰为数字槽左缘.
    未命中 (score < 0.7) / 无模板文件返回 None.
    """
    global _GOLD_ICON_TPL
    import cv2
    if _GOLD_ICON_TPL is None:
        path = default_config.PROJECT_ROOT / "recognize" / "templates" / "gold_icon.png"
        _GOLD_ICON_TPL = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if _GOLD_ICON_TPL is None:
        return None
    x1, y1, x2, y2 = regions.GOLD_BADGE
    g = cv2.cvtColor(np.asarray(img.convert("RGB"))[y1:y2, x1:x2], cv2.COLOR_RGB2GRAY)
    if _GOLD_ICON_TPL.shape[0] > g.shape[0] or _GOLD_ICON_TPL.shape[1] > g.shape[1]:
        return None
    _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(g, _GOLD_ICON_TPL, cv2.TM_CCOEFF_NORMED))
    if score < 0.7:
        return None
    return (x1 + loc[0] + _GOLD_ICON_TPL.shape[1], 801, 1002, 855)


def extract_xp_buy_cost(img) -> int | None:
    """购买经验按钮费用, 如 4; 金色小字整图 OCR 易漏检, 裁剪放大识别."""
    return _button_cost(img, regions.XP_BUY_COST)


def extract_refresh_cost(img) -> int | None:
    """刷新按钮费用, 如 0/2 (有免费刷新时显示 0); 裁剪放大识别."""
    return _button_cost(img, regions.REFRESH_COST)


def extract_button_costs(img) -> dict:
    """购买经验+刷新两个相邻按钮费用: 合并为一条横带一次 OCR, 按 box 分派 (省 1 次调用)."""
    a, b = regions.XP_BUY_COST, regions.REFRESH_COST
    band = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
    items = ocr.recognize_region(img, band, upscale=4)

    def pick(r):
        got = [it for it in items
               if r[0] <= (it["box"][0] + it["box"][2]) / 2 <= r[2]
               and r[1] <= (it["box"][1] + it["box"][3]) / 2 <= r[3]]
        digits = re.findall(r"\d", _join(got))
        return int(digits[-1]) if digits else None

    return {"xp_buy_cost": pick(a), "refresh_cost": pick(b)}


def extract_free_refresh(img) -> int:
    """免费刷新次数 (刷新按钮右上角徽章); 徽章小, 裁剪放大识别, 无徽章时为 0."""
    items = ocr.recognize_region(img, regions.FREE_REFRESH, upscale=4)
    return _to_int(_join(items)) or 0


def _button_cost(img, region) -> int | None:
    """按钮区域费用数字: 裁剪放大 OCR 后取最后一个数字 (金币图标常被粘连成 "。"/"0", 如 "02"=2)."""
    items = ocr.recognize_region(img, region, upscale=4)
    digits = re.findall(r"\d", _join(items))
    return int(digits[-1]) if digits else None


def extract_traits(items) -> list[dict]:
    """左侧羁绊列表: 以 "x/y(/z)" 阈值行锚定条目, 上方为名称, 左侧短数字为上阵数量."""
    items = _region_items(items, regions.TRAITS)
    traits = []
    for it in items:
        text = it["text"].replace(" ", "")
        if not re.fullmatch(r"\d+/\d+(?:/\d+)?", text):
            continue
        x1, y1 = it["box"][0], it["box"][1]
        name, name_bottom, count = None, -1, None
        for other in items:
            if other is it:
                continue
            otext = other["text"].replace(" ", "")
            if re.fullmatch(r"\d+/\d+(?:/\d+)?", otext):
                continue
            ox1, oy1, ox2, oy2 = other["box"]
            if otext.isdigit() and ox2 <= x1 + 4 and -14 <= y1 - oy2 <= 16:
                count = int(otext)  # 阈值行左侧的数字
            elif oy1 < y1 and -8 <= y1 - oy2 <= 20 and not otext.isdigit():
                if oy2 > name_bottom:  # 阈值行正上方的名称
                    name, name_bottom = other["text"], oy2
        traits.append({
            "count": count,
            "name": name,
            "threshold": [int(n) for n in text.split("/")],
        })
    return traits


# 血量文本里的图标噪声: (交战图标) (本回合交战标记) 紧贴血量数字左侧, OCR 把它读成 X/x/Z 等字符
# 并与数字粘成一个条目 (实测 'X100' 'x52' 'Z98' 'X64'). 清理规则见 _parse_players ——
# 只在**结构上的血量位** (上一项刚读到名字) 才剥掉数字前最多 2 个非数字字符:
# 自己行的假名 ('X7', 数字大字号左移) 出现在名字位, 绝不能被当成血量, 否则一行会被拆成两行。
_HP_ICON_PREFIX = re.compile(r"^\D{1,2}")
# 血量位常见 OCR 数字混淆 (实测: 最后一行的 "0" 被读成字母 "O" → 该行收不到血量 → 整行丢失)。
# 只在**血量位** (上一项刚读到名字) 才做这个替换, 不会污染名字/其它文本。
_HP_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "Q": "0", "D": "0", "I": "1", "l": "1", "i": "1",
                               "|": "1", "Z": "2", "z": "2", "S": "5", "s": "5", "B": "8",
                               "g": "9", "q": "9"})


def _hp_slot_text(text: str) -> str:
    """血量位文本清洗: 先修数字混淆, 再剥掉 (交战图标) 等图标前缀 (最多 2 个非数字字符).

    顺序很关键: 若先剥前缀, "O" 会被当成图标整个剥掉 (^\D{1,2} 命中) → 血量永久读不到。
    """
    return _HP_ICON_PREFIX.sub("", text.translate(_HP_DIGIT_FIX))


def _hp_height(players: list[dict]) -> list[float]:
    """每行血量数字框的高度 (拿不到就 0)."""
    out: list[float] = []
    for p in players:
        try:
            out.append(max(0.0, float(p.get("hp_h") or 0.0)))
        except Exception:         # noqa: BLE001
            out.append(0.0)
    return out


def _own_row_by_hp_height(players: list[dict]) -> int | None:
    """自己行判据之一: 血量数字**字号高** (自己行显著大于其他玩家).

    实测 37 个实机失败帧 (用户 m05143-2 的 `失败案例/*/reject.png`): 自己行血量框高
    30~36px, 其他行 20~29px; 判据 "最大且唯一, 且至少比中位数高 margin" 命中 25/37、
    **错行 0**; 把 margin 降到 2/3 以下会开始判错 (`1022_140852`), 所以宁可 None 不猜。

    **淘汰行的 "-99" 框高异常放大不参与竞争** (2026-11-05 第 7 局 P0): 已淘汰玩家显示
    "-99" 这类负血量, 负号把 OCR 框撑到 43~47px (实测 h=46.5), 比存活行 (含自己行 31.5)
    都高 → 抢走"最高且唯一"判据 → `game_over._by_players` 把该淘汰行当成我、读到 hp<=0 →
    **正在打的对局被误判结束** (那局 4-4 局中误退、名次表按存活血量排成"假吃鸡"、
    下一局还接管了残局)。判据是"自己行血量字号最大", 而淘汰行的框高是**负号造成的假字号**,
    不是自己行的特征 → 框高 > PLAYERS_OWN_ROW_HP_H_MAX 的行一律剔除; 剔除后判不出就 None
    (由调用方降级), 不猜。
    **注意不能按 hp<0 排除**: 自己出局时血量同样是负数 (如 -6), 那样会连"我出局了"都判不出
    (test_partial_player_list_still_detects_death 回归)。
    """
    hs = _hp_height(players)
    if not hs or max(hs) <= 0:
        return None
    margin = float(getattr(default_config, "PLAYERS_OWN_ROW_HP_H_MARGIN", 3.0) or 0.0)
    min_h = float(getattr(default_config, "PLAYERS_OWN_ROW_HP_H_MIN", 29.0) or 0.0)
    max_h = float(getattr(default_config, "PLAYERS_OWN_ROW_HP_H_MAX", 40.0) or 0.0)
    # 候选 = 框高落在 [min_h, max_h] 之间的行。
    # 下限: 自己行血量一定是**大字号** (实测 30~36, 其他 20~29); 整行都小的帧全被排除 → None。
    # 上限: 淘汰行的 "-99" 被负号撑到 43~47px, 是**假字号**, 剔除 (见 docstring 的 P0 说明)。
    cand = [i for i, h in enumerate(hs)
            if h > 0 and h >= min_h and (max_h <= 0 or h <= max_h)]
    if not cand:
        return None
    top = max(hs[i] for i in cand)
    peaks = [i for i in cand if hs[i] >= top - 0.5]
    if len(peaks) != 1:
        return None
    rest = sorted(hs[i] for i in cand if i != peaks[0])
    if not rest:
        # 只有一行有字号数据时没有中位数可比 —— 这一行就是唯一候选 (通常是残缺面板,
        # 如 `in_game_features` 只从整图 OCR 里读到 1 行)。已有绝对上下限兜着, 不会把
        # 整行都小 / 框高异常的帧误判成自己行。
        return peaks[0]
    med = rest[len(rest) // 2] if len(rest) % 2 else (rest[len(rest) // 2 - 1] + rest[len(rest) // 2]) / 2
    return peaks[0] if top >= med + margin else None


def own_row_index(players: list[dict]) -> int | None:
    """返回"自己行"在 players 中的下标; 无法唯一确定时返回 None (调用方不得猜测).

    自己行最稳的两个几何特征 (**用户 m05143-2 实测 37 帧失败案例后重写**):
      1. **血量数字字号更大**: 自己行血量框高 30~36px, 其他行 20~29px —— 单判据零错行;
      2. **数字右缘左移**: 自己行 1774~1798, 其他行 1819~1828 (旧判据)。
    旧实现只用第 2 条, 实测 37 帧里只命中 2 帧 (多数帧有 2 行以上右缘 < 1810:
    自己行 + 一行名字短的人) → `len(shifted)==1` 不成立 → 返回 None → 观战轮询报
    "玩家列表中识别自己血量和位置失败" (用户 m05143-2)。现在顺序改为:
      - 已标注 "my" 的行直接返回 (重入幂等; 多个 "my" 视为异常);
      - **血量框最高且唯一** → 自己行 (零错行);
      - 恰一个"数字左移"的行 → 自己行 (兜底, 保留旧能力);
      - 退回"恰一个无名行"(兼容旧行为);
      - 都判不出 → None (不猜);
      - "已造成伤害"视图 (标题被读成一行名字, 且伤害数字全部左移) 直接返回 None。
    """
    if not players:
        return None
    for p in players:  # 伤害视图守卫: 标题会被 OCR 读成一行"名字"
        name = str(p.get("name") or "")
        if "已造成伤害" in name or name.startswith("已造成"):
            return None
    marked = [i for i, p in enumerate(players) if p.get("name") == "my"]
    if marked:
        return marked[0] if len(marked) == 1 else None
    # **有字号数据就以它为准, 判不出就 None, 不再退回"数字左移"去猜** ——
    # 实测 (37 帧) 退回 x2 会判错 7 帧 (都是"全行字号相近 + 另有名字短的人"的帧),
    # 而错标自己行的后果是"把别人的血量当成我的" (AGENTS §1: 宁可漏采不可采错)。
    if any(float(p.get("hp_h") or 0.0) > 0 for p in players):
        return _own_row_by_hp_height(players)
    limit = default_config.PLAYERS_OWN_ROW_X2_MAX
    shifted = [i for i, p in enumerate(players)
               if (p.get("rect") or (0, 0, 0, 0))[2] < limit]
    if len(shifted) == 1:
        return shifted[0]
    unnamed = [i for i, p in enumerate(players) if not (p.get("name") or "").strip()]
    return unnamed[0] if len(unnamed) == 1 else None


# 玩家行不足的提示**同类只打一次** (用户口径: 每次 poll 都刷同一行, 把日志淹了):
# 内容 (缺几行 / 我的血量) 变了才再打一次, 期间被抑制的次数一并报出来 —— 不刷屏也不丢信息。
_PLAYER_WARN_KEY = None
_PLAYER_WARN_SUPPRESSED = 0


def _warn_player_rows(got: int, expect: int, my_hp) -> None:
    """玩家行提取不足 → 降级通过; 提示同类只打印一次 (重复只计数)。"""
    global _PLAYER_WARN_KEY, _PLAYER_WARN_SUPPRESSED
    key = (got, expect, my_hp)
    if key == _PLAYER_WARN_KEY:
        _PLAYER_WARN_SUPPRESSED += 1
        return
    extra = f" (上次同类提示已抑制 {_PLAYER_WARN_SUPPRESSED} 次)" if _PLAYER_WARN_SUPPRESSED else ""
    _PLAYER_WARN_KEY, _PLAYER_WARN_SUPPRESSED = key, 0
    print(f"[错误] 玩家行提取不足: {got}/{expect}, 但已识别到我的血量 ({my_hp}) → 本次降级通过。"
          f"此处识别确有问题 (缺行多为淘汰玩家整行变暗/被特效遮挡), 请排查右侧玩家列表{extra}",
          flush=True)


def extract_players(items, img=None) -> list[dict]:
    """右侧玩家列表: 纯数字短文本为血量, 其上最近的其它文本为玩家名.

    传 img 时, 整图解析不足 PLAYERS_EXPECT_COUNT 行则对 regions.PLAYERS 做
    2x 裁剪放大重读兜底 (hp=0 淘汰玩家整行变暗, 整图 OCR 常漏检半数行, 实测 4/8;
    区域放大可读全, 且与 game_over._by_players 同 region+upscale 共享缓存).

    行数仍不足时的判据 (**降级通过**): 只要**自己的血量**能识别出来就照常返回这份残缺数据,
    同时打 `[错误]` 提醒开发者"此处识别确有问题, 请排查" —— 因为缺行只影响对手血量这一项
    锦上添花的信息, 不该连累同一批提取里的金币/等级/回合/羁绊/商店 (它们同属一次整图调用,
    抛异常会让整组键全部丢失)。
    只有连自己血量都取不到 (自己行无法唯一判定, 含面板切到"已造成伤害"视图) 才抛错,
    严禁静默交出连自己都认不出的数据。

    rect 为玩家行包围盒 (名字框与血量框的并集, 名字缺失时为血量框), 供自动化定位.
    """
    expect = default_config.PLAYERS_EXPECT_COUNT
    players = _parse_players(items)
    if img is not None and len(players) < expect:
        players = _parse_players(
            ocr.recognize_region(img, regions.PLAYERS, upscale=2))
    # 自己行标注: 判据集中在 own_row_index (无名字 + 数字大字号左移). 标注后 debug 详情/prompt
    # 统一显示 "my"; 无法唯一确定时不标注 (prompt 侧显示 ?, 不做猜测)
    idx = own_row_index(players)
    if idx is not None:
        players[idx]["name"] = "my"
    if len(players) < expect:
        my_hp = players[idx].get("hp") if idx is not None else None
        if my_hp is None:  # 连自己血量都取不到 → 这份数据不可用, 必须显式失败
            why = ("自己行无法唯一判定 (面板可能切到'已造成伤害'视图或被遮挡)"
                   if idx is None else "我的血量读不到")
            raise ValueError(f"玩家行提取不足: {len(players)}/{expect} 且{why}"
                             " (识别质量差或面板被遮挡/切换)")
        _warn_player_rows(len(players), expect, my_hp)
    return players


def _hp_from_text(text: str, raw: str) -> int | None:
    """把候选血量文本转成 int; 不是纯 1~3 位数字则返回 None (调用方决定是否再清洗重试).

    raw 为未经清洗的原始文本, 用于判致命一击负号 ("-6" 保留为 -6)。
    粘连多读一位时只取末两位 (如 "82"→"782" 取 82): 这既覆盖血量与头像特效粘连,
    也避免把 "2445"(已造成伤害视图的伤害值) 这类长数字当成血量。
    """
    # 允许**负血量**: 玩家被打到 0 以下时右侧列表显示 "-10" (常与 (交战图标) 交战标记粘连成 "x -10")。
    # 实测漏检 (run_1010__042635 的 56 帧里 17 帧只解析出 7/8): 最后一行的血量是 "x -10",
    # 剥掉图标前缀后得到 "-10" → 旧正则只认纯数字 → 该行收不到血量 → **整行丢失**。
    if not re.fullmatch(r"-?\d{1,3}", text):
        return None
    negative = text.startswith("-") or "-" in raw
    hp = abs(int(text))
    if hp > 130:
        hp = int(str(hp)[-2:])
    return -hp if negative else hp


def _parse_players(items) -> list[dict]:
    """玩家行解析主体: 遍历 PLAYERS 区域内条目归组 (数字条目收行, 其上文本为名)."""
    players, name, name_box = [], None, None
    for it in _region_items(items, regions.PLAYERS):
        raw = it["text"]
        text = raw.strip(" .,*·\u2022○●①-—~+＋")
        hp = _hp_from_text(text, raw)
        if hp is None and name is not None:
            # 结构上的"血量位": 上一项刚读到名字, 说明正等这一行的血量。
            # 此处允许剥掉数字前最多 2 个非数字字符 —— 即 (交战图标) 被 OCR 读成 X/x/Z/× 后
            # 与血量粘连的情况 ('X100'→100, 'x52'→52, 'Z98'→98; 715 帧抽样 18 帧中 6 帧命中,
            # 不清洗会让该条目落到名字分支 → 覆盖上一行玩家名 → 整行丢失 → 玩家行 7/8)。
            # 加"上一项是名字"这个前提是必须的: 自己行的假名 ('X7') 也长这样, 但它出现在
            # **名字位** (上一项已收行, name 已清空), 无条件剥字符会把它当成血量行, 于是一行
            # 被拆成两行、自己行也随之认错 (实测 bug, 见 test_extract_players_overwrites_bogus_self_name)。
            hp = _hp_from_text(_hp_slot_text(text), raw)
        if hp is not None:
            rect = _union_box(it["box"], name_box) if name_box else it["box"]
            # `hp_h` = 血量数字框的**高度**: 自己行的血量是大字号 (实测 30~36px, 其他行 20~29px),
            # 这是 `own_row_index` 判自己行的主特征 (用户 m05143-2 实测 37 帧: 零错行)。
            # `hp_box` 保留血量框本身, 供调试/未来判据用 (rect 是"名字框 ∪ 血量框", 会混)。
            _hb = it.get("box") or (0, 0, 0, 0)
            try:
                _hh = float(_hb[3]) - float(_hb[1])
            except Exception:     # noqa: BLE001
                _hh = 0.0
            players.append({"name": name, "hp": hp, "rect": rect,
                            "hp_h": max(0.0, _hh), "hp_box": tuple(_hb)})
            name, name_box = None, None
        else:
            name, name_box = raw, it["box"]
    return players


def extract_shop(items, img=None) -> list[dict]:
    """商店 5 张卡牌: 按槽位归组, 下行为名称/价格, 上行为羁绊标签.

    传 img 时对价格行做两段半带裁剪放大 OCR (小号金字整图易漏检),
    裁剪结果优先, 整图结果兜底.

    仙灵法杖卡 (无羁绊标签, 卡身为效果长句): effect 字段由名称查爬虫库
    (units/db.find_charm) 得到权威文本, 名称未读出时以卡身 OCR 长句兜底.
    所有卡牌按名称查爬虫库打 type 标记; 奕子附 chess_id/db_price/races/jobs/attack_range.

    rect 为卡牌槽位整体方框 (槽位左边界+宽度, SHOP 上下沿), 恒定填充, 供自动化点击.
    """
    _, sy1, _, sy2 = regions.SHOP
    slots = [{"name": None, "price": None, "traits": [], "effect": None, "type": None,
              "chess_id": None, "db_price": None, "races": [], "jobs": [], "attack_range": None,
              "rect": (left, sy1, left + regions.SHOP_SLOT_WIDTH, sy2)}
             for left in regions.SHOP_SLOT_LEFTS]
    for it in _region_items(items, regions.SHOP):
        box = it["box"]
        cx = (box[0] + box[2]) / 2
        cy = (box[1] + box[3]) / 2
        text = it["text"].strip()
        if cy >= regions.SHOP_NAME_Y and text.isdigit():  # 价格: 常悬挂在槽右缘外 4~23px, 先按右缘归属 (槽界归组容差 ±6 兜不住)
            digits = re.findall(r"\d", text)
            idx = _price_slot(cx)
            if digits and idx is not None:
                slots[idx]["price"] = int(digits[-1])
            continue
        idx = _slot_index(cx)
        if idx is None:
            continue
        if cy >= regions.SHOP_NAME_Y:  # 名称行
            if slots[idx]["name"] is None:
                slots[idx]["name"] = text
        elif len(text) <= 8 and not any(ch in text for ch in "，。！？"):
            slots[idx]["traits"].append(text)
    if img is not None and slots and any(s["name"] or s["traits"] for s in slots):
        # 按需兜底: 整图读全时零额外开销, 缺哪槽补哪槽; 商店未渲染/被遮罩 (无任何名称或羁绊) 时跳过,
        # 避免空商店 5 价格+5 名称共 10 次兜底 OCR 白跑 (实测 ~12s)
        # 价格缺失的槽: 4x 裁剪 OCR —— **相邻缺价槽合并成一次裁剪** (用户 m00002 提速;
        # 实测 frame_A_initial.png: 逐槽 2 次 0.58s -> 合并 1 次 0.24s, 且读到的价格一字不差)
        for idx, price in _slot_prices(img, [i for i, s in enumerate(slots) if s["price"] is None]).items():
            slots[idx]["price"] = price
        for s in slots:  # 名称缺失的槽: 名称行 2x 裁剪 OCR (单字卡名如 [蔚] 整图易漏检)
            if s["name"] is None:
                s["name"] = _slot_name(img, s["rect"])
    for s in slots:  # 名称查爬虫库: 仙灵法杖得权威效果文本, 奕子得 ID/库价/羁绊/职业/攻距
        charm = unit_db.find_charm(s["name"] or "")
        if charm:
            s["type"] = "charm"
            s["effect"] = _strip_tags(charm["effect"])
        elif s["name"]:
            unit = unit_db.find_unit(s["name"])
            if unit:
                s["type"] = "unit"
                s["chess_id"] = unit["chessId"]
                s["db_price"] = unit.get("price")
                s["races"] = list(unit.get("races") or [])
                s["jobs"] = list(unit.get("jobs") or [])
                s["attack_range"] = unit_db.unit_attack_range(unit)
        if s["effect"] is None:  # 效果未知: 卡身长句 OCR 兜底 (奕子卡羁绊标签是短词无标点, 不会误入)
            body = [it for it in _region_items(
                        items, (s["rect"][0], sy1, s["rect"][2], regions.SHOP_NAME_Y))
                    if any(ch in it["text"] for ch in "，。！？；：、")]
            if body:
                s["effect"] = _join(sorted(body, key=lambda it: (it["box"][1], it["box"][0])))
    return slots


_NAME_PAD_TOP = 7      # 名称行裁剪上余量 (px): 名称底边常略超 SHOP_NAME_Y, 切太紧会切掉字顶部
_NAME_PAD_BOTTOM = 12  # 名称行裁剪下余量 (px): 名称底边常略超 SHOP[3], 切太紧会切掉字底部 (丢字主因)


def _slot_name_fast(img, rect: tuple, *, pad: int = 30) -> str | None:
    """卡槽名称**快速**识别: 裁名称行小图 -> 放大2x -> `ocr.recognize_line` (跳过 det, 只识别).

    与 `_slot_name` 的区别只在 OCR 通道与实现:
      * `_slot_name` 走 `ocr.recognize_region` (通用 OCR **含文字检测 det**, 实测 ~390ms);
      * 本函数裁同一区域 (裁剪口径完全一致: (x1-20, SHOP_NAME_Y, x2+pad, y2)) + 放大 2x,
        再走 `ocr.recognize_line` (use_det=False, use_cls=False), 只识别不检测。
    商店格子位置固定, det 检测这一步本就多余; 名称是小字, 放大 2x 保证识别率 (与原口径一致)。
    拼接/过滤口径 (含中文条目) 与 `_slot_name` 保持一致。
    """
    from PIL import Image as _Image
    x1, _, x2, y2 = rect
    if not hasattr(img, "crop"):
        return None
    # 名称行裁剪: 上下各留余量 (名称为小字, 底边常略超 SHOP_NAME_Y~y2, 切太紧会把字底部切掉丢字)。
    # 上抬 _NAME_PAD_TOP, 下扩 _NAME_PAD_BOTTOM。
    band = img.crop((x1 - 20, regions.SHOP_NAME_Y - _NAME_PAD_TOP, x2 + pad, y2 + _NAME_PAD_BOTTOM))
    # 放大 2x (名称行小图, 成本极低; 小字不放大易丢字/截断)
    w, h = band.size
    if w > 0 and h > 0:
        band = band.resize((w * 2, h * 2), _Image.LANCZOS)
    text, score = ocr.recognize_line(band)
    text = (text or "").strip()
    # 名称必须是含中文的条目; 纯数字(价格)/图标噪声不要
    if not text or not re.search(r"[\u4e00-\u9fff]", text):
        return None
    return text


def extract_shop_fast(img) -> list[dict]:
    """商店 5 张卡牌的**快速**识别 (固定格裁名称行 + 免 det 识别 + 查库属性).

    为什么更快 (2026-11-12 用户要求): 原 `extract_shop` 先对整条商店窄带跑通用 OCR(det+rec,
    ~390ms) 再逐槽补名; 而 5 格位置固定 (regions.SHOP_SLOT_LEFTS)、字体固定, det 检测完全多余。
    本函数按固定格裁名称行 -> `recognize_line` 只识别不检测, 5 格各一次, 大幅降耗时。

    返回结构与 `extract_shop` 兼容 (name/price/traits/effect/type/chess_id/db_price/races/jobs/
    attack_range/rect)。**只负责卡名与查库属性**: price 由上层（需要时）走 `_slot_prices` 兜底;
    名称读不到的槽保持 None (不猜, 由调用方决定是否回落原 `extract_shop`)。
    """
    _, sy1, _, sy2 = regions.SHOP
    slots = [{"name": None, "price": None, "traits": [], "effect": None, "type": None,
              "chess_id": None, "db_price": None, "races": [], "jobs": [], "attack_range": None,
              "rect": (left, sy1, left + regions.SHOP_SLOT_WIDTH, sy2)}
             for left in regions.SHOP_SLOT_LEFTS]
    for i, s in enumerate(slots):
        s["name"] = _slot_name_fast(img, s["rect"])
    # 名称查爬虫库: 与 extract_shop 同口径 (仙灵法杖得 effect, 奕子得 ID/库价/羁绊/职业/攻距)
    for s in slots:
        charm = unit_db.find_charm(s["name"] or "")
        if charm:
            s["type"] = "charm"
            s["effect"] = _strip_tags(charm["effect"])
        elif s["name"]:
            unit = unit_db.find_unit(s["name"])
            if unit:
                s["type"] = "unit"
                s["chess_id"] = unit["chessId"]
                s["db_price"] = unit.get("price")
                s["races"] = list(unit.get("races") or [])
                s["jobs"] = list(unit.get("jobs") or [])
                s["attack_range"] = unit_db.unit_attack_range(unit)
    return slots


# ---------- 商店图形学加速识别 (SHOP_GEO_RECOGNIZE) ----------
# 用户要求 (2026-11-12): 打掉 det 这个唯一慢点。用"白字图形学"在固定槽内求文字精确 rect,
# 逐格裁出 -> 2x -> ocr.recognize_line (免 det) -> 查库; 形近字做轻校验 (B)。
# 铁律: 从不猜内容; 只有读出的名字不在卡池时才做"同字数唯一形近候选"校正, 合法名绝不动。

def _shop_white_mask(arr, thresh: int = 150):
    """白字掩码: 每像素 min 通道 > thresh 且 max-min < 50 (排除彩色立绘/金色, 只留白字)。"""
    a = arr.astype(np.int16)
    mn = a.min(axis=2)
    mx = a.max(axis=2)
    return (mn > thresh) & ((mx - mn) < 50)


def _shop_text_rect_in_slot(arr, slot):
    """在固定槽内用图形学求"名字文字"的精确 rect (相对整图)。

    规则: 取槽内白字像素; 从最左白列起按列扫描, 遇 >=10px 的大间隙即停 (排除右侧金币数字);
    行方向取白字的 min/max。返回 (x0,y0,x1,y1) 或 None (无可裁文字)。
    """
    x0, y0, x1, y1 = slot
    # 健壮性 (单测用 "IMG" 这类假图会得到 0 维数组): 非图像直接返回 None, 让上层回落原路径,
    # 加速路径绝不允许因输入异常崩掉主链路。
    if arr is None or arr.ndim < 2:
        return None
    sub = arr[y0:y1, x0:x1]
    if sub.size == 0:
        return None
    m = _shop_white_mask(sub)
    cols = np.where(m.any(axis=0))[0]
    rows = np.where(m.any(axis=1))[0]
    if not len(cols) or not len(rows):
        return None
    # 左起第一段: 遇大间隙停止, 排除右侧金币/价格
    first = int(cols[0])
    end = int(cols[-1])
    prev = first
    for c in cols:
        c = int(c)
        if c - prev >= 10:
            end = prev
            break
        prev = c
    tx0, tx1 = x0 + first, x0 + end + 1
    ty0, ty1 = y0 + int(rows.min()), y0 + int(rows.max()) + 1
    if tx1 - tx0 < int(getattr(default_config, "SHOP_GEO_MIN_TEXT_W", 8)):
        return None
    return (tx0, ty0, tx1, ty1)


def _shop_geo_read_name(img, slot):
    """图形学路径读单槽名称: 求文字 rect -> 裁出 -> 白二值化 -> 2x -> 免det 识别 -> 清洗。"""
    arr = np.asarray(img.convert("RGB") if hasattr(img, "convert") else img)
    rect = _shop_text_rect_in_slot(arr, slot)
    if rect is None:
        return None
    tx0, ty0, tx1, ty1 = rect
    crop = img.crop((tx0 - 3, ty0 - 3, tx1 + 3, ty1 + 3))
    ca = np.asarray(crop.convert("RGB"))
    m = _shop_white_mask(ca)
    out = np.zeros((*m.shape, 3), dtype="uint8")
    out[m] = 255
    bim = Image.fromarray(out)
    w, h = bim.size
    if w > 0 and h > 0:
        bim = bim.resize((w * 2, h * 2), Image.LANCZOS)
    text, _score = ocr.recognize_line(bim)
    text = (text or "").strip()
    return text or None


def _shop_geo_correct(name):
    """形近字轻校验 (B): 仅当 name 不在卡池时, 在**同字数**卡池名里找**唯一**形近候选校正。

    - 合法名 (unit/charm 精确或别名命中) 直接返回, 绝不动;
    - name 为空 -> None;
    - 长度约束: 只和同字数卡池名比 (OCR 形近错字不改变字数);
    - 用 difflib 求最相似且相似度 >= FUZZY 阈值; 若最优唯一 (无并列) 则校正, 否则保持原样。
    返回 (corrected_name, was_corrected)。
    """
    if not name:
        return None, False
    try:
        data = unit_db.load_db()
    except Exception:
        return name, False
    # 合法性判断必须用**精确**键比对: `unit_db.find_unit` 内部走 `difflib.get_close_matches(cutoff=0.6)`,
    # 是模糊的 —— 用它判"合法"会让"芸阿螂"这种形近错字直接被当成合法名放过 (实测 2026-11-19)。
    if name in data["unit_by_name"] or name in data["charm_by_name"]:
        return name, False  # 精确合法, 不动
    pool = list(data["unit_by_name"].keys()) + list(data["charm_by_name"].keys())
    same_len = [p for p in pool if len(p) == len(name) and p != name]
    if not same_len:
        return name, False
    import difflib
    scored = sorted(
        ((difflib.SequenceMatcher(None, name, p).ratio(), p) for p in same_len),
        reverse=True,
    )
    if not scored:
        return name, False
    best_ratio, best = scored[0]
    # 并列检测: 第二名的相似度与最优相差 < 1e-6 视为多候选 -> 不校正
    if len(scored) > 1 and abs(scored[1][0] - best_ratio) < 1e-6:
        return name, False
    if best_ratio >= unit_db.FUZZY_THRESHOLD:
        return best, True
    return name, False


def extract_shop_geo(img) -> list[dict]:
    """商店 5 卡**图形学加速**识别 (固定槽 + 图形学定文字 rect + 免 det rec + 形近字轻校验)。

    与 `extract_shop` / `extract_shop_fast` 返回结构完全兼容。相比 `extract_shop_fast`:
      * 用 `_shop_text_rect_in_slot` 图形学**精确定位**每格文字区 (而非固定 pad 裁剪), 天然排除右侧金币;
      * 白二值化 + 2x 提升识别率;
      * 读出名字后 `_shop_geo_correct` 做形近字轻校验 (B)。

    坐标来自 `default_config.SHOP_GEO_SLOT_RECTS` (用户 labelme 人工标定的 5 槽紧框)。
    **不猜**: 读不出就是 None; 校正只做"同字数唯一形近", 合法名不动。调用方按 None 决定是否回落原路径。
    """
    _, sy1, _, sy2 = regions.SHOP
    rects = getattr(default_config, "SHOP_GEO_SLOT_RECTS", None)
    slots = [{"name": None, "price": None, "traits": [], "effect": None, "type": None,
              "chess_id": None, "db_price": None, "races": [], "jobs": [], "attack_range": None,
              "rect": (regions.SHOP_SLOT_LEFTS[i], sy1,
                       regions.SHOP_SLOT_LEFTS[i] + regions.SHOP_SLOT_WIDTH, sy2),
              "raw_name": None, "corrected": False}
             for i in range(len(regions.SHOP_SLOT_LEFTS))]
    for i, s in enumerate(slots):
        slot_rect = rects[i] if rects and i < len(rects) else None
        if slot_rect is None:
            continue
        try:
            raw = _shop_geo_read_name(img, slot_rect)
        except Exception:
            # 加速路径任何异常都吞掉 -> 该槽 None -> 上层回落原 extract_shop 兜底 (数据正确性优先)
            raw = None
        s["raw_name"] = raw
        if not raw:
            continue
        fixed, was = _shop_geo_correct(raw)
        s["name"] = fixed
        s["corrected"] = was
    # 查库属性 (与 extract_shop 同口径)
    for s in slots:
        charm = unit_db.find_charm(s["name"] or "")
        if charm:
            s["type"] = "charm"
            s["effect"] = _strip_tags(charm["effect"])
        elif s["name"]:
            unit = unit_db.find_unit(s["name"])
            if unit:
                s["type"] = "unit"
                s["chess_id"] = unit["chessId"]
                s["db_price"] = unit.get("price")
                s["races"] = list(unit.get("races") or [])
                s["jobs"] = list(unit.get("jobs") or [])
                s["attack_range"] = unit_db.unit_attack_range(unit)
    return slots


def _strip_tags(text: str) -> str:
    """去除爬取效果文本中的 HTML 标签 (<br>/<div> 等)."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _price_blocks(idxs: list[int]) -> list[list[int]]:
    """把缺价槽索引切成"连续段": 相邻槽合为一块 (一块只付一次裁剪 OCR), 跳号则断开.

    例: [0, 1] -> [[0, 1]]; [0, 2] -> [[0], [2]]; [1, 2, 3, 5] -> [[1, 2, 3], [5]].
    """
    blocks: list[list[int]] = []
    for i in sorted(idxs):
        if blocks and i == blocks[-1][-1] + 1:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    return blocks


def _price_text_rect_in_slot(arr, slot) -> tuple | None:
    """在价格区内用图形学求**价格数字**的精确 rect: 从最右白列往左扫, 遇 >=10px 空隙即停.

    与 `_shop_text_rect_in_slot` 正好相反 —— 名字取"最左那段", 价格取"最右那段"
    (价格右对齐贴卡缘, 左边紧挨金币图标; 图标是金色, 被白掩码排除)。
    """
    x0, y0, x1, y1 = slot
    if arr is None or arr.ndim < 2:
        return None
    sub = arr[y0:y1, x0:x1]
    if sub.size == 0:
        return None
    m = _shop_white_mask(sub)
    cols = np.where(m.any(axis=0))[0]
    rows = np.where(m.any(axis=1))[0]
    if not len(cols) or not len(rows):
        return None
    last = int(cols[-1])
    start = last
    prev = last
    for c in cols[::-1]:
        c = int(c)
        if prev - c >= 10:
            start = prev
            break
        prev = c
    else:
        start = int(cols[0])
    return (x0 + start, y0 + int(rows.min()), x0 + last + 1, y0 + int(rows.max()) + 1)


def _slot_prices_fast(img, idxs: list[int]) -> dict[int, int]:
    """价格免 det 快路径: 图色定数字框 -> 白二值化 -> 4x -> `recognize_line`.

    用户 2026-11-19: 名字已经做到"图色定框 + 免 det"(`extract_shop_geo`), 价格却还走
    `recognize_region(..., upscale=4)`(含 det), 单次 340~460ms, 是商店里最后一段慢路径。
    实测同一帧 5 槽: 本函数 **24ms 且 5/5 读对**(含原路径漏掉的槽4), 原路径 460ms 只读到 4 槽。
    数字是单字符, 白二值化后放大 4x, 识别 score 全是 1.00。
    """
    arr = np.asarray(img.convert("RGB") if hasattr(img, "convert") else img)
    out: dict[int, int] = {}
    for idx in idxs:
        if idx < 0 or idx >= len(regions.SHOP_SLOT_LEFTS):
            continue
        right = regions.SHOP_SLOT_LEFTS[idx] + regions.SHOP_SLOT_WIDTH
        slot = (right - 60, regions.SHOP_NAME_Y, right + 35, regions.SHOP[3])
        rect = _price_text_rect_in_slot(arr, slot)
        if rect is None:
            continue
        tx0, ty0, tx1, ty1 = rect
        crop = img.crop((tx0 - 2, ty0 - 2, tx1 + 2, ty1 + 2))
        ca = np.asarray(crop.convert("RGB"))
        m = _shop_white_mask(ca)
        if int(m.sum()) < 12:
            continue
        buf = np.zeros((*m.shape, 3), dtype="uint8")
        buf[m] = 255
        bim = Image.fromarray(buf)
        w, h = bim.size
        if w > 0 and h > 0:
            bim = bim.resize((w * 4, h * 4), Image.LANCZOS)
        text, _score = ocr.recognize_line(bim)
        digits = re.findall(r"\d", text or "")
        if digits:
            out[idx] = int(digits[-1])
    return out


def _slot_prices(img, idxs: list[int]) -> dict[int, int]:
    """缺失价格槽批量兜底: 相邻槽合并成一段做**一次** 4x 裁剪 OCR, 再按 x 归属到具体槽.

    价格数字右对齐贴真实卡缘, 而槽宽标定 (220px) 略小于实际卡宽 (~230px),
    数字常整体悬挂在槽右界外侧 (如阿狸 "4" 起点 x≈槽右界+2), 仅右扩 12px 仍会整字丢失;
    行界外扩为左-20/右+30 兜住悬挂数字, 槽间靠 `_price_slot` (右缘 ±25px) 归属, 中心距 233 不会串槽.

    归属用条目的**右缘**而不是中心: 合并裁剪时金币图标与价格会粘成一条 "91"
    (实测 frame_A_initial.png 槽1: box=(671.6, 1030.25, 702.1, 1049.75) → 中心 686.9 已掉出
    右缘容差 [690, 740], 右缘 702.1 才贴卡缘) —— 用中心会整槽漏价 (回归用例见
    tests/ocr/test_shop_price_fallback.py::test_glued_coin_icon_attributed_by_right_edge).

    每个槽取**该槽内最右**的数字条目 (右端才是价格, 左边的金币图标常被读成 "9") 的最后一位数字
    —— 与旧的"整段拼接后取最后一个数字"同一口径. 读不清的槽不写 (宁可不填, 不猜).

    用户 m00002 提速实测 (常驻 GPU 服务, frame_A_initial.png): 逐槽 2 次 0.58s -> 合并 1 次 0.24s.
    """
    out: dict[int, int] = {}
    left: list[int] = list(idxs)
    # 快路径 (免 det) 先读; 读不出的槽才回落下面的 det 合并带 (宁慢不错)。
    if getattr(default_config, "SHOP_PRICE_GEO_RECOGNIZE", True):
        out = _slot_prices_fast(img, idxs)
        left = [i for i in idxs if i not in out]
        if not left:
            return out
    for block in _price_blocks(left):
        rect = (regions.SHOP_SLOT_LEFTS[block[0]] - 20, regions.SHOP_NAME_Y,
                regions.SHOP_SLOT_LEFTS[block[-1]] + regions.SHOP_SLOT_WIDTH + 30,
                regions.SHOP[3])
        best: dict[int, tuple[float, int]] = {}  # 槽位 -> (最右条目的 x 右缘, 价格数字)
        for it in ocr.recognize_region(img, rect, upscale=4):
            digits = re.findall(r"\d", it["text"])
            if not digits:
                continue
            x2 = it["box"][2]
            idx = _price_slot(x2)
            if idx is None or idx not in block:
                continue
            if idx not in best or x2 > best[idx][0]:
                best[idx] = (x2, int(digits[-1]))
        for idx, (_, price) in best.items():
            out[idx] = price
    return out


def _price_slot(cx: float) -> int | None:
    """价格数字槽位: 价格渲染在卡右下角紧贴右缘, 按右缘 ±25px 归属 (槽间中心距 233 > 50, 不会双归属)."""
    for i, left in enumerate(regions.SHOP_SLOT_LEFTS):
        right = left + regions.SHOP_SLOT_WIDTH
        if right - 25 <= cx <= right + 25:
            return i
    return None


def _slot_name(img, rect: tuple, *, pad: int = 30) -> str | None:
    """卡槽名称: 名称行裁剪 2x OCR, 取含中文的条目按 x 拼接 (价格数字/图标噪声不含中文).

    `pad`: 名称行右扩像素。整条商店补名时用 30 (与 `_slot_prices` 同一外扩口径, 兜住
    贴卡缘的悬挂元素); 只读**单个**槽 (`extract_slot_name`) 时传 0 —— 单槽裁切右扩 30
    会伸进邻槽 (槽距 233, 槽宽 230, 右扩 30 已越过邻槽左界 27px), 有把邻槽牌名首字
    带进来的风险。
    """
    x1, _, x2, y2 = rect
    items = ocr.recognize_region(img, (x1 - 20, regions.SHOP_NAME_Y, x2 + pad, y2), upscale=2)
    names = [it for it in items if re.search(r"[\u4e00-\u9fff]", it["text"])]
    return _join(sorted(names, key=lambda it: it["box"][0])) if names else None


def extract_slot_name(img, idx: int, *, pad: int = 0) -> str | None:
    """只读商店第 `idx` 个槽 (0 起) 的**牌名**, 不碰价格/羁绊/卡身。

    为什么单独开这个入口 (2026-10-29 实测, `tempdir/_t_shop_fast.py`):
    `extract_shop()` 要先把整条商店窄带 OCR 一遍 (~390ms) 再对缺名槽补名称行 OCR,
    一次「读槽 1 牌名」合计 **674 / 694 / 898 ms**; 而 verify 的链2 buy 兜底**只要那一个
    名字** —— 直接读名称行只要 **244 / 254 / 495 ms**, 省约 430ms/买牌。
    拼接口径与 `_slot_name` 完全一致 (同一个函数), 不引入第二套裁切/拼接规则。

    Args:
        img: 已经截好的一帧 (1920x1080 基准)。
        idx: 槽位下标 0..4 (对应 `regions.SHOP_SLOT_LEFTS` 顺序); 越界返回 None。
        pad: 名称行右扩像素, 单槽默认 0 (理由见 `_slot_name`)。

    Returns:
        牌名; 读不到返回 None (读不到不猜, 由调用方弃权)。
    """
    if img is None:
        return None
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return None
    if not 0 <= i < len(regions.SHOP_SLOT_LEFTS):
        return None
    left = regions.SHOP_SLOT_LEFTS[i]
    rect = (left, regions.SHOP[1], left + regions.SHOP_SLOT_WIDTH, regions.SHOP[3])
    return _slot_name(img, rect, pad=pad)


def _slot_index(cx: float) -> int | None:
    """按 x 中心点判断所属卡牌槽位, 不在任何槽位内返回 None."""
    for i, left in enumerate(regions.SHOP_SLOT_LEFTS):
        if left - 6 <= cx <= left + regions.SHOP_SLOT_WIDTH + 6:
            return i
    return None


def _union_box(a: tuple, b: tuple) -> tuple:
    """两个包围盒 (x1, y1, x2, y2) 的并集."""
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _region_items(items: list[dict], region: tuple) -> list[dict]:
    """筛出中心点位于区域内的条目."""
    x1, y1, x2, y2 = region
    picked = []
    for it in items:
        bx1, by1, bx2, by2 = it["box"]
        if x1 <= (bx1 + bx2) / 2 <= x2 and y1 <= (by1 + by2) / 2 <= y2:
            picked.append(it)
    return picked


def _join(items: list[dict]) -> str:
    """区域内条目按 x 顺序拼接为单行文本."""
    return "".join(it["text"] for it in sorted(items, key=lambda it: it["box"][0]))


def _to_int(text: str) -> int | None:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None
