"""1920×1080 透视棋盘网格。行列均从1开始，落脚点与点击点分开。"""

from math import hypot

from config.config import default_config

#: 全局分配用常数 (2026-10-25 实测标定, 见 `scripts/_qa_assign_sweep.py` / `_qa_prod_sweep.py`)。
#: 离线复刻 (266 干净帧, color 方法): DY_W=1.0 MAX_COST=150 -> 精确率 0.9312 / 召回 0.9278;
#:   DY_W=0.5 / 0.6 -> F1 0.9286; DY_W=0.3 -> 0.9276 (平台很平, 取 1.0 最稳)。
#: **生产链路** (60 帧 x 3 血条方法 x 3 代价上限, `_qa_prod_sweep.py`) 实测最优是
#:   `union` + MAX_COST=110 -> 精确率 0.9592 / 召回 0.9032 / F1 0.9304
#:   (同口径逐根映射 F1 只有 0.8768, 所以全局分配本身是对的);
#:   代价上限放大到 150 反而让 F1 掉到 0.9280 —— union 多检出的敌方/UI 长条会被"代价够小"拉进 row5。
ASSIGN_X_TOL = 45.0        #: board: 血条中心到格心的横向容差
ASSIGN_BENCH_X_TOL = 64.0  #: bench: 同上 (bench 格心间距 112, 半格 56 + 余量)
ASSIGN_DY_W = 1.0          #: 竖向偏移在代价里的权重
ASSIGN_MAX_COST = 110.0    #: 单根血条的最大可接受代价, 超过则视为"配不上格位"
ASSIGN_BENCH_DY = 63.0     #: bench 血条下沿 -> 备战席格心的偏移 (实测中位, 见 _qa_bench_bar_geom)
#: board 血条下沿的**硬下界**: 实测己方 4 行血条带是 285~592, 低于它的不是己方棋子
#: (顶部 UI 时间条实测 y2≈116; 敌方半场 row1~4 血条 y2≈85~267)。
#: 不加这条闸时, union 方法检出的敌方血条会被"代价够小"拉进 row5 —— 生产链路实测
#: 幽灵从 176 涨到 263、精确率 0.9290 掉到 0.8995 (435 帧)。加闸后敌方血条直接丢弃。
ASSIGN_BOARD_Y2_MIN = 270.0
#: bench 血条下沿的允许区间 (实测 645~717, 留余量)。
ASSIGN_BENCH_Y2_MIN = 620.0
ASSIGN_BENCH_Y2_MAX = 745.0


def _bar_ok_for_assign(area: str, by2: float) -> bool:
    """血条竖向范围闸: 一眼就知道不是己方棋子的血条直接不进分配 (不猜)。"""
    if area == "board":
        return float(by2) >= ASSIGN_BOARD_Y2_MIN
    return ASSIGN_BENCH_Y2_MIN <= float(by2) <= ASSIGN_BENCH_Y2_MAX


def board_cells() -> list[dict]:
    return [
        {"row": row, "column": col, "cell_center": [round(x + (col - 1) * step), y]}
        for row, (x, y, step) in enumerate(default_config.BOARD_GRID_ROWS, 1)
        for col in range(1, default_config.BOARD_COLUMNS + 1)
    ]


def board_position(point) -> dict | None:
    """将棋子落脚点映射到最近六边形; 越界/偏离网格时返回 None，禁止强行夹边。"""
    rows = default_config.BOARD_GRID_ROWS

    def distance(cell):
        row = cell["row"] - 1
        height = rows[min(row + 1, len(rows) - 1)][1] - rows[max(row - 1, 0)][1]
        height /= 2 if 0 < row < len(rows) - 1 else 1
        cx, cy = cell["cell_center"]
        return hypot((point[0] - cx) / rows[row][2], (point[1] - cy) / height)

    cell = min(board_cells(), key=distance)
    error = distance(cell)
    if error > default_config.BOARD_CELL_MAX_DISTANCE:
        return None
    return {**cell, "own_row": cell["row"] - 4 if cell["row"] >= 5 else None,
            "position_error": round(error, 3)}


def bar_to_cell_candidates(cx: int, y2: int) -> list[dict]:
    """血条下沿 (cx, y2) -> **有序候选格位** (机制 2: 点空就地点次优; 用户 m06560 / m06566).

    返回按 `position_error` 升序排列的候选 (最多 2 个: 两个锚点各一个, 落在同一格位时
    只留误差更小的那个), 每项带 `anchor_dy`; 两个锚点都映射不出 (越界) -> 空列表。
    用途: 探针点空 (没弹卡且卡区无变化) 时按这个顺序**就地点下一点**, 仍在本趟探针内,
    不启动第二轮, 从而把"静默丢格 → 整格假负样本"变成"一次正确的落点或明确的空位"。
    可用 `CollectConfig.probe_candidate_retry` 关闭这条路径。
    """
    offsets = [int(default_config.BOARD_BAR_TO_CELL_Y),
               int(getattr(default_config, "BOARD_BAR_ANCHOR_FALLBACK_Y", 44))]
    out: list[dict] = []
    for dy in offsets:
        loc = board_position([int(cx), int(y2) + dy])
        if not loc:
            continue
        _raw = loc.get("position_error")
        err = float(_raw) if _raw is not None else 9.9
        _cell = (loc.get("row"), loc.get("column"))
        if any((c.get("row"), c.get("column")) == _cell for c in out):
            continue        # 两个锚点落在同一格 -> 只留误差更小的那个
        out.append({**loc, "anchor_dy": dy, "position_error": err})
    out.sort(key=lambda c: float(c.get("position_error") or 9.9))
    return out


def bar_to_cell(cx: int, y2: int) -> dict | None:
    """血条下沿 (cx, y2) -> 棋盘格位: 两个锚点偏移各映射一次, 取 `position_error` 更小者。

    为什么不能只用一个固定偏移 (2026-10-13 4-2 帧事故):
    血条画在单位精灵正上方, 精灵高度逐单位不同, 所以"血条下沿 -> 落脚点"的真实偏移
    在同一帧内实测就有 55 / 70 / 73 / 88 / 102 / 111 px 六种。固定 +100 会把 r1c3 的
    雷克塞锚到下一行空格 (r2c3) -> 探针点到空地 -> 详情卡弹不出 -> 整格丢框 (假负样本),
    静态补标又在空格上补了个 unknown。取更小 `position_error` 等价于"哪个偏移的落点
    离格心更近就信哪个", 实测该帧 7 根血条 (含 r1c3) 全部命中正确格位。
    两个锚点都映射不出 (越界) 时返回 None —— 与 `board_position` 一样, 不强行夹边。
    """
    cands = bar_to_cell_candidates(cx, y2)
    return cands[0] if cands else None


def bench_position(point) -> dict | None:
    slots = default_config.BENCH_SLOT_CENTERS
    index = min(range(len(slots)), key=lambda i: hypot(point[0] - slots[i][0], point[1] - slots[i][1]))
    if hypot(point[0] - slots[index][0], point[1] - slots[index][1]) > default_config.BENCH_SLOT_MAX_DISTANCE:
        return None
    return {"slot": index + 1, "cell_center": list(slots[index])}

def board_position_quad(point) -> dict | None:
    """**四边形模型**: 把落脚点判进"行带 x 列带"的四边形格位 (用户口径 2026-10-12).

    与 `board_position`(六边形, 最近格心 + 归一化距离) 并存, 由
    `config.BOARD_CELL_MODEL` 选择 ("hex" 现有默认 / "quad")。

    做法: 每行有自己的 (x 起点, y, 列间距) —— 行带 = 相邻两行 y 的中点之间;
    列带 = 该行 x 起点 + (列-1)*间距 ± 间距/2。点落在哪个行带 x 列带, 就是那个格位。
    越界(行带/列带之外)返回 None, **不强行夹边** (与 hex 方案同一原则)。
    """
    rows = default_config.BOARD_GRID_ROWS
    ys = [r[1] for r in rows]
    n = len(rows)
    # 行带边界: 相邻行 y 的中点 (首行/末行各外扩半格)
    bounds = []
    for i in range(n):
        lo = -1e9 if i == 0 else (ys[i - 1] + ys[i]) / 2
        hi = 1e9 if i == n - 1 else (ys[i] + ys[i + 1]) / 2
        bounds.append((lo, hi))
    for i, (lo, hi) in enumerate(bounds):
        if not (lo <= point[1] < hi):
            continue
        x0, _, step = rows[i]
        col = int(round((point[0] - x0) / step)) + 1
        if col < 1 or col > default_config.BOARD_COLUMNS:
            return None
        return {"row": i + 1, "column": col, "cell_center": [round(x0 + (col - 1) * step), ys[i]],
                "own_row": (i + 1) - 4 if (i + 1) >= 5 else None, "position_error": 0.0}
    return None


def board_position_auto(point) -> dict | None:
    """按 config.BOARD_CELL_MODEL 选择 hex / quad 方案 (两种并存, 便于对比)。"""
    model = str(getattr(default_config, "BOARD_CELL_MODEL", "hex")).lower()
    if model == "quad":
        return board_position_quad(point)
    return board_position(point)


# ---------------------------------------------------------------------------
# 血条 -> 格位: 全局最优分配 (2026-10-25)
# ---------------------------------------------------------------------------
# 为什么不再逐根最近邻 (实测依据, 见 dev_docs/02_采集与训练/图色采集天花板实测_血条与棋子_20261025.md):
#   同一帧内 3 个棋子只检出 2 根血条时, "逐根找最近格" 必然把某根血条判给已经被邻居
#   占掉的格位, 于是同一根血条既算对又算错; 而 dy (血条下沿 -> 格心) 实测在 55~215px
#   之间浮动、跨度 134px 远大于行间距中位 68px, 单根血条**无法唯一确定行**。
#   改成"每格最多一根血条 + 全局代价最小"后 (266 干净帧实测):
#     cur 逐根最近邻   精确率 0.9182 / 召回 0.9088
#     gfree 全局分配   精确率 0.9326 / 召回 0.9292   <- F1 +1.8pp
#     oracle 上限      精确率 0.9531 / 召回 0.9495   (代价直接吃真值格心, 不可部署)
#   分配不上的血条**直接丢弃** (宁可漏采不可采错), 不硬塞进某个格位。

def _cell_distance(row: int, column: int, point) -> float:
    """点到指定棋盘格心的**归一化**距离 (与 `board_position` 同一套公式, 便于 position_error 可比)。"""
    rows = default_config.BOARD_GRID_ROWS
    idx = row - 1
    height = rows[min(idx + 1, len(rows) - 1)][1] - rows[max(idx - 1, 0)][1]
    height /= 2 if 0 < idx < len(rows) - 1 else 1
    x0, cy, step = rows[idx]
    cx = x0 + (column - 1) * step
    return hypot((point[0] - cx) / step, (point[1] - cy) / height)


def cell_dict(row: int, column: int, point=None) -> dict:
    """构造与 `board_position` 同形的格位 dict (全局分配用, 不经过"最近格心"判定)。

    `point` 给定时顺便算出归一化 `position_error` (供下游比较两路定位的优劣, 取误差更小者),
    不给则记 0.0。
    """
    rows = default_config.BOARD_GRID_ROWS
    x0, cy, step = rows[row - 1]
    cx = round(x0 + (column - 1) * step)
    err = round(_cell_distance(row, column, point), 3) if point is not None else 0.0
    return {"row": row, "column": column, "cell_center": [cx, cy],
            "own_row": row - 4 if row >= 5 else None, "position_error": err}


def _theory_priors() -> dict:
    """血条下沿 -> 格心的理论先验 (不学习, 纯几何; 实测与学习值差 <=7px)。

    board: 格心 y - BOARD_BAR_TO_CELL_Y(100)
    bench: 备战席格心 y - ASSIGN_BENCH_DY(63)   (实测 bench 血条 y2 中位 689, 格心 754)
    """
    rows = default_config.BOARD_GRID_ROWS
    dy = int(default_config.BOARD_BAR_TO_CELL_Y)
    priors = {}
    for row in range(1, len(rows) + 1):
        priors[("board", row)] = float(rows[row - 1][1] - dy)
    bench_cy = float(default_config.BENCH_SLOT_CENTERS[0][1])
    priors[("bench", 0)] = bench_cy - ASSIGN_BENCH_DY
    return priors


def assign_bars_to_cells(bars, *, own_rows_only: bool = True) -> dict:
    """把一帧的所有血条**全局最优**地分配给格位; 返回 `{格位 key: 血条下标}`。

    Args:
        bars: `[(cx, y2, area), ...]`, area ∈ {"board","bench"}; 血条中心 x 与下沿 y。
        own_rows_only: 只考虑**己方半场**棋盘行 (绝对行 >= 5)。默认 True —— 内存真值只覆盖
            己方半场 + 备战席, 敌方半场的血条映射到哪一格都无法判定, 留着只会污染。

    Returns:
        `{("board", row, col): bar_index}` / `{("bench", slot, 0): bar_index}`
        (bench 用 3 元 key 与 board 同形, 方便下游统一处理)。
        没有分配到格位的血条**不出现在结果里**。

    实现说明: 用匈牙利算法求全局最小代价匹配。项目依赖里**没有 scipy**, 所以这里自己实现
    (代价矩阵很小: 候选格 <= 37, 血条通常 <= 15, O(n^3) 完全够快, 单帧 < 1ms)。
    """
    if not bars:
        return {}
    rows = default_config.BOARD_GRID_ROWS
    centers = default_config.BENCH_SLOT_CENTERS
    priors = _theory_priors()

    cands: list[tuple] = []
    for row in range(1, len(rows) + 1):
        if own_rows_only and row < 5:
            continue
        for col in range(1, int(default_config.BOARD_COLUMNS) + 1):
            cands.append(("board", row, col))
    for slot in range(1, len(centers) + 1):
        cands.append(("bench", slot, 0))
    if not cands:
        return {}

    big = 1e6
    cost = [[big] * len(bars) for _ in range(len(cands))]
    for i, key in enumerate(cands):
        if key[0] == "board":
            x0, _cy, step = rows[key[1] - 1]
            cell_cx = float(x0) + (key[2] - 1) * float(step)
            prior = priors[("board", key[1])]
            x_tol = ASSIGN_X_TOL
        else:
            cell_cx = float(centers[key[1] - 1][0])
            prior = priors[("bench", 0)]
            x_tol = ASSIGN_BENCH_X_TOL
        for j, (bcx, by2, area) in enumerate(bars):
            if area != key[0]:
                continue
            if not _bar_ok_for_assign(area, by2):
                continue            # 竖向范围闸: 一眼不是己方棋子的血条, 不参与分配
            dx = abs(float(bcx) - cell_cx)
            if dx > x_tol:
                continue
            c = dx + ASSIGN_DY_W * abs(float(by2) - prior)
            if c <= ASSIGN_MAX_COST:
                cost[i][j] = c

    pairs = _hungarian(cost, big)
    return {cands[i]: j for i, j in pairs if cost[i][j] < big / 2.0}


def _hungarian(cost: list[list[float]], big: float) -> list[tuple[int, int]]:
    """匈牙利算法 (最小代价完美匹配), 返回 `[(行下标, 列下标)]` 里代价 < big 的那些.

    经典 O(n^3) 势函数实现 (行数 <= 列数时按列补虚拟行)。矩阵尺寸很小 (<= 37 x <= 15),
    纯 Python 单帧 < 1ms, 不值得引入 scipy 依赖。
    """
    n = len(cost)
    m = len(cost[0]) if n else 0
    if not n or not m:
        return []
    transposed = n > m
    if transposed:
        cost = [list(row) for row in zip(*cost)]
        n, m = m, n
    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    out = []
    for j in range(1, m + 1):
        if p[j] and cost[p[j] - 1][j - 1] < big / 2.0:
            i, jj = p[j] - 1, j - 1
            out.append((jj, i) if transposed else (i, jj))
    return out

