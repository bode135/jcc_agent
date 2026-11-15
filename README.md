# jcc_robot_by_img

**纯图色的游戏视觉脚手架** —— 只靠截图，就能完成「看懂画面 → 算出点哪 → 点下去并复核 → 把结果画回屏幕」。

![Python](https://img.shields.io/badge/python-3.11-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

不读游戏内存、不注入、不改游戏文件。从一个完整的对局机器人里剥出来的图色部分，识别、抽取、操作、绘制各自独立，可以整层用，也可以只拿一个模块。

---

## 1. 干什么的

给你一套能跑的最小闭环，剩下的策略你自己写：

- **看懂画面**：一张 1920×1080 截图 → 棋子 / 血条 / 星级 / 装备 / 奖励球 / 阶段 / 商店 / 金币，结构化输出
- **点得准**：坐标换算 + 点击兜底 + 点后像素复核，不假设哪条输入通道一定生效
- **看得见**：把识别框实时画在游戏窗口上方的透明叠加层里，判错了一眼就能看出来
- **喂得动**：一帧画面直接组合成 Markdown 四表，能塞给任何决策逻辑

适合想自己写「看图打游戏」脚本、又不想碰内存和注入的人。

---

## 2. 效果图

![overlay 叠加示例](docs/images/overlay_board_ingame.jpg)

`scripts/overlay_info.py` 在真实对局帧上的输出：顶栏状态（回合 / 剩余时间 / 阶段 / 等级 / 金币）、棋子框 + 名字 + 置信度、**每根血条左侧星级徽记的金框和 ★×N**、小小英雄（绿框我方 / 红框敌方）、奖励球、商店卡、左侧装备栏。全部由识别结果实时绘制。

抽取的输出长这样：

```text
$ python -X utf8 -u -m scripts.extract_img_info --img tests/images/board_ingame.png --format table

阶段 准备    回合 3-6    剩余 19s    Lv5 (经验 10/18)    金币 103
商店  1.芸阿娜(2)  2.沃里克(2)  3.卡西奥佩娅(3)  4.阿兹尔(3)  5.疯狂掷币人(1)
玩家  8 行：血量 / 等级 / 金币 / 阶段 ...
```

---

## 3. 怎么用

### 安装

Windows 10/11 + Python 3.11，游戏窗口需要 1920×1080。

```powershell
pip install -r requirements.txt

# 拉模型权重（21MB，走 Release 附件，自动校验 SHA256；国内可加 --proxy http://127.0.0.1:7890）
python -X utf8 -u -m tools.fetch_models

# 自检
python -X utf8 -u tools/selfcheck.py
```

### 离线试一下（不用打开游戏）

仓库带 5 张真实对局截图，所有能力都能直接跑：

```powershell
python -X utf8 -u -m scripts.overlay_info --img tests/images/board_ingame.png
# -> tempdir/overlay_info_board_ingame.png

python -X utf8 -u -m scripts.extract_img_info --img tests/images/board_ingame.png
python -X utf8 -u -m scripts.game units --img tests/images/board_ingame.png
python -X utf8 -u -m scripts.frame_prompt --img tests/images/board_ingame.png
```

### 接真实游戏

```powershell
# 只观察：每秒一帧，打印 + 叠加绘制
python -X utf8 -u -m scripts.overlay_info --ld-index 0 --interval 1.0

# 动作命令先看计划，确认坐标对了再真点
python -X utf8 -u -m scripts.game buy --slot 1 --dry-run
python -X utf8 -u -m scripts.game buy --slot 1
```

### 在代码里直接用

```python
from capture.screen import capture_game, resize
from recognize import yolo_units, board, equipment, reward_ball, phase, hero

img     = resize(capture_game())               # PIL.Image, 1920x1080
units   = yolo_units.extract_units(img)        # 棋子
bars    = board.extract_units(img)             # 血条 + 星级 + 小小英雄
heroes  = hero.detect(img)                     # 小小英雄（等级块法）
slots   = equipment.extract_equipment(img)     # 左侧装备栏
balls   = reward_ball.find_balls(img)          # 奖励球
ph      = phase.detect(img)                    # 阶段
```

每个 CLI 都有 `--help`，参数不在这里堆。

---

## 4. 框架

```text
┌─────────────┐     ┌───────────────┐     ┌──────────────────┐
│ capture/    │────▶│ recognize/     │────▶│ extract_info/     │
│ 截图·找窗口  │     │ YOLO 模板 颜色 │     │ 一张图 → 结构化   │
└─────────────┘     │ OCR 阶段判定   │     └────────┬─────────┘
                    └───────┬───────┘              │
                            │                      ▼
                    ┌───────▼───────┐     ┌──────────────────┐
                    │ overlay/       │     │ control/          │
                    │ 透明叠加 + 绘制 │     │ 点击·拖拽·兜底·复核│
                    └───────────────┘     └──────────────────┘
```

```text
├── config/        配置 + 装备 id/名映射 + YOLO 类别信任表
├── capture/       截图 / 找雷电 RenderWindow
├── control/       后台点击·拖拽·按键·adb / fallback 通道兜底
├── recognize/     识别核心（yolo_units / board / equipment / reward_ball / phase / ocr / grid）+ 模板
├── extract_info/  一张图 → players / shop / game
├── overlay/       分层透明窗 + draw_frame 纯函数 + 节拍调度
├── scripts/       四个 CLI：extract_img_info / game / overlay_info / frame_prompt
├── units/         原画库 + 头像匹配 + 138 张模板
├── train_yolo/    类别冻结表
├── tools/         自检 / 四个自测 / 权重下载 / 诊断探针
├── scrape_info/   爬虫：抓赛季基础库（棋子/装备/羁绊/阵容 + 图标）
├── scrape_comp/   爬虫：抓单个阵容页 → 解密棋盘 → 打法文档
├── models/        ONNX 权重（不进 git，Release 附件）
├── workspace/     随仓库附带的赛季数据样本 + 运行时产物
└── tests/images/  5 张测试帧
```

---

## 5. 重点功能

**点击兜底**（`control/fallback.py`）
不假设某条通道一定生效：后台 `PostMessage` → 前台鼠标 → `adb tap`，每次点完都用像素差判断有没有生效，没生效就同通道重试、再换通道。拍不到 before 快照直接报错，绝不盲点。返回实际生效的通道和尝试次数。

**星级徽记定位**（`recognize/board.py`）
棋子血条左侧必定挂徽记（1★ 铜 / 2★ 银蓝 / 3★ 金），小小英雄挂等级数字。把这个当血条定位的正样本锚点：有 chevron 才是真棋子血条，有白数字才是小小英雄，两者都没有就是误检直接丢。非对局帧的假血条 25 → 0，真对局帧一根不少。

**小小英雄等级块法**（`recognize/hero.py`）
等级数字白块当锚点 → 右侧 +4…+22px 取色判绿（我方）/ 红（敌方）→ 16×22 模板余弦相似度读等级。8~19ms/帧，比原先的白名牌 OCR 召回高得多。

**格位口径只有一份**（`recognize/grid.py`）
我方 `r1~r4c1~c7`、备战席 `slot1~9`、敌方 `-r1~-r4`（负号只用于展示，不能点）。标签解析、标签→坐标、坐标→标签全在一个模块，`game.py` 和 `overlay_info.py` 都转发到这里。

**装备识别**（`recognize/equipment.py`）
官方图标库做模板匹配，覆盖左侧装备栏（5 格 × 10 行）和棋子身上携带的装备（血条下方 26px）。模板库缓存缺失时自动重建。

**奖励球**（`recognize/reward_ball.py`）
球恒 60×60 三色，HSV 就够。三道闸过滤界面高光（顶部 HUD / 人口按钮 / 英雄高光），误检 4 → 0。

**分项降频**（`overlay/live_draw.py`）
1 秒节拍下，棋子 / 血条 / 球每帧重算，OCR 类 3~5s 一次，装备栏 2s、携带装备 3s。热帧 0~0.5s。

---

## 已知限制

### 模型能力缺陷

> 目前的 `yolo_units` 基本只用**准备阶段**的画面训练，下面这些是训练数据覆盖不到的，下一个版本会专门解决。

1. **棋子背面识别不了** —— 只支持准备阶段（棋子正面站立）的识别。战斗阶段棋子会转身背对屏幕，当前模型认不出来。
2. **高费棋子准确率下降** —— 4 费棋子准确率明显下滑，5 费基本识别不了，会标成 `unknown`，只能靠「点开棋子详情再读画面」的动态提取方案兜底。
3. **奖励球重叠/被遮时不稳** —— `yolo_balls` 在球互相重叠或被棋子挡住时会漏检，需要图色法（`recognize/reward_ball.py` 的 HSV 路径）这种召回率更高的方案来兜底。

### 其它

- `search-buy` / `start` / `exit` / `blessing` 未实现（纯图色口径需要连续状态机）
- 棋子身份以 YOLO 为准，模板血条的棋子名只做交叉校验
- 小小英雄遮挡严重时等级可能读成 `?`
- 真对局中的实时叠加效果尚未目视验证（离线落图和贴屏探针都已通过）

## 爬虫（可选）

```powershell
python -m scrape_info                # 抓赛季基础库 → workspace/scrape_info/v{版本}/
python -m scrape_comp 16278          # 抓单个阵容 → 解密棋盘 → 打法文档
```

只抓站点公开数据，不需要游戏在跑。仓库随附一份固定样本，clone 下来即可离线跑识别。详见 [scrape_comp/README.md](scrape_comp/README.md)。

## 想改哪里

| 目标 | 改这里 |
| --- | --- |
| 换游戏 / 换 UI 布局 | `recognize/regions.py` + `config/config.py` 坐标键 + `recognize/grid.py` |
| 换 / 重训模型 | `train_yolo/`（类别表）+ `models/` 换 onnx |
| 加识别项 | 照 `recognize/reward_ball.py`（HSV）或 `equipment.py`（模板库）写 `extract_xxx(img)`，到 `extract_info/extractor.py` 注册 |
| 加动作命令 | 照 `scripts/game.py` 的 `cmd_buy` 写 `cmd_xxx`，点击走 `click_with_fallback` |
| 接自己的决策 | `overlay_info --json` 或 `extract_info.extract(img)` 拿结构化帧，再用 `scripts.game` 执行 |