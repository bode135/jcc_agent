# 多层级血条识别 & Hero 识别

本文讲两套识别的**判定链**：一是棋盘/备战席上棋子的血条（由此拿到位置、星级、身份），
二是小小英雄（Hero）的定位与敌我判定。两者都只靠截图，不读内存、不注入。

- 坐标一律 **1920×1080** 基准；其它分辨率先用 `capture.screen.resize()` 等比缩放。
- 涉及文件：`recognize/board.py`（血条 + 徽记 + 星级）、`recognize/grid.py`（血条→格位）、
  `recognize/hero.py`（英雄入口）、`recognize/draft_hero.py`（英雄判据本体）。
- 测量数据（颜色区间、尺寸分布、逐图验收数字）见 `docs/图色口径.md`，本文只讲方案与代码。

---

## 一、多层级血条识别

### 1.1 为什么要分这么多层

单独一层的召回和精度都不够，把它们串起来才稳：

| 单层方案 | 问题 |
| --- | --- |
| 颜色掩码找绿条 | 召回高（残血、分段渲染都还在），但草地色块、HUD 装饰、绿色特效全会命中 |
| 模板匹配找血条 | 准备阶段很准，但残血/被遮挡/战斗分段时匹配度掉到阈值以下，漏检 |
| 只做"并集"不做校验 | 召回够了，但每帧都会带出十几根假血条，下游格位分配被污染 |

所以真正的骨架是：**两路召回（宁多勿漏）→ 碎片合并 → 用"徽记"这个 UI 锚点做正样本校验
（把假血条清零）→ 徽记颜色直接给出星级 → 全帧全局最优分配格位 → 头像匹配身份**。

### 1.2 总流程

```
帧 (1920x1080 RGB)
  │
  ├─[0] 裁搜索区  BAR_REGIONS                        board.py:30
  │
  ├─[1] 候选条    color 掩码 ┐                       board.py:46
  │                template  ┼─ union 取并集+去重     board.py:85 / :162
  │
  ├─[2] 碎片合并  _merge_bar_pieces()                 board.py:122
  │
  ├─[3] 徽记校验  star_badge() 有徽记? ──否──┐        board.py:553
  │                    │是                  │
  │                    │        是小小英雄? ──是──> legend（可过滤）
  │                    │             │否
  │                    │             └──> 丢弃 dropped_no_badge
  │
  ├─[4] 星级      badge 颜色(主) / bar_stars 颜色面积(备)   board.py:553 / :479 / :605
  │
  ├─[5] 配格      assign_bars_to_cells() 全局最小代价      grid.py:316
  │
  ├─[6] 身份      头像裁剪 -> matcher 模板库多尺度匹配      board.py:319
  │
  └─[7] 去重      _dedupe_units() 同格位只留一条           board.py:440
```

入口函数：`recognize/board.py:218 extract_units(img, bar_method=None, ...)`。

### 1.3 第 0 层：搜索区

`recognize/board.py:30`

```python
BAR_REGIONS = {
    "board": (382, 75, 1564, 645),    # 棋盘
    "bench": (382, 645, 1470, 720),   # 备战席
}
```

- 棋盘区排除顶栏（回合/装备/金币，y<75）和右侧玩家列表（x>1564）。
- 备战席右界刻意放在 **1470**：早期为 1351 时**第 9 格的血条整个落在区外**，永远扫不到；
  放宽到 1470 覆盖第 9 格（x≈1375–1440），又仍在右侧 HUD（x>1490）之前。

换分辨率或 UI 改版后，第一件事就是量这两个框（用 `overlay_info --img` 看叠加图里的血条框
是否被裁掉）。

### 1.4 第 1 层：候选条

**颜色法** `recognize/board.py:46 _find_bars_color()`

| 判据 | 值 |
| --- | --- |
| 绿色填充 | `g > 170` 且 `g - r > 50` 且 `g - b > 50` |
| 橙色填充（观战/敌方配色，可选） | `r > 150` 且 `r - g > 45` 且 `g - b > 15` 且 `b < 130` |
| 尺寸闸 | 宽 44–107、高 4–15（满血约 76px，允许残血变短） |
| 上沿边框（仅绿色） | 血条上方 5px 内需有连续深色边框（`max(RGB) < 90` 的行占比 ≥ 0.8），滤掉绿色模型/特效 |

橙色分支是给观战视角准备的（观战方之外的单位血条是橙棕色，只找绿色会一根都检不出），
默认关：`config.BAR_INCLUDE_ENEMY_COLOR = False`。

**模板法** `recognize/board.py:85 _find_bars_template()`

- 模板：`recognize/templates/health_bar.png`（满血血条截图）
- `cv2.matchTemplate(..., TM_CCOEFF_NORMED)`，峰值 ≥ `BAR_MATCH_THRESHOLD = 0.65`
- NMS：`BAR_NMS_DIST = 50`（模板宽 83，相邻血条间距 > 50）
- 满血模板对残血血条匹配度会下降 → 主要覆盖准备阶段

**调度** `recognize/board.py:162 _find_bars()`

```python
method = (bar_method or config.BAR_DETECT_METHOD or "union").lower()
```

- `"color"` / `"template"`：单跑
- `"union"`（默认）：两路并集，中心距 ≤ 20px 视为同一根去重

并集依据（25 帧统计）：单跑模板时**叠放的两颗棋子只认出一根**；并集 68 根 > 模板 67 根，
且逐帧从不更差（并集多 1 帧、模板多 0 帧、同 24 帧）。

注意：**不要在叠加了标注框的图上评估颜色法** —— 框线会把 color 法的 4 根打到 1 根。

### 1.5 第 2 层：碎片合并

`recognize/board.py:122 _merge_bar_pieces()`

战斗/野怪血条是**分段渲染**的（每格之间有深色分隔符），颜色法会沿分隔符把它切成几十片：
`board_green_model.png` 里一根野怪大血条被切成 **25 片**，每片单独看都不带徽记，于是全被
"无徽记即误检"规则丢掉 —— 一根真血条都不剩。合并后再判徽记，徽记就在合并后血条的左端。

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `BAR_MERGE_DY` | 4 | 同一根被切开的碎片，竖向中心偏差上限 |
| `BAR_MERGE_GAP` | 12 | 碎片之间的横向空隙上限（分隔符约 2–6px） |

合并是"排序后贪心 + 最多 3 轮收敛"：一次合并可能让 A 同时贴住 B 和 C，多跑几轮才稳定。
相邻两格棋子血条横向间距 ≥ 40px（格宽 78），不会被误合并。

### 1.6 第 3 层：徽记锚点（正样本校验）

`recognize/board.py:553 star_badge()`

这是整套方案的核心：**每个棋子血条左侧必定挂一枚星级徽记**（1★ 铜色 chevron / 2★ 银蓝菱形 /
3★ 金色 chevron）。徽记是 UI 元素，形状、色相、尺寸都固定，比"绿色长条"稳定得多，所以它同时
干三件事：

1. 判定这根是不是真棋子血条（没有徽记 → 误检）
2. 直接给出星级（取代数条纹，条纹在战斗帧会被分段/特效压盖）
3. 给出可画可核对的锚点（overlay 把那枚框套出来）

**搜索窗**：`BADGE_WIN = (-40, -12, 2, 12)`，即相对血条 `(x1,y1,x2,y2)` 的
`x1-40 … x1+2` × `y1-12 … y2+12`。

**三重筛**（全部通过才算命中）：

| 筛 | 常量 | 值 |
| --- | --- | --- |
| 颜色 | `BADGE_COLORS`（HSV，OpenCV H 0–179） | 3★ 金 `(26,140,110)-(42,255,255)`；2★ 银蓝 `(85,20,120)-(118,120,255)`；1★ 铜 `(8,90,110)-(25,255,255)` |
| 面积（**按星级分开**） | `BADGE_AREA_RANGE_BY_STARS` | 1★ `(40,165)`、2★ `(150,280)`、3★ `(60,500)`；兜底 `BADGE_AREA_RANGE=(40,600)` |
| 尺寸 | `BADGE_H_RANGE=(8,30)`、`BADGE_W_RANGE=(6,32)` | 实测 8×12 ~ 23×26 |
| 宽高比 | `BADGE_ASPECT_RANGE=(0.45,1.8)` | 三个模板 0.95 / 1.12 / 1.39；扁条 3.2 / 4.3 被排除 |
| 竖向对齐 | `BADGE_DY_MAX=12` | 徽记中心与血条中心 dy 实测 0–6 |
| 位置 | 右边界 ≤ `x1 + 4` | 徽记必须在血条左侧 |

面积必须按星级分开的原因：旧版用一条宽区间取"面积最大"的连通域，1★ 棋子的真徽记面积只有
55–128，会被**相邻棋子身体/装备图标拼成的大连通域**（面积 201）压过 → 框画到别的棋子上，
视觉上就是"框歪了/向左倾斜"（2026-11-15 用户实测乐芙兰）。按星级分区间后，1★ 只接受
40–165，假候选 201 直接被排除。

**白像素区分英雄**：`badge_white_count()`（`board.py:534`）+ `BADGE_NUMBER_WHITE = 25`。
小小英雄血条左边是**白色等级数字**，不是彩色 chevron：

| 对象 | 框内白像素（V≥195 且 S≤45） |
| --- | --- |
| 英雄等级数字 | 52–73 |
| 1★ 铜 chevron | 0 |
| 2★ 银蓝 chevron | 0（银蓝 S 中位 87，不算白） |

所以 `badge_white_count >= 25` 就判"这是等级数字框 → 小小英雄"，不把英雄当 2 星棋子。

**丢弃规则**：`BAR_REQUIRE_BADGE = True`（`board.py:518`）。既没徽记、又不是小小英雄的血条，
判为误检丢弃，并记进 `last_stats()["dropped_no_badge"]`（诊断用）。实测非对局帧的假血条
**25 → 0**，真对局帧一根不少。

> 为什么 `extract_units` 的返回值里不能塞诊断键：下游 `equipment.extract_board_equips(units=...)`
> 会把返回值当"区域 → 单位列表"遍历，多一个非列表键就 `AttributeError`。诊断信息一律走模块级
> `_LAST_STATS` + `last_stats()`。

### 1.7 第 4 层：星级判定

主路 `star_badge()`（徽记颜色）→ 备路 `bar_stars()`（`board.py:479`）→ 合并 `bar_stars_hybrid()`
（`board.py:605`）。

`bar_stars()` 不是"数血条条纹"，而是**左侧 32px 窗口内的颜色面积计数**：

```python
gold   = (r > 180) & (g > 165) & (b < 120)
silver = (g > 150) & (b > 170) & (b - r > 20)
bronze = (r > 145) & (g > 85) & (g < 180) & (b < 130) & (r - g > 30)
for stars, mask in ((3, gold), (2, silver), (1, bronze)):
    if mask.sum() >= 12:
        return stars
return None    # 没有徽记就返回 None，绝不猜 1 星
```

输出里同时带：

- `stars`：1/2/3 或 None
- `stars_source`：`"badge"`（徽记读出的）/ `"stripes"`（备路）/ `None`
- `badge_box`：`[x1,y1,x2,y2]`（已从 cv2 的 `(x,y,w,h)` 归一成画布口径）

### 1.8 第 5 层：血条 → 格位（全局最优分配）

`recognize/grid.py:316 assign_bars_to_cells(bars, own_rows_only=True)`

逐根最近邻的致命缺陷：同一帧 3 个棋子只检出 2 根血条时，必然把某根判给邻居已占的格位；
而且 dy（血条下沿 → 格心）实测 55–215px 浮动，跨度 134px > 行间距 68px，单根血条无法定行。

改成"**每格最多一根 + 全帧代价最小**"的二分图匹配（自己实现的匈牙利算法，项目不依赖 scipy；
候选格 ≤ 37、血条通常 ≤ 15，O(n³) 单帧 < 1ms）。

代价 = 横向偏差（`ASSIGN_X_TOL=45`，备战席 `ASSIGN_BENCH_X_TOL=64`）+ 纵向偏差（相对理论先验
`格心 y − BOARD_BAR_TO_CELL_Y(100)`）× `ASSIGN_DY_W=1.0`；超出 `ASSIGN_MAX_COST=110` 的边
直接不可用。

实测（266 干净帧）：精确率 **0.9182 → 0.9326**，召回 **0.9088 → 0.9292**。

分配不上格位的血条**不猜**：

| 情况 | `cell_source` | 行为 |
| --- | --- | --- |
| 全局分配到棋盘格 | `"global"` | 用格心 + `BOARD_BAR_TO_CELL_Y` 做落点 |
| 没分配到、但最近邻是己方行 | `"nearest"` | 退回逐根最近邻（`bar_to_cell`，`grid.py:201`） |
| 最近邻落在敌方半场 / 配不上 | `"unassigned"` | `location=None`，只给 `cell_anchor` |

`own_rows_only=True` 只考虑绝对行 ≥ 5（己方半场）+ 备战席 9 格；敌方半场血条无法可靠归格，
留着只会污染。

### 1.9 第 6 层：身份

`recognize/board.py:319`附近：

- 以血条中心 `cx` 为基准，向下裁头像：`cx-52 … cx+52`、`y2+3 … y2+92`
  （`PORTRAIT_HALF_W=52`、`PORTRAIT_H=92`）
- `match(portrait)` 走 `units/matcher.py`：portrait_match（138 变体）优先，`units.db` 原画兜底
- **身份以 YOLO 为准，头像匹配置信度只做交叉校验**（实测 138 变体对 8 根血条命中 3 根、
  其中正确 2 根；名字置信度 0.4–0.6 常为 None，不猜）

性能开关 `match_cells`：身份匹配是全棋子库多尺度扫描，单只 **145ms**，9 只 1.3s，占
`extract_units` 总耗时 73%。传 `match_cells={"board,2,4"}` 可只认关心的格位（1.8s → 0.5s），
其它格名字留 `None`，位置/血条/星级照常给。

### 1.10 第 7 层：去重与收口

`recognize/board.py:440 _dedupe_units()`

- 按格位 key 精确去重：board 用 `(row, column)`、bench 用 `slot`；无格位的条目不参与去重
- 优先级：有血条（位置最准）> 有 YOLO box（名字最准）> 其它
- 合并时补齐名字、星级、血条框，不重复检两次

纯图色工程没有 SAM3 框，所以**不做血条 mapping 的冲突修正**（历史版本会用 SAM3 修瘦长棋子的
±1 行偏移；现在只有血条一路位置来源，没有第二路可比）。

### 1.11 参数总表

| 常量 / 配置 | 值 | 位置 |
| --- | --- | --- |
| `BAR_REGIONS` | board (382,75,1564,645) / bench (382,645,1470,720) | `board.py:30` |
| `BAR_MATCH_THRESHOLD` | 0.65 | `board.py:42` |
| `BAR_NMS_DIST` | 50 | `board.py:43` |
| `BAR_MERGE_DY` / `BAR_MERGE_GAP` | 4 / 12 | `board.py:118` |
| `BAR_REQUIRE_BADGE` | True | `board.py:518` |
| `BADGE_WIN` | (-40, -12, 2, 12) | `board.py:502` |
| `BADGE_AREA_RANGE_BY_STARS` | {1:(40,165), 2:(150,280), 3:(60,500)} | `board.py:508` |
| `BADGE_H_RANGE` / `BADGE_W_RANGE` | (8,30) / (6,32) | `board.py:510` |
| `BADGE_ASPECT_RANGE` | (0.45, 1.8) | `board.py:515` |
| `BADGE_DY_MAX` | 12 | `board.py:516` |
| `BADGE_COLORS` | 见 1.6 | `board.py:527` |
| `BADGE_NUMBER_WHITE` | 25 | `board.py:550` |
| `PORTRAIT_HALF_W` / `PORTRAIT_H` | 52 / 92 | `board.py:37` |
| `BAR_DETECT_METHOD` | `"union"` | `config/config.py:172` |
| `BAR_ASSIGN_GLOBAL` | True | `config/config.py:181` |
| `BAR_INCLUDE_ENEMY_COLOR` | False | `config/config.py:186` |
| `BOARD_BAR_TO_CELL_Y` | 100 | `config/config.py:145` |
| `ASSIGN_X_TOL` / `ASSIGN_BENCH_X_TOL` | 45 / 64 | `grid.py`（模块常量） |
| `ASSIGN_DY_W` / `ASSIGN_MAX_COST` | 1.0 / 110 | `grid.py`（模块常量） |

### 1.12 实测基线

单帧 1920×1080（GPU CUDA）。注意 `tests/images/*.png` 里有些图原生尺寸不是 1920×1080
（如 `board_ingame.png` 是 1219×686），**必须先 `capture.screen.resize()`**，否则判据全偏：

| 图 | board | bench | 徽记丢弃 | 耗时 |
| --- | --- | --- | --- | --- |
| `board_ingame.png` | 5 | 3 | 0 | 1672ms |
| `reward_balls.png` | 4 | 3 | 0 | 1163ms |
| `bench_forge.png` | 3 | 0 | 1（绿色藤蔓） | 570ms |
| `augment_choose.png` | 0 | 0 | 5（卡片遮罩假条） | 67ms |
| `board_green_model.png` | 0 | 0 | 20（分段血条合并后仍非棋子） | 69ms |

不传 `match_cells` 时耗时构成：身份匹配约 1.2s（~75%）+ 其余 ~0.4s。

### 1.13 故障定位：症状 → 哪一层

| 症状 | 先查哪层 | 怎么查 |
| --- | --- | --- |
| 整帧一根血条都没有 | 0/1 层 | 区域是否裁掉（叠图画框）；`BAR_DETECT_METHOD` 是否被改成 `template` |
| 漏检某一格（尤其第 9 格） | 0 层 | `BAR_REGIONS["bench"]` 右界 |
| 一根血条被拆成多根 | 2 层 | 是否分段渲染；`BAR_MERGE_DY/GAP` |
| 血条框在但星级是 `?` | 3/4 层 | `tools.probe_star_badge.py` 看候选面积/宽高比 |
| 星级框画到相邻棋子上 | 3 层 | 候选里"面积最大的假连通域"是否在允许区间内 |
| 血条判给了相邻格 | 5 层 | `last_stats` + `probe_star_badge`；全局分配是否被关 |
| 名字是 `?` 或 None | 6 层 | portrait_match 命中数；是否该传 `match_cells` |
| 同一棋子出现两次 | 7 层 | `location` 是否 None（无格位不判重） |

### 1.14 重标定步骤（换分辨率 / UI 改版）

1. 截一张**准备阶段**干净帧（1920×1080），放 `tests/images/`。
2. 跑 `python -X utf8 -u -m tools.probe_star_badge --img <图> --crop`，确认每根血条的徽记
   bbox、面积、宽高比、右侧 gap 都在上表区间内。
3. 若面积整体偏移（UI 缩放），按 1★/2★/3★ 分别重测 min/max，改 `BADGE_AREA_RANGE_BY_STARS`；
   宽高比变则改 `BADGE_ASPECT_RANGE`。
4. 若血条整体变高/变矮，改颜色法的尺寸闸（`44 <= w <= 107 and 4 <= h <= 15`）与
   `BADGE_H_RANGE`。
5. 跑 `python -X utf8 -u -m tools.selftest_recognize`，看 board/bench 计数与基准是否一致。
6. 再跑 `tools.selfcheck --full`。

---

## 二、Hero（小小英雄）识别

### 2.1 为什么不用"找血条 + 看名牌"

老办法（`board._find_bars` + `_has_name_text`）两头不讨好：

- **假阳性**：好友列表头像、聊天条、底栏都能满足"白色密集行"的条件 → 实测 3 检出 2 假
- **假阴性**：战斗帧名牌常被棋子/特效遮住 → 认不出
- 慢：150–200ms/帧

新办法反过来锚定，稳得多，也快（**10–18ms/帧**）。

### 2.2 判据（用户口径）

> 小小英雄的"血条"不是一个整条，而是**等级数字模板右边一点点**的绿色或红色小段。
> **绿色 = 我方 hero，红色 = 敌方 hero**，就是用来区别敌我的。

所以判据是：**白色等级数字块 + 它右边一小段必须是红/绿**，两者同时成立才是 hero。

### 2.3 流程

```
RGB 帧
  │
  ├─[A] 裁对局区域  hero.GAME_REGION = (150,250,1550,820)      hero.py:33
  │      （排除顶栏 y<250 / 底栏 y>820 / 右侧玩家列表 x>1550 的 UI 数字与头像血条）
  │
  ├─[B] 找白色数字块  _white_blocks()                          draft_hero.py:198
  │      白色掩码 (200,200,200)-(255,255,255) → 连通域
  │      尺寸闸 7–24 × 11–28、面积 ≥ 40
  │      暗底闸：外扩 6px 的暗像素均值 ≥ 100
  │
  ├─[C] 判敌我  _kind_of()                                     draft_hero.py:180
  │      取数字块右侧 x+w+4 … x+w+22、中心行 ±6 的平均 RGB
  │      绿: g > r+50 且 g > b+50 且 g >= 110  -> "mine"
  │      红: r > g+50 且 r > b+40 且 r >= 120  -> "enemy"
  │      都不满足 -> 丢弃（不是 hero）
  │
  ├─[D] 读等级  match_level()                                  draft_hero.py:136
  │      数字块整体拉伸到 16×22 → 与 templates/hero_level/<1..10>.png 做余弦相似度
  │
  ├─[E] 落点    hero_xy() = 数字块中心 + (DRAFT_HERO_LEVEL_DX, DY) = +(60.0, 54.5)
  │
  └─[F] 去重    距离聚类 40px（DRAFT_HERO_LEVEL_TOL），分数高的优先，同分取更靠左
```

入口：`recognize/hero.py:57 detect(img, region=None, max_x=None)`。
它做的是"裁区域 + 调 `draft_hero.hero_blocks()` + 坐标回加偏移 + 尺寸复核 + 排序 + 打 `cx/cy`"。

### 2.4 关键点：为什么两个墙要这么砌

**暗底闸**（`DARK_PAD=6`、`DARK_MEAN_MIN=100`）：等级数字是白字压在深色底（血条槽）上。
没有深色底的白色块（棋子模型高光、UI 白字）会被滤掉。**没有这一层，白块数量涨 3–5 倍。**

**色差 + 亮度双条件**（`MINE_MIN=110` / `ENEMY_MIN=120`）：只判色差会把暗色地面判成血条 ——
实测暗地面 `rgb(19,55,26)`、`(16,44,18)`、`(34,69,42)` 的 g 都比 r/b 高 30–50，只卡
`g > r+50` 时全部命中。加上亮度下限才干净。

**余弦相似度、不去均值**（`match_level`）：等级数字模板空白多，去均值相关会让窄笔画「1」
和任何块都虚高 —— 实测把 4 级判成 1 级。余弦只奖励笔画重叠，不奖励"共同的黑背景"。

**整体拉伸、不保宽高比**（`digit_mask`）：保持宽高比 + 居中的话，「1」只占中间一条，
与「4」误配 0.80；统一拉伸到 16×22 后形状差异才可比。

### 2.5 与棋盘识别的接线（hero 锚点 → legend 判定）

`recognize/board.py:218 extract_units()` 里：

1. 开头跑 `hero.detect(arr)` 拿 `hero_hits`（try/except：英雄路失败只打日志，不许拖垮棋子识别）
2. 血条循环里 `hero_hit = _hero_near(bar, hero_hits)`（`board.py:396`）：
   **数字块右沿**落在血条左沿 `x1-40 … x1+44`、**数字块中心与血条中心 dy ≤ 16** → 认定配对
   （不要求整条血条对齐，因为英雄血条本来就只有一小段）
3. 命中 hero 或 `level_number`（框内白像素 ≥ 25）时：
   - 清空 `badge_stars` / `rank`（数字不是星级，别把英雄当 2 星棋子）
   - `legend = True`
4. unit 上带出：`hero_kind`（mine/enemy）、`hero_level`、`hero_level_score`、`hero_box`、
   `hero_pos`、`legend`
5. `include_legend=False`（默认）时直接过滤掉 hero，不当作棋子

overlay 侧：`OVERLAY_SHOW_HERO=True` 时画绿框（我方）/ 红框（敌方）+ `小小英雄 我方 Lv5` 牌，
并在落点画半径 7 的小圈；YOLO legend 紫框与英雄框重叠时跳过（同一个英雄不画两遍）。

### 2.6 参数总表

| 常量 / 配置 | 值 | 位置 |
| --- | --- | --- |
| `GAME_REGION` | (150, 250, 1550, 820) | `hero.py:33` |
| `BLK_MIN_SIDE` / `BLK_MAX_SIDE` | 6 / 30 | `hero.py:36` |
| `LEVEL_WHITE_LO` | 230 | `draft_hero.py:58` |
| `BLK_W_MIN/MAX` `BLK_H_MIN/MAX` `BLK_AREA_MIN` | 7/24、11/28、40 | `draft_hero.py:63` |
| `DARK_PAD` / `DARK_MEAN_MIN` | 6 / 100 | `draft_hero.py:68` |
| `SEG_DX0/DX1/DY`、`SEG_MIN_W` | 4/22/6、8 | `draft_hero.py:72` |
| `MINE_DC/MINE_MIN` | 50 / 110 | `draft_hero.py:76` |
| `ENEMY_DC/ENEMY_DB/ENEMY_MIN` | 50 / 40 / 120 | `draft_hero.py:77` |
| `LEVEL_TPL_W/H` | 16 / 22 | `draft_hero.py:57` |
| `DRAFT_HERO_USE_LEVEL_MATCH` | True | `config/config.py` |
| `DRAFT_HERO_LEVEL_DX` / `DY` | 60.0 / 54.5 | `config/config.py` |
| `DRAFT_HERO_LEVEL_TOL` | 40.0 | `config/config.py` |
| `DRAFT_HERO_SEG_MAX_X` | 1600 | `config/config.py` |

### 2.7 实测

| 图 | 命中 | 明细 |
| --- | --- | --- |
| `board_ingame.png` | 1 我方 | Lv5，`numblk=(403,502,11,16)`，`rgb=(62,133,52)`，落点 (468.5,564.5)，18.7ms |
| `reward_balls.png` | 1 我方 | Lv4，相似度 0.871，14.0ms |
| `bench_forge.png` | 1 我方 | Lv6，相似度 0.889 |
| `augment_choose.png` | 0 | 卡片遮罩挡住 |
| `board_green_model.png` | 0 | 无英雄 |
| 实机帧 | 1 我方 | Lv6，`(407,541,11,17)` |

全帧不裁区域时（直接调 `draft_hero.hero_blocks`）会多出两个假候选：
顶部玩家列表 `enemy Lv6 @(1367,98)`、底栏 `enemy Lv1 @(574,947)` —— 这就是 `GAME_REGION`
必须存在的原因。

### 2.8 加等级模板 / 重标定

1. **补模板**：截 1920×1080 帧里纯白等级数字，做成 16×22 灰度 PNG（白字 255、底 0），
   命名 `recognize/templates/hero_level/<等级>.png`；缺失等级会让 `level=None`，hero 仍能
   识别（靠色段），只是等级读不出。
2. **换分辨率/UI 缩放**：先量白数字块实际尺寸，改 `BLK_W_MIN/MAX`、`BLK_H_MIN/MAX`；
   再量到血段距离，改 `SEG_DX0/DX1`；最后用实机帧核对 `DRAFT_HERO_LEVEL_DX/DY` 落点
   是否压在英雄身上（overlay 画的小圈就是干这个的）。
3. **敌我判反或漏判**：打印 `_kind_of` 返回的 `rgb` 均值，对照 `MINE_MIN`/`ENEMY_MIN`。
4. 回归：`python -X utf8 -u -m tools.selftest_recognize` + `scripts/overlay_info --img`。

---

## 三、两套判据怎么区分（同一位置，两种 UI）

棋子和英雄的血条左边都挂着一个"小方块"，长得像但完全不是一回事：

| | 棋子血条左侧 | 小小英雄血条左侧 |
| --- | --- | --- |
| 内容 | **彩色星级 chevron**（1★ 铜 / 2★ 银蓝 / 3★ 金） | **白色等级数字**（1–10） |
| 框内白像素 | 0 | 52–73（`BADGE_NUMBER_WHITE=25` 为界） |
| 判据代码 | `star_badge()` -> `BADGE_COLORS` | `badge_white_count()` + `hero.detect()` |
| 右侧血段 | 绿色/橙色整条血条（长，76px 量级） | 绿色(我)/红色(敌)一小段（约 8–18px） |
| 含义 | 棋子星级 | 英雄等级 + 敌我 |
| 拿它做什么 | 血条正样本校验 + 星级 | hero 定位 + `legend` 过滤 |

一次 `extract_units` 里两者是**同一条流水线**判的：先 `star_badge`，再用白像素数把"数字框"
从"徽记框"里摘出来，最后用 `hero.detect` 的锚点做二次确认。

---

## 四、验证与调试

```bash
# 1) 徽记/星级逐候选打印（第 3、4 层最直观）
python -X utf8 -u -m tools.probe_star_badge --img tests/images/board_ingame.png
python -X utf8 -u -m tools.probe_star_badge --img tests/images/board_ingame.png --crop   # 落放大图

# 2) 全链路识别自测（OCR / YOLO / 血条 / 装备 / 球 / phase / augment）
python -X utf8 -u -m tools.selftest_recognize

# 3) 叠加图（看框画在哪、英雄绿/红框对不对）
python -X utf8 -u -m scripts.overlay_info --img tests/images/board_ingame.png --seconds 1
#   -> tempdir/overlay_info_board_ingame.png / _mix.png

# 4) 结构化输出（看 stars 列、legend、hero_level 落到文本里的样子）
python -X utf8 -u -m scripts.extract_img_info --img tests/images/board_ingame.png --format table

# 5) 全工程自检
python -X utf8 -u -m tools.selfcheck --full
```

代码里直接用：

```python
from PIL import Image
from capture.screen import resize          # 非基准尺寸必须先缩放到 1920x1080
from recognize import board, hero

img = Image.open("tests/images/board_ingame.png").convert("RGB")
img = resize(img)                          # 该测试图原生 1219x686, 不缩放会整帧判错
res = board.extract_units(img, include_legend=False)
for area in ("board", "bench"):
    for u in res[area]:
        print(area, u.get("location"), u.get("name"), u.get("stars"),
              u.get("stars_source"), u.get("badge_box"))
print("被徽记规则丢掉的血条:", board.last_stats()["dropped_no_badge"])

for h in hero.detect(img):
    print(h["kind"], h["level"], h["level_score"], h["numblk"], (h["x"], h["y"]))
```

> **尺寸是硬前提**：所有判据（颜色法尺寸闸、徽记面积/尺寸区间、头像裁剪 52×92、等级数字块
> 7–24×11–28）都是按 1920×1080 标定的。`tests/images/board_ingame.png` 原生只有 1219×686，
> 直接喂给 `extract_units` 会得到完全不同的结果（棋子数量、名字全 None、hero 0 命中）。
> `scripts/overlay_info.py` / `scripts.extract_img_info.py` 内部已经做了 `resize`，自己写脚本时
> 别漏。