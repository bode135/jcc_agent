# jcc_robot_by_img —— 纯图色游戏视觉脚手架（截图识别 + 桌宠式叠加 + 点击兜底）

> 一套**只靠屏幕像素**的「看懂画面 → 决定点哪里 → 点下去并复核」的最小完整闭环。
> 不读进程内存、不注入、不 hook、不改游戏文件：全部能力来自 **截图 + 模型/模板识别 + 模拟输入 + 分层透明窗口绘制**。
>
> 本项目是从一个完整对局机器人里**剥离出的图色部分**，去掉内存链路后重新整理成可复用的脚手架，
> 面向「想自己写个看图打游戏的脚本」的开发者：识别层、抽取层、操作层、绘制层各自独立，可整层用、也可拆单模块用。

**License: AGPL-3.0** · 纯图色（不读内存、不注入、不改游戏文件）· 修改后通过网络提供服务也须开源（§13）

![overlay 叠加示例](docs/images/overlay_board_ingame.jpg)

上图是 `scripts/overlay_info.py` 的一帧产物：顶栏状态（回合 / 剩余时间 / 阶段 / 等级 / 金币）、棋盘棋子框+名字+置信度、
**每根血条左侧星级徽记的金框 + 大号 ★×N**（星级就看这一处，判错了立刻能看出是哪根血条）、
血条、小小英雄、奖励球、商店卡、装备栏（左侧）、棋子携带装备，全部由识别结果实时绘制在游戏窗口上方的透明窗口里。

---

## 一、三个脚手架（本项目的交付面）

| 脚手架 | 入口 | 干什么 | 只读? |
| --- | --- | --- | --- |
| **抽取** | `scripts/extract_img_info.py` + `extract_info/` | 一张截图 → 结构化对局信息（回合/剩余时间/等级/经验/金币/阶段/海克斯/商店/玩家血量/小小英雄），可落 JSON、可表格、可循环 | 是 |
| **操作** | `scripts/game.py` + `control/fallback.py` | 「看图 → 点哪里」的动作 CLI：买卡/卖卡/上场/交换/刷新/买经验/装备/合成/拆卸/选格/捡球/海克斯，**每次点击都走通道兜底 + 点后复核** | 否（可 `--dry-run`） |
| **绘制** | `scripts/overlay_info.py` + `overlay/` | 被动观察器：每秒一帧，打印一行行中文决策消息，同时把识别框实时画到游戏窗口上方的透明叠加层 | 是（全文无任何 input 调用） |

三者共用同一套识别核心：`recognize/`（YOLO 棋子 + 模板血条 + 装备图标库 + 球 HSV + 阶段/海克斯判定 + OCR）。

---

## 二、它不是什么（边界先讲清楚）

- **不读游戏内存**：父项目的 frida / proc_mem / roster 全套内存链路已删除，本项目只吃像素。
- **不注入、不 hook、不抢键鼠**：叠加层是外部 Win32 分层透明窗口（`WS_EX_LAYERED|WS_EX_TRANSPARENT|WS_EX_TOPMOST|WS_EX_NOACTIVATE`），
  鼠标键盘默认走后台消息投递（`PostMessage`），失败才升级到前台/`adb`。
- **不含决策 Agent**：父项目的 prompt / LLM / 状态机决策层未移植。本项目只负责「把画面变成可信的决策信息」和「把动作可靠地落下去」，
  决策策略留给你自己写。
- **不内置任何对局自动化循环**：`search-buy`（自动刷牌买卡）、`start`（自动开局）、`exit`（结算退出）、`blessing`（野怪赐福）
  在纯图色口径下依赖父项目的连续状态机/专用模块，本次**明确标注未实现**（命令仍在，跑起来会返回中文 `[X]` 与退出码 2）。

---

## 三、环境

- **Windows 10/11**（截图 `PrintWindow`、后台点击、分层透明窗都是 Win32；识别核心本身跨平台，但入口脚本都依赖 `capture/control`）。
- **Python 3.11**（实测 3.11.16）。依赖见 [`requirements.txt`](requirements.txt)：

  ```
  numpy==2.3.5  pillow==12.3.0  opencv-contrib-python==4.10.0.84
  onnxruntime==1.30.0  rapidocr-onnxruntime==1.4.4
  pywin32==312  psutil==7.2.2  bdtime==1.0.6
  ```

- 需要放在 **1920×1080** 的游戏窗口上（识别区域、坐标常量都按这个基准标定；离线图不是基准尺寸时会先等比 `LANCZOS` 缩放到基准）。
- 素材随仓库提供（`workspace/scrape_info/` 数据 4.9MB、`recognize/templates/` 0.6MB、`units/templates_alpha/` 1.9MB、`tests/images/` 测试帧）。
- **模型权重不进 git**：两个 `.onnx` 合计 21.3MB，走「发行版 / Release」附件分发，克隆后用一条命令拉取并校验 SHA256
  （`--check-only` 只校验、`--proxy` 走代理），详见 [`models/README.md`](models/README.md)。

```powershell
# 安装 (示例用 conda, venv 同理)
conda create -n jcc_img python=3.11 -y
conda activate jcc_img
pip install -r requirements.txt

# 拉权重 (缺什么下什么 + SHA256 校验; 已就绪自动跳过; 国内可加 --proxy http://127.0.0.1:7890)
python -X utf8 -u -m tools.fetch_models

# 一键自检 (完整性/干净度/可导入, 约 10s)
python -X utf8 -u tools/selfcheck.py
```

---

## 四、快速开始（不需要打开游戏）

仓库带 5 张真实对局截图在 `tests/images/`，所有识别/抽取/绘制都能离线跑通：

```powershell
# 1) 抽取: 一张图 -> 结构化信息 (终端表格 + 落盘 JSON)
python -X utf8 -u -m scripts.extract_img_info --img tests/images/board_ingame.png

# 2) 操作脚手架: 识别类命令 (不点鼠标)
python -X utf8 -u -m scripts.game units  --img tests/images/board_ingame.png   # 场上/备战席棋子
python -X utf8 -u -m scripts.game shop   --img tests/images/board_ingame.png --json
python -X utf8 -u -m scripts.game equip  --img tests/images/board_ingame.png   # 左侧装备栏

# 3) 操作脚手架: 动作类命令 --dry-run (只打印坐标与判定依据, 一次都不点)
python -X utf8 -u -m scripts.game buy --slot 1 --dry-run
python -X utf8 -u -m scripts.game equip-to --item 无用大棒 --row 2 --col 4 --dry-run

# 4) 绘制: 离线落 PNG (不弹窗), 看识别框画得对不对
python -X utf8 -u -m scripts.overlay_info --img tests/images/board_ingame.png
#    -> tempdir/overlay_info_board_ingame.png 与 *_mix.png

# 5) 全量自测 (四个功能自测, 约 100s)
python -X utf8 -u tools/selfcheck.py --full
```

三个脚本都支持 **`python scripts/xxx.py` 直接跑**（内部自动把项目根加进 `sys.path`），也支持 `-m` 方式。

---

## 五、接真实游戏（雷电模拟器 + 1920×1080 窗口）

```powershell
# 只观察不操作: 每秒一帧, 打印决策消息 + 窗口上叠加绘制 (Ctrl+C 结束)
python -X utf8 -u -m scripts.overlay_info --ld-index 0 --interval 1.0

# 只看消息不画: 
python -X utf8 -u -m scripts.overlay_info --interval 1.0 --no-overlay

# 抽一帧信息 (实时截图):
python -X utf8 -u -m scripts.extract_img_info --live

# 真点一次买卡 (确认画面在准备阶段才会执行; 先看 --dry-run 的计划)
python -X utf8 -u -m scripts.game buy --slot 1 --dry-run
python -X utf8 -u -m scripts.game buy --slot 1
```

- 窗口定位自动完成：`capture.window.find_render_window()` 找雷电 `RenderWindow` 子窗口（**不是主窗口**，主窗口含右侧工具栏会导致坐标偏移）。
- 没有模拟器时不会抛 traceback：返回中文 `[X] NO_WINDOW: …` 与退出码。
- 叠加层是独立的置顶透明窗，**被别的窗口遮挡时会自动隐藏**（`OVERLAY_HIDE_WHEN_OCCLUDED`），所以截屏/录屏看不到它是正常现象。

---

## 六、命令参考

### 6.1 `scripts/extract_img_info.py`（抽取）

| 参数 | 说明 |
| --- | --- |
| `--img PATH` | 离线图（非 1920×1080 自动等比缩放到基准） |
| `--live` | 实时截图（与 `--img` 互斥） |
| `--format json\|table` | 输出格式（默认 table） |
| `--out DIR` | JSON 落盘目录（默认 `workspace/extract_info`） |
| `--no-save` | 不落盘 |
| `--only game,players,shop,little_legend,hextech` | 只抽指定分组 |
| `--loop SEC` / `--loop-count N` | 循环抽帧（Ctrl+C 结束） |

输出契约（`extract_info.extract(img)`）：`{"players":[…], "shop":[…], "game":[{key,value,rect},…]}`；
`game` 固定 8 键 `stage/round_time/level/xp/gold/xp_buy_cost/refresh_cost/free_refresh`，外加 `phase`（阶段）与 `hex`（海克斯选择状态）两行。
每轮打印一行 `[耗时]`（`ocr.recognize` 即整图 OCR 一次的耗时，便于判断该不该降频）。

### 6.2 `scripts/game.py`（操作）

所有子命令通用：`--dry-run`（只打印计划）、`--json`（stdout 只有一行 JSON，人类可读输出全部转 stderr）、`--ld-index N`（雷电实例号）。
动作命令执行前都会过**阶段门**（`recognize.phase.detect`），不在允许阶段直接拒绝、一次都不点。

| 识别类 | 说明 |
| --- | --- |
| `extract [--info-types a,b]` | 指定信息组（不写子命令时等价本命令） |
| `units --ocr\|--bench\|--board` | 场上/备战席棋子（身份优先 YOLO，模板血条做交叉校验） |
| `shop` / `equip` / `bench` | 商店 5 卡 / 左侧装备栏 / 备战席空位 |
| `cell r3c4 \| slot5 \| board,行,列 \| bench,格` | 某格中心坐标 + 该格棋子身份（我方 `r1~r4c1~c7`、备战席 `slot1~9`；敌方 `-r1~-r4` 负号只用于展示，不能放子/点击） |
| `hex [--refresh N\|--pick N]` / `balls [--rounds N]` | 海克斯卡面 / 奖励球 |

| 动作类 | 复核判据（点完怎么算生效） |
| --- | --- |
| `buy --slot 1..5\|--all` | 该槽卡面像素变化 |
| `sell --bench N \| --row R --col C` | 源格子区域像素变化 |
| `place --slot N --row R --col C` | 拖拽后目标格变化（`timing=board`） |
| `swap --src spec --dst spec` | 源格变化 |
| `refresh --times N` | 商店区像素差 **且** 金币减少（只看出变化会把"金币不足"气泡误判成功） |
| `levelup --times N` | 金币变小（金币 < 4 直接放弃） |
| `equip-to --item 名\|--slot N (--row/--col\|--bench)` | 该件装备从装备栏消失 |
| `combine --a --b` | 确认弹窗点掉后装备栏件数 −1（弹窗模板缺失时超时判失败） |
| `use-remover --item 拆卸器` | 装备栏件数 +1 |
| `search --times N` | 刷新前后商店/备战席/金币对比 |

退出码：`0` 成功 / `2` 用法错 / `3` 状态不对（阶段门拒绝）/ `4` OCR 不可用 / `5` 目标不存在 / `6` 动作无效 / `7` 超时 / `8` 内部错误。

### 6.3 `scripts/overlay_info.py`（绘制 / 被动观察）

| 参数 | 说明 |
| --- | --- |
| `--ld-index N` | 目标雷电实例 |
| `--img PATH` | 离线图模式（不弹窗，落 PNG 到 `tempdir/`） |
| `--interval SEC` | 节拍（默认 1.0s） |
| `--seconds N` | 跑多久（0=一直） |
| `--json` | 每帧输出一行 JSON 决策消息 |
| `--no-overlay` / `--no-print` | 关绘制 / 关打印 |
| `--show a,b,c` 与逐项 `--show-units --show-bars …` | 选择绘制项（`units,bars,balls,yolo_balls,shop,equip,bequips`；顶部状态栏与小英雄框随 units/bars 一起画） |

每帧消息首行会明说「本帧重算: …」，其余项来自缓存（分项降频：棋子/血条/球 0.5s、阶段 0.5s、装备栏 2s、棋子携带装备 3s、经济/商店 5s）。

---

## 七、识别层 API 速查（脱离 CLI 直接用）

```python
from capture.screen import capture_game, resize
from PIL import Image

img = resize(capture_game())            # -> PIL.Image, 1920x1080

from recognize import yolo_units, board, equipment, reward_ball, phase, augment, ocr
units = yolo_units.extract_units(img)    # 棋子: name/star/cell/center/score，速度 ~0.26s/帧
bars  = board.extract_units(img, include_legend=True)         # 血条+星级+小小英雄判定
heroes = hero.detect(img)      # 小小英雄: 等级数字块 + 右侧红/绿小血段 (8~19ms)
slots = equipment.extract_equipment(img)                      # 左侧装备栏（图标库匹配）
carry = equipment.extract_board_equips(img, units=units)      # 棋子身上携带的装备
balls = reward_ball.find_balls(img)                           # 奖励球（HSV 三色）
ph    = phase.detect(img)                                     # {'phase','sword_score','card_score'}
aug   = augment.extract(img)                                  # 海克斯三选一判定 + 卡名
text  = ocr.recognize(img)                                    # 整图 OCR（rapidocr 本地）
```

坐标一律是 **1920×1080 基准**；换屏幕尺寸请先 `resize()` 到基准再识别，否则区域常量全部错位。

格位工具**只有一份实现**，在 `recognize.grid`（口径移植自源项目 `utils/unit_cell.py` / `operations/units.py`）：

```python
from recognize import grid
grid.cell_label(8, 4)           # 'r1c4'          绝对行 -> 人读标签（我方 r1~r4 / 敌方 -r1~-r4）
grid.parse_cell_label('-r2c1')  # ('board', 3, 1)  敌方负号 -> 绝对行
grid.board_cell_center(1, 4)    # (960, 435)       己方第 1 行第 4 列格心
grid.bench_slot_center(5)       # (958, 754)       备战席第 5 格
grid.label_to_center('r3c4')    # (960, 584)       标签 -> 坐标，一步到位
```

---

## 八、性能实测（`tests/images/board_ingame.png`，单帧，本机）

| 项 | 耗时 | 备注 |
| --- | --- | --- |
| 整图 OCR（66 条文本） | 1.7 ~ 2.1s | GPU 回退 CPU 时首次会更慢（冷启动 5.99s） |
| YOLO 棋子（11n，imgsz 1280） | 0.26s | pre 20ms / infer 152ms / post 17ms |
| 血条+星级（`match_cells=set()`，跳过身份匹配） | 0.05s | 带全库身份匹配版 1.66s（8 探针 × 138 变体） |
| 球 HSV / 球 YOLO / 小小英雄 YOLO | 0.02s / 0.01s | 球恒 60×60，HSV 足够（界面高光误检见第九节） |
| 小小英雄等级块法 `hero.detect` | **8~19ms** | 等级数字块 + 右侧红/绿小血段（6.1 节） |
| 阶段判定（扫剑/选卡模板） | 0.25s | |
| 海克斯三选一 | 0.04s | 卡名靠 OCR 小区域 |
| 装备栏（图标库匹配） | 0.5 ~ 1.1s | 依赖 `items.json` + 150 张图标 |
| 场上棋子携带装备 | 0.7 ~ 2.1s | 依赖 `board_item_templates.pkl` 缓存 |
| 抽取一帧全量（extract） | 3.0s | OCR 2.08s 为大头 |
| overlay 热帧 | 0.00 ~ 0.53s | 分项降频后 1s 节拍跟得住；冷首帧 4.8 ~ 6.6s |

结论：**1 秒一帧的预算下，棋子/血条/球/阶段可以每帧重算，OCR 类必须降频（3~5s 一次）**。这也是 `live_draw` / `overlay_info` 里分项间隔的来源。

---

## 九、几条硬邦邦的图色口径（详见 [`docs/图色口径.md`](docs/图色口径.md)）

- 血条恒定高 **8px**，相对格心中位：上沿 `格心−105`（p10 −146 / p90 −86），下沿 `格心−97`。
- 装备栏恒定高 **26px**（`BOARD_ICON_SIDE=26`）= `血条上沿+20 … +46`，横向 5 槽、间距 **30px**。
- 图色框（识别单位框）中位上沿 `格心−88`、高 128、下沿 `格心+40`；其中约 **47.7% 的框把血条/装备栏一起框了进去**，需要把上沿钳到 `格心−49` 再画。
- YOLO 棋子框宽恒 **78**、高随本体（96~156）；采集侧历史上是 78×110 —— **框口径不一致是"框对不上"的根因**，本项目统一以 YOLO 返回框为准。
- 奖励球恒 **60×60** 圆形（蓝/银/金三色），HSV 阈值即可，不必进 YOLO。**注意三处界面高光会冒充球**
  （顶部 HUD 物品槽 / "6人口"按钮 / 小小英雄的青色高光），已用「区域 y 上界 150 + 银球 V≤175 + 蓝球 S≥175 且 V≥190」三道闸清掉：
  `board_ingame.png` 从 4 个误检 → 0，`reward_balls.png` 从 12 → 真值 9（蓝 5 + 银 4），详见 [`docs/图色口径.md`](docs/图色口径.md) 5.1 节。
- 小小英雄框约 **123×94**，靠血条上方白色名牌 `_has_name_text` 判定（**召回很低**，见限制）；
  更稳的判据是**血条左侧是等级数字**（徽记框内白像素 ≥25，见下一条）。
- **血条左侧必定挂徽记**：棋子挂星级 chevron（1★铜 / 2★银蓝 / 3★金），小小英雄挂等级数字。
- **小小英雄主路 = 等级块法**（源项目 `draft_hero` 口径）：等级数字白块当锚点 → 右侧 `+4…+22px` 取色判绿（我方）/红（敌方）→ 16×22 模板余弦相似度读 Lv，**8~19ms/帧**；overlay 上绿框=我方、红框=敌方、框下写 `小小英雄 我方 Lv5`。详见 `docs/图色口径.md` 6.1 节。
  所以徽记是血条定位的**正样本锚点**（`recognize/board.py::star_badge` + `BAR_REQUIRE_BADGE=True`）：
  有 chevron → 真棋子血条且星级直接读颜色；有白数字 → 小小英雄；**两者都没有 → 这根"血条"是误检，直接丢**。
  实测收益（同一批测试帧，`include_legend=False` 的棋子血条数）：非对局帧假血条 **25 → 0**（`board_green_model.png`）、
  海克斯遮罩帧 **5 → 1**、战斗帧误检的绿色藤蔓 **1 → 0**，而真对局帧一根不少（`board_ingame.png` 8 根、`reward_balls.png` 7 根）。
  详见 [`docs/图色口径.md`](docs/图色口径.md) 2.1 节；复核工具 `tools/probe_star_badge.py`。
- **overlay 把星级标出来**：`board.extract_units` 给每根血条附 `badge_box`，`overlay_info` 用金框套住徽记、
  上方画黑底 **★×N**（`OVERLAY_SHOW_STAR_BADGE=True`，`draw_frame` 的 `font 20` 大字）。判位错了 → 框不套血条；
  星级错了 → 框对了但 ★ 数量不对，一眼能分。详见 [`docs/图色口径.md`](docs/图色口径.md) 2.1.2 节。

---

## 十、点击兜底（`control/fallback.py`）

这是本项目"纯图色方案"能真正落地动作的关键：**不假设某一种输入通道一定生效**。

```python
from control.fallback import click_with_fallback, ClickFailed

res = click_with_fallback(
    x, y,
    verify=None,                  # 缺省用 PixelDiffVerifier(点击点局部像素差)
    channels=("bg", "fg", "adb"),  # 依次尝试: 后台 PostMessage -> 前台鼠标 -> adb tap
    retries=2,                    # 每个通道最多 2 轮
    region=None, half=180,        # 复核区域
    diff_min=200, settle=0.2,     # 判"生效"的像素差阈值 / 点后等待
    dry_run=False, note=None,
)
# -> ClickResult(ok, channel, attempts, detail); 全通道失败抛 ClickFailed(带中文排查清单)
```

设计要点：

1. **每次点后都复核**（像素差或自定义复核器：金币变了 / 装备栏件数变了 / 球消失 / 详情面板变化…）。不生效 → 同通道重试 → 换通道。
2. **拍不到 before 快照就直接报错，绝不盲点**；每轮结束重拍 before，避免上一轮的变化污染下一轮（假阳性）。
3. 单通道异常（如 `ldconsole` 不存在）只记 `[!]` 并继续下一个通道；全失败才抛 `ClickFailed`，消息里附 4 条中文排查清单。
4. 返回 **实际生效的通道与尝试次数**，便于统计「在这台机器上哪种通道靠谱」。
5. 动作命令全部支持 `--dry-run`（打印通道链 + 坐标 + 判定依据），没有模拟器也能验证坐标算得对不对。

> 实测机制层证据（本机无对局画面，仅注入假通道+假帧）：`bg` 点完画面完全没变（diff=0，未生效）→ 自动切 `fg` → diff=1600 判定生效 → `ClickResult(channel='fg', attempts=2)`。
> **真有对局时建议先跑一次 `scripts.game buy --slot 1` 观察实际通道**（这条留作上游待验证项）。

---

## 十一、已知限制与未验证项（老实列，别当 bug 追）

| 项 | 现状 |
| --- | --- |
| `search-buy` / `start` / `exit` / `blessing` | 未实现（依赖父项目连续状态机/专用模块），命令返回中文 `[X]` + 退出码 2 |
| 小小英雄旧路 | 白色名牌 + YOLO `legend` 召回极低（血条单位 258 个里只认出 1 个名字），**现在只作备查**；主路是等级块法（见下条） |
| 非对局画面（大厅/登录/结算） | `overlay_info` 自动跳过识别项（实测大厅帧会刷出 23 个假球），只显示 header 的 `非对局` 提示 |
| 小小英雄等级块法 | 需「等级数字块 + 右侧红/绿小血段」两样都在才认；10 级模板没有 11 级以后样本，实机遮挡严重时 level 可能读成 `?` |
| 模板血条路径的棋子名 | 置信度 0.4~0.6、常为 `None` → **身份以 YOLO 为准**，模板路径只做交叉校验 |
| 头像匹配（`units/matcher.py`） | 138 变体里命中率约 3/8、准确率约 67%（含 1 个张冠李戴）→ 不建议单独用作身份源 |
| 非对局帧 | 徽记门禁已经把假血条全丢（`board_green_model.png` 25 → 0），但 `extract_board_equips` 仍可达 7s → 下游一律先用 `phase.detect` 过滤 |
| 玩家列表第 5 行 | 名字框未检出（rect 只有 54px 宽），`players` 该行 name 为 None |
| 真对局实时叠加 | 本机测试时游戏停在非对局画面，**叠加层在真实对局中的效果尚未目视验证**（离线落图与"贴屏可见"探针均已通过） |
| `fg` 通道 | `ALLOW_FOREGROUND_FALLBACK=False` 时与 `adb` 等价；要真前台鼠标需置 True |
| 战斗帧星级 | 改读血条左侧徽记后，战斗帧也能读到星级（实测 2★ 战斗血条全部正确）；但战斗帧备战席单位本来就没血条，仍是 `-` |
| 3★ 金徽记 | 已用实机 3★ 乐芙兰验证（徽记框 H=39/S=158/V=156 落在金区间）；换分辨率后需用 `tools/probe_star_badge.py` 复核 |
| 海克斯阶段经济 | 画面被遮罩，`level/gold` 如实给 `None`（不猜） |

---

## 十二、目录结构

```
jcc_robot_by_img/
├── config/           # config.py（419 个键，见第十三节）+ 装备 id/名映射 + YOLO 类别信任表
├── capture/          # screen.py 截图(PrintWindow) / window.py 找雷电 RenderWindow / save.py 落盘
├── control/          # input.py 后台点击·拖拽·按键·adb / fallback.py 通道兜底+复核 / mouse.py 别名
├── recognize/        # 识别核心: yolo_units(棋子) board(血条/星级/小小英雄) equipment(装备)
│   │                 #           reward_ball(球) phase(阶段) augment(海克斯) extract(信息抽取)
│   │                 #           ocr(rapidocr 本地) grid(格位坐标) regions(区域常量) game_over
│   └── templates/    # 16 项模板: health_bar / phase_swords / phase_cards / augment_eye / 球 / 装备图标库 …
├── extract_info/     # extractor.py: 一张图 -> players/shop/game（一次 OCR + 按区域切片）
├── units/            # db.py 原画库 / matcher.py 头像匹配(portrait_match 优先) + templates_alpha 138 张
├── train_yolo/       # class_table.py 类别冻结表 + classes.json
├── utils/            # template_match / overlay_draw / portrait_match / reward_ball(捡球) / show_img / tt_log …
├── overlay/          # debug_overlay.py 分层透明窗+draw_frame 纯函数 / live_draw.py 节拍调度 / mouse_icon.py
├── scripts/          # game.py / extract_img_info.py / overlay_info.py + README_ops.md
├── tools/            # selfcheck.py 一键自检 + 四个 selftest + config_audit + import_closure
├── models/           # 权重不进 git（Release 附件）: yolo_v2.1_balls.onnx（球/小小英雄/调试）, yolo_v4.1_units_11n.onnx（棋子）
├── workspace/        # scrape_info/v18.3b: 棋子/装备/羁绊数据 json + 150 装备图标 + 装备模板缓存
├── tests/images/     # 5 张真实对局截图（离线自测用）
└── tempdir/          # 运行产物（gitignore）
```

---

## 十三、config 说明（重要，别被 419 个键吓到）

`config/config.py` 是从父项目整段搬来的：**定义 418 个键，本工程实际只读 96 个，另外 322 个是父项目遗留（删掉不影响运行）**。
`tools/config_audit.py` 会打印实际使用情况。真正需要你改的通常只有这几个：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `YOLO_UNITS_ONNX` | `models/yolo_v4.1_units_11n.onnx` | 棋子模型（想更准可换 `yolo_v4.1_units_11s.onnx`，38MB 未随仓库提供） |
| `YOLO_BALL_ONNX` / `YOLO_LEGEND_ONNX` / `YOLO_DEBUG_ONNX` | `models/yolo_v2.1_balls.onnx` | 球 / 小小英雄 / 调试共用 |
| `YOLO_UNITS_CONF` / `YOLO_UNITS_IOU` / `YOLO_UNITS_IMGSZ` | `0.35 / 0.5 / 1280` | 置信度 / NMS / 输入尺寸 |
| `BACKGROUND_INPUT` | `True` | True=后台 `PostMessage` 点击 |
| `ALLOW_FOREGROUND_FALLBACK` | `False` | 兜底是否允许升级到前台真鼠标 |
| `USE_CPU_OCR` | `None` | None=自动（有 CUDA 用 GPU，否则 CPU） |
| `EXTRACT_INFO_DIR` / `CAPTURE_DIR` | `workspace/…` | 抽取 JSON / 截图落盘目录 |
| `OVERLAY_*` | — | 叠加层节拍/开关/遮挡自动隐藏等 |

`PROJECT_ROOT` / `SCRAPE_INFO_DIR` 由 `Path(__file__)` 自动推导，**换目录不用改代码**。

---

## 十四、自测与诊断工具

| 工具 | 用途 |
| --- | --- |
| `tools/selfcheck.py` | 一键自检：完整性（文件/目录/权重非 LFS 指针）、干净度（import 行无内存/frida/sam3 残留）、可导入（37 模块）；`--full` 追加跑四个功能自测 |
| `tools/selftest_recognize.py` | 8 段识别自测（OCR/YOLO/血条+小小英雄/装备/球/阶段/海克斯 + 星级徽记门禁回归）；`--img` 可重复、`--no-yolo`、`--no-rows` |
| `tools/selftest_extract.py` | 抽取链路 + JSON 落盘断言（3 图，约 14s） |
| `tools/selftest_ops.py` | 操作链路 5 组 63 项（api/parse/dryrun/offline/live，约 61s） |
| `tools/selftest_overlay.py` | 绘制链路 5 组（画框纯函数/节拍/离线图/贴屏探针），校验 1s 节拍跟得住 |
| `tools/config_audit.py` | 统计 config 里哪些键真被代码读取（输出 96 在用 / 322 遗留） |
| `tools/probe_star_badge.py` | 星级徽记探针：打印每根血条左侧徽记的色相/白像素/几何，用来标定 1★/2★/**3★** 区间（`--crop` 放大落图） |
| `tools/import_closure.py` | AST 分析入口脚本的模块级/惰性 import 闭包（裁剪与迁移时用） |

实测基线（本机，全部通过）：`selfcheck.py` exit 0；`selftest_recognize` 16.1s（2/2 图无失败）；
`selftest_extract` 13.7s；`selftest_ops --live` 63/63、61.4s；`selftest_overlay` 冷帧 5.72s → 热帧 0.00~0.53s。

---

## 十五、从父项目搬过来时删掉了什么

| 删掉的 | 原因 |
| --- | --- |
| frida / proc_mem / roster 内存读取 | 纯图色方案不需要，且涉及注入与反作弊风险 |
| sam3 分割补检（`recognize/bar_less.py` 等） | 依赖外部服务，默认关闭后直接移除以减少依赖面 |
| 选秀（draft）识别、海克斯 population 判定、`utils.augment_state` | 依赖内存或父项目状态缓存 |
| LLM/Agent 决策层（prompt、runner、observer、openai 依赖） | 本项目只做"信息抽取 + 动作执行" |
| OCR 远程服务通道（`scripts/ocr_server.py` 及客户端重试） | 单进程本地 rapidocr 足够，去掉跨进程等待 |
| 127 张模板里 13 个未引用文件 + 6 个未引用目录（约 505KB） | 开源前清理（`recognize/templates` 现只剩 16 项在用） |
| paddlex PP-OCR det/rec 模型（约 138MB） | 实测零引用（OCR 实际走 rapidocr 自带模型） |

保留但需要注意的"历史痕迹"：`config/config.py` 里有 46 处注释提到 frida/内存/trae_agent 等父项目模块 ——
那是配置项的历史说明，**不影响运行**；`tools/selfcheck.py` 只检查 import 行，正是为了避免这些注释造成误报。

---

## 十六、License / 免责

- 代码以脚手架形式提供，**License = GNU AGPL-3.0**（全文见仓库根 `LICENSE`）。

**AGPL-3.0 对你意味着什么**

| 场景 | 是否允许 | 义务 |
| --- | --- | --- |
| 自己用 / 改了自己用 | 允许 | 改了不必公开（但分发就要按下面几条） |
| 分发原版或改版 | 允许 | 必须**同样以 AGPL-3.0 开源**全部源码（含你的改动），保留版权与免责声明 |
| 把它做成对外提供的网络服务（SaaS/机器人后台/网页） | 允许 | **§13 网络条款**：必须给远端用户提供**获取完整对应源码**的入口，且同样 AGPL-3.0 |
| 闭源商用（改了自己卖、且不开源） | **不允许** | 需要作者另行书面授权（见本节末） |
| 拿它去注册商标/收费分发再限制下游 | 不允许 | AGPL 不允许追加限制 |

- **模型权重与模板图片**（`models/*.onnx`、`recognize/templates/**`、`units/templates_alpha/**`）是作者在本仓库一并提供的**附属资产**，
  随代码按 AGPL-3.0 一同提供；如果你只想复用权重做闭源项目，请先联系作者取得单独授权。
- 仓库内 `workspace/scrape_info/**`（英雄/装备/羁绊数据与图标）与游戏内美术素材版权归**原游戏厂商**所有，
  仅作为识别用的本地参考数据随仓库提供；AGPL 只能覆盖作者自己写的代码，**无权**也不代表授权你商用这些素材。
- 如需**闭源商用授权**（含分成等商业条款），请在仓库 Issues 里留言或邮件联系作者。
- 仓库内 `workspace/scrape_info/**`（英雄/装备/羁绊数据与图标）与游戏内美术素材版权归原游戏厂商所有，
  仅作为**识别用的本地参考数据**随仓库提供；如用于商业用途请自行确认授权，或替换为你自己的数据源。
- 本项目只做屏幕读取与模拟输入，**不修改游戏文件、不读写游戏内存、不注入进程**；使用前请自行确认目标游戏的服务条款与当地法规。
- 未实现命令、已知限制见第十一节 —— 它们是**设计选择**，不是待修 bug。

---

## 十七、想二次开发从哪下手

| 你的目标 | 改哪里 |
| --- | --- |
| 换游戏/换 UI 布局 | `recognize/regions.py`（区域常量）+ `config/config.py` 的坐标键 + `recognize/grid.py`（格位映射） |
| 换/重训识别模型 | `train_yolo/`（类别表 `class_table.py` + `classes.json`）+ `models/` 换 onnx；`yolo_units.py` 自带 letterbox/decode/NMS，不依赖 ultralytics |
| 加新的识别项 | 照 `recognize/reward_ball.py`（HSV）或 `recognize/equipment.py`（模板库）写一个 `extract_xxx(img)`，再在 `extract_info/extractor.py` 注册 |
| 加新的动作命令 | 照 `scripts/game.py` 里 `cmd_buy` / `cmd_equip_to` 写 `cmd_xxx`，点击统一走 `control.fallback.click_with_fallback(..., verify=你的复核器)` |
| 换叠加层样式 | `overlay/debug_overlay.py::draw_frame()`（纯函数，可直接单测）+ `utils/overlay_draw.py::OverlayDraw` |
| 接自己的决策逻辑 | 用 `scripts/overlay_info.py --json` 或 `extract_info.extract(img, only=…)` 拿结构化帧，再用 `scripts/game.py` 的命令执行动作（两步之间策略由你定） |
