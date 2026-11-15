# 组合成 Agent 的 prompt

一句话：**把一帧画面变成 Agent 能直接读的四张 Markdown 表**（商店 / 场上棋子 / 备战席棋子 / 装备栏），
外加阶段、金钱、等级、各玩家血量这些决策必需的头信息。

```bash
# 离线图 -> 直接打到终端
python -X utf8 -u -m scripts.frame_prompt --img tempdir/frame_4-5.png

# 落盘给 LLM 用（不带玩家血量，省 token）
python -X utf8 -u -m scripts.frame_prompt --img tempdir/frame_4-5.png --no-players --out prompt.md

# 实机截一帧（只读，不点不拖）
python -X utf8 -u -m scripts.frame_prompt --ld-index 0

# 机器可读
python -X utf8 -u -m scripts.frame_prompt --img frame.png --format json
```

识别耗时：**热帧约 4~6s**（整图 OCR 约 1.8~2.2s 占大头，其余是装备与棋子），冷启动首帧 +3~5s。

## 1. 为什么要有这一层

识别层吐的是嵌套 dict（`extract_info.extractor` + `recognize.board` + `recognize.equipment` 三路），
直接丢给 LLM 有两个问题：

1. **字段噪声**：`bar` 是血条方框、`rect` 是图标框、`center` 是像素坐标 —— 这些是给人画框用的，
   对决策无意义，却占 token。
2. **一个棋子被拆成两条记录**：YOLO 给名字与格位、血条法给星级与携带装备，必须按几何关联起来才是
   "一只棋子"。

`extract_info/prompt.py` 就是做这两件事：**合并 + 只留决策字段**。它不参与识别，也不做任何猜测
（查不到棋子库就写 `-`，认不出名字就写 `?`）。

## 2. 输出长什么样

```markdown
# 对局快照 · 4-5

- 倒计时 25s · 等级 7 (经验 31/56) · 金币 87 · 买经验 4 · 刷新 2

- 识别到: 场上 5 / 备战席 9 / 装备栏 2 / 商店 5

## 商店 (自左向右槽位 1-5)

| 槽位 | 棋子 | 价格 | 攻距 | 羁绊 | 职业 | 定位 |
| :--- | :--- | ---: | :--- | :--- | :--- | :--- |
| 1 | 阿卡丽 | 1 | 1 (前排) | 地狱火 | 魔战士/狂战士 | 物理战士 |
...

## 场上棋子 (格位 r1~r4 = 己方, -rN = 敌方)

| 格位 | 棋子 | 置信度 | 星级 | 羁绊 | 职业 | 定位 | 携带装备 |
| :--- | :--- | ---: | :---: | :--- | :--- | :--- | :--- |
| r2c4 | 厄斐琉斯 | 0.98 | 1星 | 月蚀骑士 | 迅捷射手 | 物理射手 | 圣盾使的誓约 |
...

## 备战席棋子 (slot1~slot9)

| 槽位 | 棋子 | 置信度 | 星级 | 羁绊 | 职业 | 携带装备 |
...

## 装备栏

| 槽位 | 装备 | 件数 | 类型 |
| :--- | :--- | ---: | :--- |
| 1 | 装备拆卸器 | 6 | 消耗品 |
...

## 各玩家血量
```

和父项目 `trae_agent/prompt.py` 的段落口径一致（`--- 商店 (自左向右槽位 1-5) ---`、
`--- 己方场上奕子 ---`、`--- 备战席 ---`、`--- 装备栏 ---`），只是换成了 Markdown 表格，
表头就是字段名，LLM 不用再猜哪列是什么。

## 3. 每个字段从哪来

| 字段 | 来源 | 备注 |
| :--- | :--- | :--- |
| 阶段 / 倒计时 / 等级 / 经验 / 金币 / 买经验价 / 刷新价 | `extract_info.extractor.extract`（整图 OCR + 区域切片） | 一次 OCR 供 game+shop+players 共用 |
| 商店 5 张的名字 / 价格 | 同上（商店区 OCR） | 价格 = 数字 OCR；`(疑似2星)` 是"价格=3×基础价"的提示 |
| 攻距 / 羁绊 / 职业 / 定位 | `units.db.find_unit(名字)` | 查到才填，查不到留 `-`，**不猜** |
| 场上 / 备战席的棋子名、格位、置信度 | `recognize.yolo_units.extract_units`（YOLO v4.1_units，~60~280ms） | 主力路：名字与格位最准 |
| 星级 | `recognize.board.extract_units`（血条 + 左侧星级徽记） | 靠"中心距 ≤70px"并到 YOLO 棋子上，见 `overlay.live_draw.merge_stars` |
| 携带装备 | `recognize.equipment.extract_board_equips`（血条框 → 装备图标） | 同样按中心距关联，**一对一贪心**分配（一件装备只挂一只棋子） |
| 装备栏 | `recognize.equipment.extract_equipment` | 含消耗品（拆卸器/重铸器），带件数 |
| 玩家血量 | 同上 OCR（右侧玩家列表） | 名字被 OCR 读错很常见（如 `my`），血量一般可信 |

## 4. 三条诚实约定（别当 bug 追）

1. **`?` = 名字没认出来**，不是"没有棋子"。这类行的置信度通常是 0.24~0.55（血条兜底条目），
   星级仍然可信（徽记读出来的）。
2. **同一格位出现多个检测时只留置信度最高的**，被丢的写进 `errors` 的 `<area>.duplicate`，
   例如 `r4c7:沃里克(0.66)`。画面里两只棋子叠在一起/正在移动时会出现。
3. **备战席上的道具也会被列出来**（如 `基础装备锻造器`，置信度 0.98~1.00），它们的星级是 `-`。
   决策时按"这是道具不是棋子"处理即可。

## 5. 与识别层的关系

```
scripts/frame_prompt.py                    ← CLI（本文件讲的这层）
        │
        ▼
extract_info/prompt.py                     ← 合并 + 排版（snapshot / to_markdown / to_json）
        │
        ├── extract_info/extractor.py        整图 OCR：game / shop / players
        ├── recognize/yolo_units.py          棋子框 + 名字 + 格位（主力）
        ├── recognize/board.py               血条 + 星级徽记（补星级、给装备当锚点）
        ├── recognize/equipment.py           装备栏 + 棋子携带装备
        └── units/db.py                      静态库：羁绊 / 职业 / 定位 / 攻距
```

代码里直接用（不用 CLI）：

```python
from PIL import Image
from extract_info.prompt import snapshot, to_markdown

snap = snapshot(Image.open("frame.png").convert("RGB"))   # 非 1920×1080 请先 resize
print(to_markdown(snap))
# 只要某几项:
snap = snapshot(img, only={"shop", "board"})
# 要耗时/失败明细:
timings, errors = {}, {}
snap = snapshot(img, timings=timings, errors=errors)
```

`snapshot()` 的每一项都独立 `try/except`：某一项失败（比如装备栏读崩）只会写进 `errors`，
其余照常输出 —— 决策不能因为一个子项崩掉就整帧作废。

## 6. 自测

```bash
python -X utf8 -u -m tools.selftest_prompt      # 34 项，约 14s
```

覆盖：干净准备帧（`tests/images/board_ingame.png`：商店 5/5、场上 5/5 名字命中、星级、
装备栏 6 槽）+ 4-5 战斗帧（带"备战席已满"弹窗：场上 5/5、备战席 9/9、装备挂载、同格位去重）
+ `--only` / `to_json` 的组合行为。