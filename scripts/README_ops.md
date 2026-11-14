# 游戏操作脚手架 (T3: control/fallback + scripts/game.py)

本文件说明 `scripts/game.py` 的命令面、`control/fallback.py` 的点击兜底 API、
以及本工程里**有意不做**的命令。输出无 emoji, 统一前缀 `[OK]` / `[X]` / `[!]` / `[i]`。

所有命令都在工程根目录下用下面这条口径跑 (中文必须带 `-X utf8`):

```
python -X utf8 -u -m scripts.game <子命令> [参数]
```

坐标一律是 **1920×1080 基准**, 屏幕/客户区/Android 像素换算由 `control.input` 负责;
离线图不是基准尺寸时会先等比缩放到基准再识别 (`[i] 离线图尺寸 … != 基准 …`), 否则识别
区域和点击数字会是另一套比例、不能与实机对齐。

## 命令一览

识别类 (不点鼠标, 可用 `--img` 走离线图):

| 子命令 | 作用 |
| --- | --- |
| `extract` (`--info-types game/shop,game/equip`) | 识别指定信息组; **不写子命令时等价于本命令** |
| `units --ocr|--bench|--board` | 场上奕子/备战席识别 (身份优先用 YOLO, 模板血条只做交叉校验) |
| `shop` | 商店 5 张卡 (默认 `--info-types shop,game`) |
| `equip` | 左侧装备栏 (默认 `--info-types equip`) |
| `bench` | 备战席空位/占用/道具 |
| `cell <board,行,列|bench,格>` | 某格的中心坐标 + 该格棋子身份 (可用 `--save-dir` 存图) |
| `hex --refresh N|--pick N` | 海克斯卡面识别 / 刷新 / 点选 |
| `balls --rounds N` | 奖励球识别 + 逐个点击 (点后该球消失即算生效) |

动作类 (全部支持 `--dry-run`, 只打印坐标 + 判定依据, 一次都不点):

| 子命令 | 作用 |
| --- | --- |
| `buy --slot 1..5|--all` | 点商店槽买卡, 复核=该槽卡面像素变化 |
| `sell --bench N|--row R --col C` | 拖到商店中心卖出, 复核=源格子变化 |
| `place --slot N --row R --col C` | 备战席 -> 棋盘 (拖拽时序 `timing=board`) |
| `swap --src spec --dst spec` | 两格互换 (位置写法 `board,行,列` / `bench,格`) |
| `refresh --times N` | 刷新商店, 复核=商店区像素差 **且** 金币减少 (只看出变化会把「金币不足」气泡误判成功) |
| `levelup --times N` | 买经验, 复核=金币数值变小 |
| `equip-to --item 名|--slot N (--row R --col C|--bench N)` | 装备拖到棋子, 复核=该件从装备栏消失 |
| `combine --a 名|格号 --b 名|格号` | 合成, 复核=确认弹窗点击后装备栏件数 -1 |
| `use-remover --item 拆卸器 …` | 拆卸装备, 复核=装备栏件数 +1 |
| `search --times N` | 刷新 + 前后商店/备战席/金币对比 |

## 通用约定

- `--json`: **stdout 只有一行** `{"ok":true,"cmd":…,"data":…}` 或
  `{"ok":false,"cmd":…,"error":{"code","exit_code","message"}}`;
  人类可读输出全部转 stderr (实现上会把 fd 1 整体重定向到 stderr, 因为 onnxruntime
  这类 C 层日志会绕过 `sys.stdout` 直接写 fd 1)。
- 退出码: `0` 成功 / `2` 用法错 / `3` 状态不对 (如不在允许阶段) / `4` OCR 不可用 /
  `5` 目标不存在 / `6` 动作无效 / `7` 超时 / `8` 内部错误。
- `--ld-index N`: 指定雷电实例号, 影响 `adb` 通道。
- 无模拟器窗口时不打 traceback, 只打中文 `[X]` 报错 (如
  `[X] NO_WINDOW: 未找到雷电模拟器窗口, 请先启动模拟器 …`)。

## 阶段门 (避免点空/误点)

每个动作命令先跑 `recognize.phase.detect` 判断当前画面阶段, 不在允许阶段就**拒绝执行**:

| 阶段 | 允许的命令 |
| --- | --- |
| `prep` 准备 | 买卡 / 卖出 / 上场 / 交换 / 刷新 / 买经验 / 装备 / 合成 / 拆卸 |
| `prep` + `combat` | 选格 (`cell`) / 捡球 (`balls`) |
| `augment` 海克斯 | `hex` |
| `unknown` 非对局帧 | 全部拒绝 (实跑), `--dry-run` 只打 `[!]` 提示并继续给计划 |

实测: 本机画面不在对局时 `buy --slot 1` 打出
`[X] NOT_IN_GAME: 当前画面阶段为 'unknown' … 拒绝执行买卡`, 退出码 3, **一次都没点**。

## 点击兜底 API (`control/fallback.py`)

```python
from control.fallback import click_with_fallback, ClickFailed, PixelDiffVerifier

res = click_with_fallback(
    x, y,
    verify=None,                     # None = 用默认 PixelDiffVerifier(局部像素差)
    channels=("bg", "fg", "adb"),    # 依次尝试的通道
    retries=2,                       # 每个通道最多点几次 (轮为单位)
    region=None, half=180,           # 复核区域 (缺省=点击点周围 half 的方框)
    diff_min=200,                    # 判定"生效"的像素差阈值
    settle=0.2,                      # 点完等多久再复核
    dry_run=False,                   # True 只打印计划, 一次都不点
    note=None,                       # 动作专属判定依据文案
)
# res -> ClickResult(ok, channel, attempts, detail); 失败抛 ClickFailed(带 .result)
```

- 通道: `bg`=后台 PostMessage (`control.input.click_left_background`)、
  `fg`=前台真实鼠标 (`click_left_foreground`, 配置关闭前台时会走 adb)、
  `adb`=雷电 `adb shell input tap`。
- `verify` 可以是 `() -> bool|dict` 或 `(info) -> bool|dict` 的回调; 返回 dict 时取 `ok`/`hit`。
  动作命令各自注入专属复核器 (金币变小 / 装备栏件数变化 / 球消失 / 详情面板变化 …)。
- 每轮开始会重拍 before 快照, 避免把上一轮已发生的变化算进下一轮 (假阳性)。
  单通道异常 (如 ldconsole 找不到) 只记 `[!]` 并继续下一个通道。
- `click_many([(x, y), …])` 是对同一套复核器连点的便捷封装。

## 明确未实现 (有意不做, `--help` 与命令列表里标注 `[未实现]`)

| 命令 | 原因 |
| --- | --- |
| `search-buy` | 自动刷牌买卡: 依赖「识别-刷新-买-回退」连续对局状态机与金币预算, 本工程只保留单步动作 (需要时用 `refresh` + `buy` 组合) |
| `start` | 启动游戏: 依赖窗口尺寸初始化与自动开局脚本 (`utils.window_size` / `auto_start_game`), 新工程未移植; 请手动开模拟器与对局 |
| `exit` | 退出对局: 依赖关卡轮转与结算页识别 (`restart_after_stage`), 新工程未移植 |
| `blessing` | 野怪赐福: 依赖 `recognize/beast.py` 专用识别模块与优先级配置, 新工程没有该模块 |

## 自测 (`tools/selftest_ops.py`)

```
python -X utf8 -u -m tools.selftest_ops                 # api/parse/dryrun/offline 四组
python -X utf8 -u -m tools.selftest_ops --live          # 追加实机检查 (无窗口则 skip)
python -X utf8 -u -m tools.selftest_ops --json --only api,parse
```

五组: `api`(fallback API 约定) / `parse`(argparse 与默认值隔离) / `dryrun`(每个动作
命令 `--dry-run --json` 必须 exit 0 且给出计划) / `offline`(离线图识别 + 复核器) /
`live`(实时截图/窗口, 没有窗口记 skip 不算失败)。退出码 0=全通过, 1=有失败。

## 已知坑 (别再当 bug 追)

1. `capture.save.save_to_dir` 的 `image_name` **必须自带扩展名**, 否则
   `ValueError: unknown file extension`; 本工程的 `_save_flash` 已统一补
   `_{时间戳}_{毫秒}.png`。
2. `board_green_model.png` 这类非对局帧 `board.extract_units` 会误检几十根血条 ——
   所以动作命令先过阶段门, 别拿它当真单位。
3. 模板图色的棋子名置信度低 (0.4~0.6, 常见 `name=None`), 身份以
   `yolo_units.extract_units` 为准 (0.97~0.99, 约 0.26~0.7s/帧)。
4. `--info-types` 不能放在 `parents=[common]` 里: argparse 子命令共用同一个 action 对象,
   `set_defaults` 会互相污染 (实测 shop/equip 会窜), 每个子命令自己声明默认值。
