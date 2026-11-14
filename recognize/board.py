"""棋盘区域识别: 血条定位棋子, 提取场上/备战席棋子与左侧装备凹槽.

原理 (1920×1080 标准分辨率, 由 1080×720 基准等比缩放: x×16/9, y×3/2):
    - 棋子头顶有亮绿色血条 (约 76×8px, RGB≈(123,222,106)), 以颜色掩码定位;
    - 血条下方约 104×89 区域为棋子头像, 交由 units.matcher 模板匹配身份;
    - 小小英雄也有血条, 通过血条上方是否有白色名字文本排除;
    - 棋盘左侧一列六边形凹槽为装备库存区, 以金色掩码检测已存放的装备图标.

血条定位双方案 (config.BAR_DETECT_METHOD 切换):
    - "color"  : HSV/RGB 颜色掩码找亮绿像素, 再连通域归组 (默认; 对色偏敏感);
    - "template": 用血条截图做 cv2.matchTemplate, 按分数峰值+NMS 定位
                  (对色彩偏移更鲁棒, 但满血模板对残血血条匹配度下降).

局限:
    - 对战阶段血条会因损伤变短/变色, 本模块主要面向准备阶段提取;
    - 敌方棋子为橙色血条, 暂不识别;
    - 血条左侧徽记可提供星级候选, 完整提取时用详情卡确认; 棋子身上装备暂不提取.
"""

import cv2
import numpy as np
from PIL import Image
from pathlib import Path

from units.matcher import match
from recognize.grid import bar_to_cell, board_position, bench_position
from config.config import default_config

# 血条搜索区 (1920×1080)
BAR_REGIONS = {
    "board": (382, 75, 1564, 645),    # 棋盘
    # 右界原为 1351 → **第 9 格(x≈1406)的血条整个落在区外, 永远扫不到**(用户实测 bench_9 识别不出);
    # 放宽到 1470: 覆盖第 9 格血条(x≈1375-1440), 同时仍在右侧 HUD(x>1490) 之前。
    "bench": (382, 645, 1470, 720),   # 备战席
}

PORTRAIT_H = 92     # 头像裁剪底边相对血条底部的偏移 (1080 基准模板裁剪 y2+60×1.5)
PORTRAIT_HALF_W = 52  # 头像裁剪半宽 (1080 基准 28×16/9, 略放宽给匹配留滑动余量)

# ---- 血条定位方案切换 ----
BAR_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "health_bar.png"
BAR_MATCH_THRESHOLD = 0.65   # matchTemplate 峰值阈值 (真实血条 0.74+, 误检 <0.65)
BAR_NMS_DIST = 50             # 非极大值抑制: 峰值间距小于此值则合并 (模板宽83, 相邻血条间距>50)


def _find_bars_color(arr: np.ndarray, region: tuple,
                     include_enemy_color: bool = False) -> list[tuple[int, int, int, int]]:
    """颜色掩码法: 在区域内找亮绿血条 (+可选橙色血条), 返回 [(x1, y1, x2, y2)].

    include_enemy_color: 同时找**橙棕色**血条. 观战视角下"非被观战方"的单位血条
    是橙棕色 (实测填充 (205,111,60)~(183,99,54), 上沿是浅色高光 (227,173,143)),
    只找亮绿会**一根都检不出** (2026-10-23 实测 board/bench 全为 0)。
    默认关闭, 不影响自己对局的行为。
    橙色分支**不做上沿深色边框检查** —— 它的上沿本来就不是深色, 检查会全滤掉;
    误检由下游(格位映射/头像匹配)兜。
    """
    x0, y0, x1, y1 = region
    roi = arr[y0:y1, x0:x1]
    # 注意: np.array(PIL RGB) 的通道顺序就是 R,G,B。
    r, g, b = (roi[:, :, i].astype("int16") for i in range(3))
    green = ((g > 170) & (g - r > 50) & (g - b > 50)).astype(np.uint8)
    bars: list[tuple[int, int, int, int]] = []

    def _collect(mask: np.ndarray, check_frame: bool) -> None:
        m = cv2.morphologyEx(mask * 255, cv2.MORPH_CLOSE, np.ones((1, 16), np.uint8))
        n, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
        for i in range(1, n):
            x, y, w, h, _ = stats[i]
            if not (44 <= w <= 107 and 4 <= h <= 15):  # 满血约76px, 允许对战损伤变短
                continue
            if check_frame:
                # 绿色模型/特效也可能形成长条，真血条上沿有连续深色边框。
                top = arr[max(0, y0 + y - 5):y0 + y, x0 + x:x0 + x + w]
                if top.size == 0 or (top.max(axis=2) < 90).mean(axis=1).max() < 0.8:
                    continue
            bars.append((int(x0 + x), int(y0 + y), int(x0 + x + w), int(y0 + y + h)))

    _collect(green, True)
    if include_enemy_color:
        orange = ((r > 150) & (r - g > 45) & (g - b > 15) & (b < 130)).astype(np.uint8)
        _collect(orange, False)
    return bars


def _find_bars_template(arr: np.ndarray, region: tuple) -> list[tuple[int, int, int, int]]:
    """模板匹配法: 用血条截图做 matchTemplate, 峰值+NMS 定位血条.

    满血模板对残血血条匹配度下降 (绿色填充变短), 主要面向准备阶段.
    """
    if not BAR_TEMPLATE_PATH.exists():
        return []
    x0, y0, x1, y1 = region
    roi = arr[y0:y1, x0:x1]
    tpl = np.array(Image.open(BAR_TEMPLATE_PATH).convert("RGB"))
    if tpl.size == 0:
        return []
    res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(res >= BAR_MATCH_THRESHOLD)
    if len(xs) == 0:
        return []
    scores = res[ys, xs]
    order = np.argsort(scores)[::-1]  # 按分数降序
    picked: list[tuple[int, int]] = []
    for idx in order:
        px, py = int(xs[idx]), int(ys[idx])
        if all(abs(px - p[0]) + abs(py - p[1]) > BAR_NMS_DIST for p in picked):
            picked.append((px, py))
    tw, th = tpl.shape[1], tpl.shape[0]
    bars = []
    for px, py in picked:
        bx1, by1, bx2, by2 = px, py, px + tw, py + th
        w, h = bx2 - bx1, by2 - by1
        if 44 <= w <= 107 and 4 <= h <= 15:
            bars.append((x0 + bx1, y0 + by1, x0 + bx2, y0 + by2))
    return bars


BAR_MERGE_DY = 4        # 同一条(被分隔符切碎)的血条, 各碎片的竖向中心偏差上限
BAR_MERGE_GAP = 12      # 碎片之间的横向空隙上限 (分隔符约 2~6px)


def _merge_bar_pieces(bars: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """把**同一条血条被分隔符切成的多段**合回一根 (2026-11-14).

    战斗/野怪血条是**分段渲染**的 (每格之间有深色分隔符), 颜色法会沿分隔符把它切成几十片:
    `board_green_model.png` 一根野怪大血条被切成 **25 片**, 每片都不带徽记, 于是全被
    "无徽记即误检"规则丢掉 —— 一根真血条都没留下。合并后再判定, 徽记就在合并后血条的左端。
    相邻两只棋子的血条横向间距 >= 40px (格宽 78), 不会被误合并。
    """
    if not bars:
        return []
    out: list[tuple[int, int, int, int]] = []
    for b in sorted(bars, key=lambda t: ((t[1] + t[3]) // 2, t[0])):
        cy = (b[1] + b[3]) // 2
        for i, o in enumerate(out):
            if (abs((o[1] + o[3]) // 2 - cy) <= BAR_MERGE_DY
                    and -BAR_MERGE_GAP <= b[0] - o[2] <= BAR_MERGE_GAP):
                out[i] = (min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3]))
                break
        else:
            out.append(b)
    # 合并可能让 A 同时贴住 B 和 C: 再来一两轮收敛 (碎片数量级很小, 代价可忽略)
    for _ in range(3):
        merged_any = False
        again: list[tuple[int, int, int, int]] = []
        for b in out:
            cy = (b[1] + b[3]) // 2
            for i, o in enumerate(again):
                if (abs((o[1] + o[3]) // 2 - cy) <= BAR_MERGE_DY
                        and -BAR_MERGE_GAP <= b[0] - o[2] <= BAR_MERGE_GAP):
                    again[i] = (min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3]))
                    merged_any = True
                    break
            else:
                again.append(b)
        out = again
        if not merged_any:
            break
    return out


def _find_bars(arr: np.ndarray, region: tuple,
               method: str | None = None,
               include_enemy_color: bool = False) -> list[tuple[int, int, int, int]]:
    """按 method 选择血条定位方案; method=None 时用 config.BAR_DETECT_METHOD.

    method="union"/"both": **颜色法与模板法取并集** (用户口径 2026-10-12).
    实测依据 (3-5_120932_916.png 及 25 帧统计): 单跑 template 时**叠放的两颗棋子只认出一根**
    (该帧 template 3 根 / color 4 根), 并集 68 根 > template 67 根, 且**从不更差**
    (逐帧: 并集更多 1 帧 / template 更多 0 帧 / 相同 24 帧)。
    血条是所有下游(探针格位计划/格位映射/静态补全)的共同输入, 漏一根就是假负样本。
    去重: 中心距 <= 20px 视为同一根。
    注意: **不要在叠加了标注框的图上评估** —— 框线会把 color 法从 4 根打到 1 根。
    """
    method = (method or default_config.BAR_DETECT_METHOD or "union").lower()
    if method == "template":
        return _merge_bar_pieces(_find_bars_template(arr, region))
    if method == "color":
        return _merge_bar_pieces(_find_bars_color(arr, region, include_enemy_color))
    # union / both / 其它未知值 -> 并集 (最稳)
    merged: list[tuple[int, int, int, int]] = []
    for one in (_find_bars_color(arr, region, include_enemy_color),
                _find_bars_template(arr, region)):
        for b in one:
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            if not any(abs((m[0] + m[2]) / 2 - cx) <= 20 and abs((m[1] + m[3]) / 2 - cy) <= 20
                       for m in merged):
                merged.append(b)
    return _merge_bar_pieces(merged)


def _has_name_text(arr: np.ndarray, bar: tuple[int, int, int, int]) -> bool:
    """血条上方是否有白色名字文本 (小小英雄名牌特征).

    名字文本=多行连续高密度白色 (每行 >=10px, 文本行高约 15px);
    上方棋子模型的白色高光=稀疏散点 (每行 1-4px), 用高密度行数区分,
    避免竖向堆叠时上方棋子的身体被误判为名牌 (实测总量阈值 30 会误杀).
    """
    x1, y1, x2, _ = bar
    patch = arr[max(0, y1 - 24): y1 - 3, max(0, x1 - 14): x2 + 14]
    if patch.size == 0:
        return False
    r, g, b = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
    white = (r > 190) & (g > 190) & (b > 190)
    dense_rows = int((white.sum(axis=1) >= 10).sum())
    return dense_rows >= 5


def _cell_label(region_name: str, own) -> str | None:
    """`taken` 里的格位 key -> 可读格名 (`board,r,c` / `bench,N`), 供 `match_cells` 比对。"""
    if own is None:
        return None
    if own[0] == "board":
        return f"board,{int(own[1])},{int(own[2])}"
    return f"bench,{int(own[1])}"


def extract_units(img, bar_method: str | None = None,
                  match_threshold: float | None = None,
                  include_legend: bool = False,
                 include_bar_less: bool | None = None,
                  bar_less_scope: str | None = None,
                  include_enemy_color: bool | None = None,
                  match_cells=None) -> dict:
    """提取场上与备战席的棋子.

    Args:
        img: 1920×1080 截图 (PIL Image).
        bar_method: 血条定位方案, "color"/"template"/None(=模块默认).
        match_threshold: 身份识别相似度阈值, None 则用 matcher 默认 0.55.
        include_legend: 是否保留小小英雄 (默认过滤; True 时其单位带 "legend": True).
        include_enemy_color: 是否同时把**橙棕色**血条(观战/敌方配色)纳入检测.
            不传则读 config.BAR_INCLUDE_ENEMY_COLOR(默认 False)。观战截图必须开,
            否则 board/bench 会一根都检不出(2026-10-23 实测)。
        match_cells: **只对这些格位做身份模板匹配** (`{"board,2,4", "bench,3"}`),
            None = 全部匹配(旧行为)。身份匹配是全棋子库多尺度扫描, 单只 145ms, 9 只血条
            就要 1.3s (占本函数 73%)。给装/合成的前后校验只关心目标那一格, 传进来即可
            把 1.8s 压到 ~0.5s; 其它格的名字留空(None)而不猜 —— 位置/血条/星级照常给出。

    Returns:
        {"board": [unit, ...], "bench": [unit, ...]},
        unit 包含 name/score/center、bar、stars候选、cell_anchor和location。
        location: 场上row/column/own_row/cell_center，备战席slot/cell_center；未映射为None。
    """
    if include_enemy_color is None:
        include_enemy_color = bool(getattr(default_config, "BAR_INCLUDE_ENEMY_COLOR", False))
    arr = np.array(img.convert("RGB"))
    result = {}
    #: 被"无星级徽记"规则丢掉的候选血条 (诊断用; 真棋子血条不会进这里)
    dropped_no_badge: list[dict] = []
    #: 本帧全部血条按区域收集 (全局最优分配需要"看到整帧的血条再决定谁归哪格")。
    bars_of: dict[str, list[tuple]] = {}
    for region_name, region in BAR_REGIONS.items():
        bars_of[region_name] = _find_bars(arr, region, bar_method, include_enemy_color)
    # ---- 血条 -> 格位: 全局最优分配 (2026-10-25) ----
    # 逐根最近邻的致命缺陷: 同一帧 3 个棋子只检出 2 根血条时, 必然把某根判给邻居已占的格位;
    # 而 dy (血条下沿 -> 格心) 实测 55~215px 浮动, 跨度 134px > 行间距 68px, 单根血条无法定行。
    # 改成"每格最多一根 + 全局代价最小"后实测 (266 干净帧): 精确率 0.9182->0.9326,
    # 召回 0.9088->0.9292。分配不上的血条**不猜格位** (宁可漏采不可采错)。
    taken: dict[tuple, int] = {}
    if getattr(default_config, "BAR_ASSIGN_GLOBAL", True):
        flat: list[tuple] = []
        where: list[tuple[str, int]] = []
        for region_name, _bars in bars_of.items():
            for bi, b in enumerate(_bars):
                flat.append(((b[0] + b[2]) // 2, b[3], region_name))
                where.append((region_name, bi))
        try:
            from recognize.grid import assign_bars_to_cells
            for cell, fi in assign_bars_to_cells(flat).items():
                taken[where[fi]] = cell        # (区域, 区内下标) -> 格位 key
        except Exception as _e:      # 分配器异常不许拖垮识别 (退回逐根映射)
            print(f"[血条分配] 全局分配失败, 退回逐根映射: {_e!r}")
            taken = {}
    for region_name, region in BAR_REGIONS.items():
        units = []
        for bi, bar in enumerate(bars_of[region_name]):
            x1, y1, x2, y2 = bar
            # 星级徽记 = 血条定位的正样本锚点 (2026-11-14): 棋子血条左侧必有徽记,
            # 有徽记就读它的颜色定星级 (比数血条条纹稳), 没徽记也不是小小英雄 -> 误检丢弃。
            badge_stars, badge_box = star_badge(arr, bar)
            rank = badge_stars if badge_stars is not None else bar_stars(arr, bar)
            # 小小英雄的左侧是**等级数字**(白字), 不是星级 chevron —— 徽记框里白像素多就是数字。
            level_number = badge_box is not None and badge_white_count(arr, badge_box) >= BADGE_NUMBER_WHITE
            if level_number:
                badge_stars = None          # 数字不是星级, 别把小小英雄当 2 星棋子
                rank = None                 # 小小英雄没有星级
            # 竖向堆叠时上方棋子白色模型可能落进名牌窗；有星级徽记的是真棋子。
            # 等级数字框本身就是小小英雄的铁证 (不必再靠名牌, 名牌在战斗帧常被遮)。
            legend = (_has_name_text(arr, bar) and (badge_stars is None or level_number)) or level_number
            if legend and not include_legend:
                continue  # 小小英雄名牌
            if badge_stars is None and not legend and BAR_REQUIRE_BADGE:
                dropped_no_badge.append({"bar": list(bar), "stars_color": rank})
                continue        # 无星级徽记: 不是棋子血条 (草地/顶栏/HUD 装饰被当成血条)
            cx = (x1 + x2) // 2
            portrait = img.crop((cx - PORTRAIT_HALF_W, y2 + 3,
                                 cx + PORTRAIT_HALF_W, y2 + PORTRAIT_H))
            _own = taken.get((region_name, bi))
            # 身份模板匹配是全棋子库多尺度扫描, 单只实测 **145ms** —— 9 只血条就是 1.3s,
            # 占 `extract_units` 总耗时的 73% (2026-10-31 实测 1784ms 里的 1305ms)。
            # `match_cells` 让校验链路只认**关心的那一格**: 别格的名字一律留空 (不猜),
            # 位置/血条/星级照算 (`location` 与名字无关, 格位来自血条 -> 格的全局分配)。
            if match_cells is None or _cell_label(region_name, _own) in match_cells:
                name, score = match(portrait, threshold=match_threshold)
            else:
                name, score = None, 0.0
            unit = {"name": name, "score": score, "center": [cx, (y2 + 3 + y2 + PORTRAIT_H) // 2]}
            unit["bar"] = list(bar)
            if _own is not None and _own[0] == "board":
                from recognize.grid import cell_dict
                _loc = cell_dict(_own[1], _own[2],
                                 [cx, y2 + int(default_config.BOARD_BAR_TO_CELL_Y)])
                unit["cell_anchor"] = [cx, y2 + int(default_config.BOARD_BAR_TO_CELL_Y)]
                unit["location"] = _loc
                unit["cell_source"] = "global"
            elif _own is not None:
                _slot = int(_own[1])
                unit["cell_anchor"] = [cx, y2 + default_config.BOARD_BAR_TO_CELL_Y]
                unit["location"] = {"slot": _slot,
                                    "cell_center": list(default_config.BENCH_SLOT_CENTERS[_slot - 1])}
                unit["cell_source"] = "global"
            elif region_name == "board":
                # 没被全局分配 (可能是敌方半场血条 / 配不上任何己方格): 不猜格位。
                _loc = bar_to_cell(cx, y2)
                if _loc is not None and int(_loc.get("row") or 0) >= 5:
                    unit["cell_anchor"] = [cx, y2 + int(_loc.get("anchor_dy",
                                                                default_config.BOARD_BAR_TO_CELL_Y))]
                    unit["location"] = _loc
                    unit["cell_source"] = "nearest"
                else:
                    unit["cell_anchor"] = [cx, y2 + default_config.BOARD_BAR_TO_CELL_Y]
                    unit["location"] = None
                    unit["cell_source"] = "unassigned"
            else:
                unit["cell_anchor"] = [cx, y2 + default_config.BOARD_BAR_TO_CELL_Y]
                unit["location"] = bench_position(unit["center"])
                unit["cell_source"] = "nearest"
            unit["stars"] = rank if not legend else None
            if legend:
                unit["legend"] = True
            units.append(unit)
        units.sort(key=lambda u: (u["center"][1], u["center"][0]) if region_name == "board"
                   else (u["center"][0], u["center"][1]))
        result[region_name] = units
    # ---- 无血条奕子补检: 纯图色工程没有 SAM3/内存通道 (recognize/bar_less.py 不拷) ----
    # 历史实现在这里调 `bar_less.merge_sources + bar_less.fill` 给无血条奕子补检;
    # 新工程只用血条 + YOLO 两路, 所以这里只给血条检出的单位打 source 标记, 不再补检。
    # `include_bar_less` / `bar_less_scope` 参数保留 (调用方签名兼容), 但不产生任何行为。
    for area in ("board", "bench"):
        for u in (result.get(area) or []):
            u.setdefault("source", "bar")
    # ---- 两路统一去重 (血条/YOLO 谁先谁后都按 slot 去重一次) ----
    # 同一只奕子可能被多路各检出一次; 这里在"各路径都已给出格位"之后, 按 (row,column)/slot
    # 精确判重 —— 同 slot 只留优先级最高的一条。相邻行的两只真奕子 slot 不同, 不会被误合并。
    for area in ("board", "bench"):
        result[area] = _dedupe_units(result.get(area) or [])
    # 诊断信息放模块级 (不能塞进 result: 下游 `extract_board_equips(units=result)` 会
    # 把 result 当"区域 -> 单位列表"遍历, 多一个非列表键就会炸)。
    _LAST_STATS.clear()
    _LAST_STATS.update({"dropped_no_badge": dropped_no_badge,
                        "require_badge": BAR_REQUIRE_BADGE})
    return result


def last_stats() -> dict:
    """最近一次 `extract_units` 的徽记诊断 (被"无星级徽记"规则丢掉的候选血条等)."""
    return {"dropped_no_badge": list(_LAST_STATS.get("dropped_no_badge") or []),
            "require_badge": _LAST_STATS.get("require_badge", BAR_REQUIRE_BADGE)}


def _slot_key(u: dict) -> tuple | None:
    """unit 的格位 key: bench 用 slot, board 用 (row, column); 无格位返回 None (不参与去重)."""
    loc = u.get("location") or {}
    if loc.get("slot"):
        return ("bench", int(loc["slot"]))
    if loc.get("row") and loc.get("column"):
        return ("board", int(loc["row"]), int(loc["column"]))
    return None


def _dedupe_units(units: list[dict]) -> list[dict]:
    """统一去重 (slot 精确): 同一 (row,column)/slot 只留优先级最高的一条, 合并名字/星级.

    优先级: 有血条(位置最准) > 有 YOLO box(名字最准的补充) > 其它只有位置的条目。
    没有格位的条目无法判重, 原样保留。

    **纯图色工程不再有 SAM3 框**: 历史版本在去重前用 `sam3_location` 修正血条 mapping
    (瘦长棋子血条固定锚点会判偏 1 行), 该通道已随内存方案一起删掉 —— 血条 mapping 就是
    唯一位置来源, 冲突修正不做 (没有第二路位置可比)。
    """
    def _prio(u: dict) -> int:
        if u.get("bar"):
            return 0
        if u.get("box"):
            return 1
        return 2
    kept: list[dict] = []
    seen: dict = {}
    for u in sorted(units, key=_prio):
        key = _slot_key(u)
        if key is None:
            kept.append(u)
            continue
        dup = seen.get(key)
        if dup is None:
            seen[key] = u
            kept.append(u)
            continue
        if not dup.get("name") and u.get("name"):
            dup["name"] = u["name"]
            dup["identity_source"] = u.get("identity_source")
            dup["score"] = u.get("score")
        if dup.get("stars") is None and u.get("stars") is not None:
            dup["stars"] = u["stars"]
        if dup.get("bar") is None and u.get("bar"):
            dup["bar"] = u["bar"]
    return kept


def bar_stars(arr, bar) -> int | None:
    """血条左侧等级徽记: 铜色I/银蓝菱形II/金色III; 没有徽记不猜1星。"""
    x1, y1, _, y2 = bar
    patch = arr[max(0, y1 - 7):y2 + 7, max(0, x1 - 32):x1].astype(int)
    if not patch.size:
        return None
    r, g, b = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
    gold = (r > 180) & (g > 165) & (b < 120)
    silver = (g > 150) & (b > 170) & (b - r > 20)
    bronze = (r > 145) & (g > 85) & (g < 180) & (b < 130) & (r - g > 30)
    for stars, mask in ((3, gold), (2, silver), (1, bronze)):
        if int(mask.sum()) >= 12:
            return stars
    return None


# ---- 星级徽记定位 (2026-11-14 用户口径) -----------------------------------
# 用户给的三个模板: 1星=铜色 chevron(21×22) / 2星=银蓝菱形(19×17) / 3星=金色 chevron(25×18),
# 原件存 `templates/star/star_{1,2,3}.png`。口径: **每个棋子血条左侧必定有星级徽记**
# (小小英雄左侧则是等级数字, 不是 chevron), 所以徽记可以作为血条定位的"正样本锚点":
#   有徽记 -> 一定是棋子血条, 星级直接读徽记颜色;
#   没徽记也不是小小英雄 -> 这根"血条"是误检 (草地色块/顶栏装饰/HUD 元素),
#   实测 bench_forge.png 里 (1124,84) 那根就是一片绿色藤蔓被 color 法当成血条。
BADGE_WIN = (-40, -12, 2, 12)      # 相对血条 (x1,y1,x2,y2) 的搜索窗: (左, 上, 右, 下)
BADGE_AREA_RANGE = (40, 600)       # 彩色 chevron 像素面积 (实测 1★ 48~128, 2★ 193~215)
BADGE_H_RANGE = (8, 30)
BADGE_W_RANGE = (6, 32)
BADGE_DY_MAX = 12                  # 徽记竖向中心与血条竖向中心的最大偏差 (实测 0~6)
#: 血条必须带星级徽记才算真棋子血条 (关掉就回到"有绿条就认"的旧宽松口径, 易误检)。
BAR_REQUIRE_BADGE = True

#: 最近一次 extract_units 的徽记诊断 (模块级, 见 last_stats())
_LAST_STATS: dict = {}
#: 颜色类 -> HSV 上下界 (OpenCV H 0-179)。金色优先级最高 (与铜色色相相邻, 靠 S/V 拉开)。
#: [!] 3 星金徽记**本机测试帧里没有样本**, 区间是按用户模板 + 游戏内金色 UI 推断的
#: (H 26~42 偏黄, 与铜色 H 8~25 分开); 拿到真实 3★ 帧后请用
#: `python -X utf8 -u tools/probe_star_badge.py --img <帧>` 复核并微调。
BADGE_COLORS: tuple[tuple[int, tuple, tuple], ...] = (
    (3, (26, 140, 110), (42, 255, 255)),
    (2, (85, 20, 120), (118, 120, 255)),
    (1, (8, 90, 110), (25, 255, 255)),
)


def badge_white_count(arr: np.ndarray, bbox: tuple) -> int:
    """徽记框内的白色像素数 (V≥195 且 S≤45).

    用来把**小小英雄的等级数字**和**棋子的星级 chevron**分开 (2026-11-14 用户口径:
    "hero 的血条左边就一定是代表等级的数字"):
      实测 等级数字框 white=52~73 (数字本身是白的),
           1★ 铜 chevron white=0, 2★ 银蓝 chevron white=0 (银蓝 S 中位 87, 不算白)。
    """
    x, y, w, h = bbox
    sub = cv2.cvtColor(arr[y:y + h, x:x + w], cv2.COLOR_RGB2HSV)
    s = sub[:, :, 1].astype(int)
    v = sub[:, :, 2].astype(int)
    return int(((v >= 195) & (s <= 45)).sum())


#: 徽记框白色像素达到这个数 -> 认定是等级数字框 (小小英雄), 而不是星级 chevron
BADGE_NUMBER_WHITE = 25


def star_badge(arr: np.ndarray, bar) -> tuple[int | None, tuple | None]:
    """在血条左侧窗口找**星级徽记** (1/2/3 星 chevron).

    Returns:
        (stars, bbox): stars 为 1/2/3; 没找到返回 (None, None)。
        bbox 为徽记彩色部分的整帧坐标 (x, y, w, h)。

    定位口径 (2026-11-14): 徽记是 UI 元素, 形状/色相/尺寸都固定, 比"绿色长条"稳定得多;
    因此它既能**判定这根是不是真血条** (棋子血条必有徽记), 又能**直接给出星级**
    (借此取代按血条条纹数星级的旧做法 —— 条纹在战斗帧被分段/被特效压盖时会数错)。
    """
    x1, y1, x2, y2 = bar
    dl, du, dr, dd = BADGE_WIN
    wx1, wy1 = max(0, x1 + dl), max(0, y1 + du)
    wx2, wy2 = x1 + dr, y2 + dd
    if wx2 <= wx1 or wy2 <= wy1:
        return None, None
    win = arr[wy1:wy2, wx1:wx2]
    hsv = cv2.cvtColor(win, cv2.COLOR_RGB2HSV)
    bar_cy = (y1 + y2) / 2
    best = None
    for stars, lo, hi in BADGE_COLORS:
        mask = cv2.inRange(hsv, np.array(lo), np.array(hi))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            if not (BADGE_AREA_RANGE[0] <= area <= BADGE_AREA_RANGE[1]):
                continue
            if not (BADGE_H_RANGE[0] <= h <= BADGE_H_RANGE[1]):
                continue
            if not (BADGE_W_RANGE[0] <= w <= BADGE_W_RANGE[1]):
                continue
            cy = wy1 + y + h / 2
            if abs(cy - bar_cy) > BADGE_DY_MAX:
                continue
            # 徽记必须在血条**左侧**: 右边界不越过血条左沿太多 (留 4px 容差)
            if wx1 + x + w > x1 + 4:
                continue
            cand = (stars, (wx1 + x, wy1 + y, w, h), area)
            if best is None or area > best[2]:
                best = cand
    if best is None:
        return None, None
    return best[0], best[1]


def bar_stars_hybrid(arr: np.ndarray, bar) -> int | None:
    """星级读取: 先读徽记 (准), 徽记没读出来再退回旧的颜色计数法。"""
    stars, _bbox = star_badge(arr, bar)
    if stars is not None:
        return stars
    return bar_stars(arr, bar)



BENCH_ICON_SCALES = tuple(round(0.7 + 0.05 * i, 2) for i in range(12))  # 格内图标渲染有轻微缩放抖动


def extract_bench_items(img, threshold: float | None = None,
                        scales: tuple[float, ...] = BENCH_ICON_SCALES) -> list[dict]:
    """无血条备战席消耗品的独立模板库。仅匹配已采集的已知图标，不猜未知物品。

    每个格子在多尺度下匹配全部模板, 取跨模板跨尺度最高分, 达到 threshold 才计入。
    threshold 缺省用 **`config/config.py:95 BENCH_ITEM_FORGE_THRESHOLD`**, 现值 **0.75**
    (引入时就是 0.75, commit 780fec0 "user asked me to set it")。下面这段 0.80 的
    实测是**当时的记录**, 现在是历史依据, 不是当前值 —— 别照着它改代码。
    用户口径 (2026-10-12):
      "优先识别[是否锻造器], 不允许遗漏, 即使误判成装/基础, 也可以接收, 只需要定位出它是[锻造器]"
    实测 60 帧 / 63 个真锻造器: 真品分数下限 **0.849**, 非锻造器上限 **0.848** ->
      * 原阈值 0.85 -> 召回 98.4% (漏 1 个, 正是用户看到的"第二个能查出、第三个不行")
      * 阈值 0.80 -> 召回 **100%** (非锻造器误报 5/477 ≈ 1%, 且其中多半是真品: 上限 0.848 说明它们本就贴着真品下限)
      阈值 0.75 是用户后来拍的更宽口径 (同样的取舍, 只是更宽一点)。
    类别 = 跨类最高分的那一类 (允许基础/成装判错, 用户明确接受)。
    2026-10-15 复核 `images/sample/image_test__备战席的成装锻造器.png`: 格1 成装 0.978 /
    格3 基础 0.832, 两种都判对了 —— 这张图里本来就两种锻造器各一个。
    同一物品可有多个模板变体, 文件名用 "__" 分隔: 如 基础装备锻造器__2.png (高光/动画态),
    识别名只取 "__" 前的部分。
    """
    if threshold is None:
        threshold = float(getattr(default_config, "BENCH_ITEM_FORGE_THRESHOLD", 0.80))
    arr = np.asarray(img.convert("RGB"))
    result = []
    for slot, (cx, cy) in enumerate(default_config.BENCH_SLOT_CENTERS, 1):
        x0, y0 = cx - 70, cy - 65
        roi = arr[y0:cy + 60, x0:cx + 70]
        best = (0.0, None, None)
        for path in default_config.BENCH_ITEM_TEMPLATE_DIR.glob("*.png"):
            tpl = np.asarray(Image.open(path).convert("RGB"))
            for scale in scales:
                t = tpl if scale == 1.0 else cv2.resize(tpl, None, fx=scale, fy=scale)
                if t.shape[0] > roi.shape[0] or t.shape[1] > roi.shape[1]:
                    continue
                _, score, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED))
                if score > best[0]:
                    best = (score, path.stem.split("__")[0], [x0 + x + t.shape[1] // 2, y0 + y + t.shape[0] // 2])
        
        # 调试信息：显示最佳匹配分数
        if best[1]:
            if best[0] >= threshold:
                result.append({"slot": slot, "name": best[1], "score": round(best[0], 3),
                               "center": best[2], "stars": None, "kind": "item"})
            else:
                pass
                # 调试：显示未达到阈值的最佳匹配
                # print(f"[匹配调试] 格{slot}: {best[1]} 得分{best[0]:.3f} < 阈值{threshold:.3f} (未命中)", flush=True)
    return result


def extract_inventory(img) -> list[dict]:
    """提取左侧装备凹槽区已存放的装备图标框; 身份/详情识别见 recognize/equipment.py."""
    from recognize import equipment
    return [{"box": [int(x) for x in rect],
             "center": [(rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2]}
            for rect in equipment.detect_slots(img)]
