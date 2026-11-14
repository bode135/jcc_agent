"""项目全局配置: DefaultConfig 单例, 集中管理便于实时修改.

使用方式:
    from config.config import default_config

    path = default_config.CAPTURE_DIR        # 读取
    default_config.STOP_FLAG = True          # 运行时修改, 各处引用即时生效
"""

import os
import shutil
import sys
from pathlib import Path


class DefaultConfig:
    """默认配置. 项目内共享同一个实例 default_config, 改属性即时生效."""

    # 文件聊天桥：共享单槽信箱与有界锁等待。
    BRIDGE_DIR = "tempdir/bridge"
    BRIDGE_LOCK_TIMEOUT = 5.0
    BRIDGE_POLL_INTERVAL = 10.0

    # 规则采集运营：先保战力再采高费；均为待实机校准的初始值。
    GPT_BASE_STAGE_LEVELS = (("2-1", 4), ("2-5", 5), ("3-2", 6), ("3-5", 7), ("4-5", 8))
    GPT_BASE_HP_WARNING = 50
    GPT_BASE_HP_EMERGENCY = 30
    GPT_BASE_BIG_LOSS = 15
    GPT_BASE_KEEP_HEALTHY = 30
    GPT_BASE_KEEP_WARNING = 10
    GPT_BASE_KEEP_EMERGENCY = 0
    GPT_BASE_XP_PER_BUY = 4
    GPT_BASE_MAX_ROLLS = 3
    GPT_BASE_MAX_OPS = 20
    GPT_BASE_MAX_SPEND = 60
    GPT_BASE_PRE_SECONDS = 6.0
    GPT_BASE_POST_SECONDS = 20.0
    GPT_BASE_COMBAT_SECONDS = 30.0
    GPT_BASE_STOP_LEFT = 10.0
    GPT_BASE_ACTION_SECONDS = 3.0
    GPT_BASE_SAMPLE_LEFT = 28.0
    # 2026-10-31 用户: "为什么老是会合成[珠光护手]这件法师装备, 我们 wanted_equip 中应该没有这装备吧?"
    # → 本阵容是物理体系 (凯特琳/希维尔), 珠光护手 (无用大棒+拳套) 属于法系成装, 不该进保命合成表。
    # 当前 gpt_base 链默认不跑 (use_gpt_base=False), 但常量留着就迟早会用 --gpt-base 复现同样的错, 故删掉。
    GPT_BASE_COMBINE_PRIORITY = ("狂徒铠甲", "石像鬼石板甲", "棘刺背心", "巨龙之爪",
                                 "鬼索的狂暴之刃", "朔极之矛", "无尽之刃")

    # ---- 路径 ----
    # 项目根 (2026-10-18 exe 打包): PyInstaller 打出来的 exe 里 `__file__` 指向解包目录, 不能再用它
    # 推项目根 —— 入口脚本 `scripts/runner_client/exe_entry.py` 会先把真实项目根写进环境变量
    # `JCC_PROJECT_ROOT`; 源码直接运行时没有这个变量, 行为与原来完全一致 (按本文件位置上跳两级)。
    PROJECT_ROOT = (Path(os.environ["JCC_PROJECT_ROOT"]).expanduser().resolve()
                    if os.environ.get("JCC_PROJECT_ROOT", "").strip()
                    else Path(__file__).resolve().parent.parent)
    CAPTURE_DIR = PROJECT_ROOT / "workspace" / "captures"
    PADDLEX_CACHE_DIR = PROJECT_ROOT / "models" / "paddlex"
    SCRAPE_INFO_DIR = PROJECT_ROOT / "workspace" / "scrape_info"  # 爬虫数据根目录
    EXTRACT_INFO_DIR = PROJECT_ROOT / "workspace" / "extract_info"  # 对局信息抽取结果目录
    BILI_VIDEO_DIR = PROJECT_ROOT / "workspace" / "bili_video"  # B站视频分析产物根目录 (scripts/bili_video.py)

    # ---- 截图 ----
    SAMPLE_IMAGE_SAVE_DIR = PROJECT_ROOT / "images" / "sample"
    WORK_SPACE_DIR = PROJECT_ROOT / "workspace"

    # ---- 临时文件根（2026-10-28 用户 m00001）----
    # 为什么要有它: 以前到处 `tempfile.mkdtemp(dir=str(ROOT))`, 中断/异常退出后
    # 项目根会堆满 `.dsh-tmp-*`、`tmp*` 空目录壳, 用户看着很脏。现在统一收口到这里:
    # 所有临时目录/临时文件一律建在 `tempdir/` 下面, 它的清理是整目录递归, 不会漏。
    # `tempdir/` 已在 .gitignore 里整体忽略, 不会污染 git status。
    TEMP_ROOT = PROJECT_ROOT / "tempdir"

    # ---- 内存 runner v2：单步预算与证据目录（详见 dev_docs/04_runner与决策/gpt_run_mem_v2-执行闭环设计_20261026.md）----
    RUN_MEM_V2_DIR = WORK_SPACE_DIR / "run_mem_v2"
    RUN_MEM_V2_STEP_SECONDS = 4.0  # 一次输入加前后读取的保守预算，实机验收后再标定
    RUN_MEM_V2_INPUT_SECONDS = 0.8
    RUN_MEM_V2_MARGIN_SECONDS = 1.0
    RUN_MEM_V2_MAX_ACTIONS = 20
    RUN_MEM_V2_MAX_SEARCH_REFRESH = 10
    RUN_MEM_V2_BALL_RADIUS = 40
    RUN_MEM_V2_CONFIRM_WAIT = 0.25
    RUN_MEM_V2_POOL_TTL = 45.0
    RUN_MEM_V2_READ_TIMEOUT = 8.0
    RUN_MEM_V2_UI_SECONDS = 8.0
    RUN_MEM_V2_MAX_BALLS = 3
    RUN_MEM_V2_BALL_MIN_Y = 150  # 沿用采集器的 HUD 排除线，顶栏误检不能生成点击
    RUN_MEM_V2_OUTPUT_EQUIPS = ("鬼索的狂暴之刃", "死亡之刃", "无尽之刃", "最后的轻语", "红霸符",
                               "朔极之矛", "蓝霸符", "海妖之怒", "海克斯科技枪刃")
    RUN_MEM_V2_DEFENSE_EQUIPS = ("石像鬼石板甲", "振奋盔甲", "狂徒铠甲", "日炎斗篷", "薄暮法袍", "适应性头盔")

    # ---- 内存 runner v3：在 v2 骨架上补窗口级预算、证据强度与包含式判据 ----
    # 详见 dev_docs/04_runner与决策/dsv4f_动作成功率根因与执行闭环_20261026.md 第 9 节。
    # 与 RUN_MEM_V2_* 的关系：v3 是独立一套常量，v2 包仍按 v2 常量运行，两者可并存对照。
    RUN_MEM_V3_DIR = WORK_SPACE_DIR / "run_mem_v3"
    RUN_MEM_V3_STEP_SECONDS = 4.0  # 单步（读+输入+再读+判据）的保守预算
    RUN_MEM_V3_INPUT_SECONDS = 0.8  # 只够把输入发出去的下限
    RUN_MEM_V3_MARGIN_SECONDS = 1.0
    RUN_MEM_V3_LLM_SECONDS = 6.0  # 一次三角色决策的历史均值上界，窗口预算要预留
    RUN_MEM_V3_RESERVE_SECONDS = 2.0  # 留给收尾/下一轮轮询的余量
    RUN_MEM_V3_REPLAN_LIMIT = 1  # 一个 prep 窗口最多重新决策几次（0 表示关掉重规划）
    RUN_MEM_V3_MAX_ACTIONS = 20
    RUN_MEM_V3_POOL_ROUNDS = 3  # prep 窗口内最多连冲几轮动作池（每轮都重取快照，做完再判）
    RUN_MEM_V3_COMBAT_POOL_ROUNDS = 1  # combat 期冲池轮数：战斗会持续十几秒，只留一轮给合法动作
    RUN_MEM_V3_MAX_SEARCH_REFRESH = 10
    RUN_MEM_V3_BALL_RADIUS = 40
    RUN_MEM_V3_CONFIRM_WAIT = 0.25
    RUN_MEM_V3_POOL_TTL = 45.0
    RUN_MEM_V3_READ_TIMEOUT = 8.0
    RUN_MEM_V3_UI_SECONDS = 8.0
    RUN_MEM_V3_MAX_BALLS = 6  # 用户 m01718: 1~2 阶段地上堆球不捡 -> 装备栏永远空, 每窗口多捡几颗
    RUN_MEM_V3_BALL_MIN_Y = 150  # 沿用采集器的 HUD 排除线，顶栏误检不能生成点击
    # 同一坐标点过之后多久允许再点：球捡走会消失，但同一位置后面可能刷出新的球；
    # 永久去重会导致"地上一堆球永远不点"（2026-10-29 用户实机指出）。
    # 2026-10-30 用户 m01718: 12s 太长, 捡球频率要像买卡一样高 -> 收到 4s。
    RUN_MEM_V3_BALL_RETRY_SECONDS = 4.0
    # 点球后等球消失的上限秒数: 点击只是指挥小小英雄走过去, 实际 1~3s 才捡起
    # (2026-10-29 实机, 固定 1.2s 等不到 -> 误判「目标球仍在原位」)。
    RUN_MEM_V3_BALL_CONFIRM_WAIT = 3.0
    RUN_MEM_V3_OUTPUT_EQUIPS = ("鬼索的狂暴之刃", "死亡之刃", "无尽之刃", "最后的轻语", "红霸符",
                               "朔极之矛", "蓝霸符", "海妖之怒", "海克斯科技枪刃")
    RUN_MEM_V3_DEFENSE_EQUIPS = ("石像鬼石板甲", "振奋盔甲", "狂徒铠甲", "日炎斗篷", "薄暮法袍", "适应性头盔")

    # ---- 实时播报桌宠 ----
    DESKTOP_PET_REPLY_PATH = WORK_SPACE_DIR / "brain" / "reply.txt"
    DESKTOP_PET_ACTION_POOL_PATH = WORK_SPACE_DIR / "brain" / "actions_poll.txt"
    DESKTOP_PET_ASSET_DIR = PROJECT_ROOT / "assets" / "desktop_pet"
    # 注: 「每次 runner 启动是否自动拉起桌宠解说员」的开关不在这里, 而在
    # `trae_agent/config.py::PET_DIRECTOR_ENABLED` (那是 runner 行为开关, 与 MANUAL_MODE 等放一起)。

    # ---- 运行归档 (DSH 每次运行/实验的日志与总结) ----
    # 子目录命名: dsh_<日期>_<时:分>_<exp_id>/; 结构同 dsh_2027-10-17_12-23
    # (note.md 经验总结 + log.txt/log_<时间戳>.txt 运行日志 + err.txt/log_err.txt 错误流)
    AGENT_WORK_DIR = WORK_SPACE_DIR / "agent_work"

    DEFAULT_RESOLUTION = (1920, 1080)  # 截图统一分辨率 (宽, 高), 与雷电模拟器原生渲染一致
    # DEFAULT_RESOLUTION = (1958, 1114)  # 截图统一分辨率 (宽, 高), 与雷电模拟器原生渲染一致(要稍微大一点点, 让渲染区域为[1920, 1080])
    # DEFAULT_RESOLUTION = (1200, 720)  # 截图统一分辨率 (宽, 高), 与雷电模拟器原生渲染一致
    INIT_WINDOW_SIZE = DEFAULT_RESOLUTION  # 窗口初始化分辨率: scripts.set_window_size (及开局前自动调用) 把渲染区调成此尺寸; 设为 None 跳过

    # 棋盘透视网格: 每行 (第1列中心x, 中心y, 列间距), 整盘从上到下8行、左到右7列。
    BOARD_GRID_ROWS = (
        (676, 185, 91), (711, 243, 94), (652, 304, 98), (691, 367, 102),
        (636, 435, 108), (678, 508, 112), (612, 584, 116), (654, 663, 120),
    )
    BOARD_COLUMNS = 7
    BOARD_BAR_TO_CELL_Y = 100  # 血条底边到落脚点, 不使用随模型姿态变化的脚部轮廓
    # **2026-10-13 4-2 帧事故**: 血条画在单位精灵正上方, 精灵高度逐单位不同 ->
    # "血条底边 -> 落脚点"的真实偏移在同一帧内实测 55 / 70 / 73 / 88 / 102 / 111 px。
    # 固定 +100 会把 r1c3 的雷克塞锚到下一行空格 (r2c3) -> 探针点空 -> 详情卡弹不出 -> 整格丢框 (假负样本),
    # 静态补标又在空格补了个 unknown。因此改用 `recognize.grid.bar_to_cell`:
    # 两个锚点各映射一次, 取 `position_error` 更小者 (哪个落点离格心近就信哪个)。
    BOARD_BAR_ANCHOR_FALLBACK_Y = 44  # 备选锚点偏移 (老口径, 只在它离格心更近时采用)
    # **棋盘格位模型**: "hex"(六边形, 现有方案) / "quad"(四边形, 网格带判定)
    # 用法: 改这一行即可切换; 两个方案并存, 便于用真实帧对比谁更准 (用户口径 2026-10-12)
    BOARD_CELL_MODEL = "hex"
    BOARD_CELL_MAX_DISTANCE = 0.65  # 以横/纵格距归一化后的最大定位误差
    # BAR_DETECT_METHOD = "color"  # 血条定位方案: "color"=绿掩码(战斗阶段亮绿地面可能漏检) / "template"=模板匹配(S19全检出)
    # **血条定位方案**: "union"(并集, 推荐) / "color" / "template"
    # 用户口径 2026-10-12: 单跑 template 时叠放棋子会漏一根 (实测该帧 template 3 根 vs color 4 根);
    # 25 帧统计: 并集 68 根 > template 67 根, 且从不更差 (并集更多 1 帧 / template 更多 0 帧)。
    # **默认回退为 template** (2026-10-12) —— 但要说清证据强度, 别自欺:
    #   当时只观察到**相关性**: 改 union 后"同格已占用->丢弃"在 4 帧出现 10 次 (旧 template 47 帧 0 次)。
    #   后来逐帧核对发现: 那 4 帧走的是"分两次(只补备战席)"路径, 而 47 帧那批是"一次性全提",
    #   **根本不是同一条路径** -> **因果未证明**。真正让名字/框丢掉的原因在
    #   extract_info/roster.py 的"判空分支提前 return"与采集器兜底补框的静默 continue (提交 791df61),
    #   与 union/template 之争无关。
    #   所以"回退 template"只是当时的保守动作, **不代表 union 更差**。
    # **2026-10-13 改回 union —— 这次有错标签的直接证据** (用户实机 5-1_144851_374.png):
    #   同一张图 template 只找到 7 根血条 (漏掉莫甘娜那条 (919,441,998,449)), color 找到 8 根;
    #   而探针的格位计划**唯一来源就是血条** -> 那一轮 probe_evidence 里真的没有 board_3_4,
    #   莫甘娜整格没点 -> 标签里一个框都没有 (假负样本), 静态补标又用另一套锚点把它贴成
    #   左上格的"第二个拉莫斯" (错标签)。漏一根血条 = 漏一个单位, 所以默认并集。
    BAR_DETECT_METHOD = "union"  # "union"(并集, 默认, 召回优先) / "color" / "template"; 依据见上方与 RULES.md 第 1 节
    # **血条 -> 格位: 全局最优分配 (2026-10-25 新增, 默认开)**。
    # 逐根"找最近格心"的致命缺陷: 同一帧 3 个棋子只检出 2 根血条时, 必然把某根判给邻居已占的
    # 格位; 而 dy (血条下沿 -> 格心) 实测 55~215px 浮动, 跨度 134px > 行间距中位 68px,
    # 单根血条**无法唯一确定行**。改成"每格最多一根血条 + 全局代价最小"后实测 (266 干净帧):
    #   逐根最近邻 精确率 0.9182 / 召回 0.9088  ->  全局分配 精确率 0.9326 / 召回 0.9292
    # (oracle 上限 0.9531 / 0.9495, 代价直接吃真值格心, 不可部署)
    # 分配不上的血条**不猜格位** (宁可漏采不可采错); 关掉它即退回逐根 bar_to_cell。
    # 实现见 `recognize/grid.assign_bars_to_cells`; 常数 ASSIGN_* 同在该模块。
    BAR_ASSIGN_GLOBAL = True
    # **是否同时检测橙棕色血条 (观战/敌方配色, 默认关)** 2026-10-23 加入:
    # 观战截图里「非被观战方」的单位血条是橙棕色(实测填充 205,111,60 / 上沿高光 227,173,143),
    # 只找亮绿时 board/bench 实测**一根都检不出**。自己对局时全部是绿色, 开了只会增加误检,
    # 所以默认关闭, 只有观战采集链路显式传 include_enemy_color=True。
    BAR_INCLUDE_ENEMY_COLOR = False
    # **奖励球识别方案 (统一配置, 2026-10-13)**: 采集器与 show_regions **共用这一份**,
    # 免得"两边方案不同 -> 复现不出结果" (用户实测: show_regions 默认 color, 而采集器用三法并集)。
    #   color   = 仓库颜色法 (蓝/银/金三色阈值) —— **银球只靠它** (balls/ 里没有银模板)
    #   template= 多尺度模板匹配 (balls/*.png, 蓝+金)
    #   sam3    = SAM3 概念分割
    # 组合方式: BALL_COMBINE = "union"(并集, 召回优先) / "intersection"(交集)
    # **用户口径 (2026-10-13)**: "球模板匹配 去除template; SAM3 在验证时再用 sam3 去标注 balls,
    # 普通 collect balls 中不用 sam3 了" -> 普通采集**只用 color** (实测: template 单跑 2.1s,
    # sam3 单跑 ~1s, 而 color 只要 0.27s)。验证/标注路径仍可显式传 sam3。
    BALL_DETECT_METHODS = ("color",)
    BALL_COMBINE = "union"
    # **奖励球检测区域 (2026-11-04 用户 m02276)**: 颜色法原先扫 (250,85,1510,710) 整块,
    # 战斗期会把**对方备战席**上的棋子/旗子误当成奖励球点过去 —— 对方备战席根本没有球。
    # 收紧到"己方棋盘可玩区" (2026-10-13 标定: 真球 x∈[639,1238] y∈[212,475]):
    #   x 560~1310 去掉左右边缘装饰; y 190~645 去掉上方对方半场/石墙(y<190) 与下方备战席(y>645)。
    # 只影响 utils.reward_ball.detect_balls 这条生产链路; recognize.reward_ball.find_balls 的单测不受影响。
    BALL_REGION = (560, 190, 1310, 645)
    BENCH_SLOT_CENTERS = tuple((510 + 112 * i, 754) for i in range(9))
    BENCH_SLOT_MAX_DISTANCE = 65
    # 备战席空/占颜色法阈值 (recognize/bench_space.py): 格子上方 ROI 拉普拉斯方差,
    # 2026-10-17 实测空槽 <=69 / 有单位 >=211, 取 150 留余量.
    BENCH_OCCUPIED_LAP = 150.0
    ROSTER_DIR = EXTRACT_INFO_DIR / "roster"
    ROSTER_SAVE_EVIDENCE = False  # roster 提取是否保存取证截图 (基线+详情卡全屏PNG, 磁盘大户; 排查识别问题时再开)
    # **用户口径 (2026-10-13)**: "怎么又一堆unkown??? 是click_delay时间太短了导致无法识别? 设置为0.3s吧"
    #   实机硬证据 (probe_evidence/1013_130941): 9 张备战席卡片的 DETAIL_STARS 均值全部 180.3
    #   (= 亮棋盘底色, 卡根本没弹出来), OCR 只读到战场上的 '已造成伤' -> 0.15s 确实不够。
    ROSTER_CARD_DELAY = 0.15  # 点击棋子后等待详情卡刷新的秒数；卡片读错/未展开时调大
    ROSTER_CLEAR_DELAY = 0.15  # 收起旧详情卡后等待的秒数；旧卡未关闭时调大
    ROSTER_POLL_SECONDS = 0.3  # 本次提取未完成时，下一次重试前等待的秒数
    BENCH_ITEM_TEMPLATE_DIR = PROJECT_ROOT / "recognize" / "templates" / "bench_items"
    BENCH_ITEM_MATCH_THRESHOLD = 0.85  # 备战席图标模板匹配阈值; 基础/成装锻造器交叉误报实测上限0.834, 需高于此值
    BENCH_ITEM_MATCH_THRESHOLD_COMBAT = 0.71  # 战斗阶段阈值: 光效干扰使真实锻造器得分降至0.72左右, 空格约0.36, 取0.71分界
    # **是否锻造器**的判定阈值 (用户口径 2026-10-12: 优先识别[是否锻造器], 不允许遗漏;
    # 即使误判成装/基础也可接受)。实测 60 帧/63 个真锻造器: 真品下限 0.849, 非锻造器上限 0.848;
    # 0.85 -> 召回 98.4% (会漏), 0.80/0.75 -> 召回 100% (误报 ~1%)。故取 0.75 更偏召回。
    BENCH_ITEM_FORGE_THRESHOLD = 0.75
    OCR_INTRA_OP_THREADS = 2  # 模拟器同时运行时避免三个ONNX模型争抢全部CPU线程
    OCR_INTER_OP_THREADS = 1
    OCR_SERVER_HOST = "127.0.0.1"  # OCR 常驻服务地址 (scripts/ocr_server.py); 环境变量 JCC_OCR_HOST/PORT 可覆盖

    # ---- 统一常驻 API 服务 (server/api_server.py: OCR + SAM3 + Frida 同一端口) ----
    API_SERVER_HOST = "127.0.0.1"
    # 环境变量 JCC_API_SERVER_PORT 可覆盖(2026-10-25 加, 与 JCC_OCR_PORT 同风格):
    # 8765 默认值兼顾旧 OCR 客户端; 但同机另一条线要拿纯 OCR 服务独占 8765 时,
    # 内存链路改走 8766 (server/api_server --port 8766 --services frida), 两边不打架。
    API_SERVER_PORT = int(os.environ.get("JCC_API_SERVER_PORT", "8765"))
    # 单次内存读取的子进程上限; 每实例请求在服务端串行。
    # 2026-10-24 由 45s 提到 120s: 冷读实测 14~23s(极端 169.5s), 45s 太贴线;
    # 一旦超时 subprocess.run 内部会 kill 子进程, 注入体来不及 detach -> 游戏进程里漏
    # 一个 gum-js-loop + JS 堆, 漏 2~3 个后 jemalloc arena 被搞坏, 腾讯 libtcb.so 调
    # free() 时 SIGSEGV(tombstone 实证)。机制见 dev_docs/内存操作/反作弊崩溃根因_注入泄漏_20261024.md。
    FRIDA_API_READ_TIMEOUT = 120.0
    FRIDA_API_HEALTH_CACHE_SECONDS = 10.0  # /health 缓存设备端 ps, 避免每次探测都跑 adb
    # 内存读取是否走**常驻 serve**。
    # 2026-10-27 **默认已关** (True 曾开过半天就回退):
    #   常驻 serve 把 frida 注入体长时间留在游戏进程里, 而腾讯反作弊 libtcb.so 会因
    #   「注入体没 detach 就被硬杀」累积写坏 jemalloc arena, 最终 free() 时秒退游戏
    #   (实测 33 分钟内游戏 pid 换 4 次, 每次读 3~7 次就被信号杀, 无 tombstone 无 am_kill)。
    #   机制与 tombstone 实证见 dev_docs/内存操作/反作弊崩溃根因_注入泄漏_20261024.md。
    # True  = 每实例一个常驻 `get_current_state_by_mem --serve` 子进程, 读一次只花 RPC
    #         时间 (实测 read_ms p50=117~291ms; 原来每读一次重新 attach, 中位 1.78s)。
    #         **只在明确知道风险、且愿意接受游戏被反作弊秒退时开**。
    # False = 老的一次性子进程路径 (每次 attach→读→detach, ~1.8s), 无残留注入体。
    # 也可用 `server/api_server --no-serve` 或 GUI 开关临时覆盖本值。
    FRIDA_API_SERVE = False
    # 常驻 serve **空闲多少秒就自己 detach 退出** (用户 2026-10-27 方案)。
    # 这是让 `FRIDA_API_SERVE=True` 从"全程注入"变成"按批次注入"的那把闸:
    #   * 批内连续读 (间隔 < 本值) 复用同一个注入体, 每次只花 RPC 时间 (~0.1~0.3s);
    #   * 批后空闲满本值, serve 自己写 exiting 标记 → 双检 trigger → detach 后退出,
    #     零残留; 下一批再来重新 attach (付一次 ~1.4s)。
    # 注入窗口因此从"整个 api_server 的寿命"缩到"一批动作的几秒钟"。
    # 为什么是 5s: 一个动作池批次内的两次读 (批前 probe + 批后 probe) 间隔实测远小于 1s,
    # 5s 足够兜住批内抖动, 又短到不会跨回合。
    # 0 = 不启用 (退化成老行为: 常驻到 api_server 退出, 风险见上面 FRIDA_API_SERVE 注释)。
    FRIDA_API_IDLE_EXIT = 5.0
    # ---- 内存读取后端选择 (2026-10-27 加; 同日 m00956 改成与 GUI「读取方法」一一对应的口径) ----
    # **读取方法**共三种 (runner_client 的「读取方法」下拉框就是这三项):
    #   "ocr"      = 老路径: 截图 + OCR 识别棋盘/备战席/玩家列表 —— **不读内存**,
    #                所以它根本不经过 `/frida/state`, 本开关管不到它 (由 GUI/动作自己选路);
    #   "frida_mem"= 注入 frida 读 (老路径, 需要 attach; 注入体写坏 jemalloc arena 会崩游戏);
    #   "proc_mem" = **纯 /proc/<pid>/mem 直读** (实例0 专用, 零注入, 反作弊看不到 ptrace)。
    # 本开关定的是**默认**内存后端: api_server 启动时按它决定起不起 frida;
    # 运行时 GUI 把下拉框的选择随请求发下来 (`body["backend"]`), 请求级覆盖本值
    # (见 server/api_server.py::_state_backend) —— 所以改 GUI 不必重启服务。
    # 为什么要有 proc_mem: frida 注入实测崩过两次 (tombstone 在 free(), 见
    # dev_docs/内存操作/反作弊崩溃根因_注入泄漏_20261024.md); 进程内存直读只依赖
    # "su + /proc/<pid>/mem 可读", 完全不进游戏进程, 不会写坏堆。
    # 两个内存后端返回**完全同构**的 `{"inst": n, "state": <同构 state>}`, 上层一行不改。
    # **proc_mem 只支持实例0** (adb/session 层写死 emulator-5554; 实例1 被另一条会话
    # 用 frida 调试, 去读会互相打断)。
    # 兼容旧值: "frida" 等价 "frida_mem"。环境变量 JCC_MEM_READ_BACKEND 可覆盖。
    # 2026-10-29 用户要求「内存读取方式统一用 proc_mem」-> **默认就是 proc_mem**,
    # 唯一入口是 `python -X utf8 -u -m scripts.proc_mem`（read/health/frame/locate/...），
    # runner / verify / api_server / GUI 全走这一套, 别再各自拼子进程。
    # 关于"扫堆": 常态读帧**一次都不扫**——常驻热路径只用带 pid 校验的缓存地址
    # (`workspace/proc_mem/inst0/{klass,instances}.json`) 读 768B 校验 klass + BattleInit,
    # 0.2~0.3s 出数据; 只有游戏**进程换过**(重启/被 kill) 缓存 pid 对不上时才要
    # `klass` → `locate` 各跑一次建缓存, 之后又是零扫描。所以扫堆是**一次性建缓存**,
    # 不是每次读都付的钱。实机教训 (2026-10-29): 在**对局进行中**做那次大扫描, 有把游戏
    # 读死的风险 (logcat: dd 打开 /proc/<pid>/mem 5s 后 `exited due to signal (9)`, 无
    # Killing 记录), 所以建缓存要在**进对局前/大厅**做, 或接受一次性的风险。
    MEM_READ_BACKEND = os.environ.get("JCC_MEM_READ_BACKEND", "proc_mem")
    # ---- proc_mem 读内存的**传输层策略** (2026-10-30 用户 m02484: 「写成策略模式, config 里设」) ----
    #   "dd"    = 方案1 (默认): 常驻 su 会话 + 设备端 `dd if=/proc/<pid>/mem | base64`。
    #             零注入、最稳; 但设备端**每个合并区间 fork 一个 dd** (~7ms/区间)。
    #   "frida" = 方案2: 设备端常驻 frida agent, 一批地址 = **一条 RPC**, 进程内 readByteArray,
    #             零 fork; 出问题自动降级回 dd (见 scripts/proc_mem/transport.py::_degrade)。
    # 实测拆解 (实例0, 2026-10-30, 只读探针 tempdir/probe_read_breakdown|probe_roundtrip|
    # probe_frida_*|probe_throughput):
    #   · 每批往返 **40ms** —— adb shell `echo hi` 40.7ms、frida RPC ping 40.5ms,
    #     两者一样 => 这是**设备链路延迟**, 换传输层省不掉, 只能减少调用次数;
    #   · 设备端每区间 fork dd ≈7ms (一帧 16 区间 ≈112ms) => frida 能省掉这部分;
    #   · 传输本身不慢: 48KB≈10ms, 64KB/37ms、1MB/175ms (5.8KB/ms);
    #   · 设备上没有 nc/busybox/socat, 也没有编译链 => "设备端 socket 监听服务" 这条路走不通;
    #   · `adb exec-out` 不带 root, 读 /proc/<pid>/mem 回读 0 字节。
    # 环境变量 JCC_PROC_MEM_TRANSPORT 可临时覆盖 (调参/对照实验用, 不必改文件)。
    PROC_MEM_READ_TRANSPORT = os.environ.get("JCC_PROC_MEM_TRANSPORT", "dd").strip().lower()
    # 方向A「投机预取」开关 (2026-10-30 用户 m02738; 实现见 transport.PrefetchTransport)。
    #   "off" = 关闭: 一帧按指针链分层读 4~6 批, 每批一次往返。
    #   "on"  = **默认**: 用**上一帧的请求清单, 一帧只发一次 RPC**, 把整批字节先捞回来当缓存;
    #           上层遍历逻辑一行不改, 请求命中缓存就切给它, 命中不了才真读 (一帧通常 0~1 次)。
    # 2026-10-30 用户 m03442 拍板默认开启:「PROC_MEM_READ_PREFETCH 开启吧」。
    # 实测 (tempdir/probe_merge_gain.py, 同一热帧 28 个请求, 实例0):
    #   dd 原路径 272ms / frida 分层 171ms / **frida 一次 RPC 40.0ms** (链路往返的下限)。
    # 与并行 dd 叠加 (tempdir/bench_dd_modes4.py, 16 区间 x 24KB, 零注入):
    #   4 批串行 328ms -> 1 批串行(预取) 155ms -> **1 批 + 并行 38ms (8.52x)**,
    #   即"零注入也能到 40ms 级", 不再依赖 frida 注入。
    # 正确性: 预取回来的字节与 dd 逐条**逐字节相同**; 地址失效时照旧过不了 klass/id 校验,
    #   抛 NeedColdPath 回退冷路径, 与原来读到脏字节的处理完全一样。
    # 代价: 上一帧还不存在的新对象 (新买棋子/新装备/上人口/换回合) 那一帧要补一次读 (80ms)。
    # 环境变量 JCC_PROC_MEM_PREFETCH=off 可退回分层读 (对照实验用)。
    PROC_MEM_READ_PREFETCH = os.environ.get("JCC_PROC_MEM_PREFETCH", "on").strip().lower()
    # 并行 dd 开关 (2026-10-30 用户 m03147「纯 proc_mem 上限就是 150ms?」)。
    #   串行时每个页区间要等一条 `dd` 结束 (~7ms/区间), 16 个区间白等 ~110ms。
    #   改成 `dd ... of=f$i &` 全部后台 + `wait`, fork 的等待就重叠掉了:
    #     实测 (tempdir/probe_dd_parallel2.py, 同一 16 区间 x 8KB, 零注入):
    #       **串行 139ms -> 并行 66ms (2.10x)**; 回读字节与串行逐区间一致。
    #   代价: 设备侧临时文件 (/data/local/tmp/_pm_*) + 一条 `stat -c %s` 取真实长度
    #   (区间尾部落在未映射页会短读, 长度表保证 host 只采信有效前缀, 绝不串位)。
    # 环境变量 JCC_PROC_MEM_DD_PARALLEL=off 可退回串行 (对照实验用)。
    PROC_MEM_DD_PARALLEL = os.environ.get(
        "JCC_PROC_MEM_DD_PARALLEL", "on").strip().lower() not in ("0", "off", "false", "no")
    # proc_mem 单次读取上限秒。老路径 (每次起子进程 + 重读所有玩家) 同局内实测 2.8~2.9s;
    # 换局/换进程要重定位 (dump 42MB metadata + 扫段), 实测 25~35s, 故不能设太小。
    PROC_MEM_READ_TIMEOUT = 180.0
    # proc_mem 走**常驻热路径** (`scripts/proc_mem/hot.py`): 复用一个 su 会话 + 缓存
    # pid/maps/klass/BM 地址/玩家名字, 并且只读"当前视野那一家"的棋盘棋子与装备。
    # 目标是把单次读从 3.7s 压到 0.2~0.3s (frida 常驻 serve 的量级)。
    # 关掉 (False) 就退回"每次起子进程跑完整 state.py"的老行为 (便于对照排查)。
    PROC_MEM_USE_DAEMON = os.environ.get("JCC_PROC_MEM_DAEMON", "1").strip().lower() not in (
        "0", "false", "no", "off")
    # 常驻进程空闲多少秒后自己退出。0 = 不退出 (默认): 本进程零注入, 生命周期跟 api_server
    # 走最省事; 设成正数后空闲退掉, 下一次读要重新拉起 (~0.6s, 只有空闲后的第一次贵)。
    PROC_MEM_DAEMON_IDLE = 0.0
    # 热路径单次等待上限秒 (2026-10-28 加, 用户实机报「点 [提取观战奕子信息] 后 GUI 卡死、
    # 按钮永久变灰」): 常驻进程正常 0.2~0.3s, 首次含拉进程 ~2.2s。
    # 超时就认输落慢路径 —— 旧代码用 PROC_MEM_READ_TIMEOUT(180s) 等热路径, 一旦常驻进程
    # 卡住/退出, GUI 会连着静默 3~6 分钟, busy 态一直不清 = 用户眼里的死机。
    # 2026-10-29 从 25s 提到 90s: 常驻进程内部遇到 need_cold 时**自己要跑一次冷定位**
    # (批量扫段实测最坏 36s), 25s 会在它跑到一半时放弃, 然后慢路径又从头扫一遍 =
    # 一次读变 70s+, runner 直接判"内存不可用"。90s 留足余量又不至于真卡死时等太久。
    PROC_MEM_HOT_TIMEOUT = 90.0
    # 同一实例的读锁等待上限秒。等不到 = 上一次读卡住了, 直接抛中文可操作错误;
    # 旧代码无超时 acquire, 会无声无限等 (busy 态同样永远清不掉)。
    PROC_MEM_LOCK_TIMEOUT = 30.0
    SAM3_CONDA_ENV = "spine_2d_animation"          # SAM3/torch 所在环境 (主环境不装 torch)
    SAM3_CONDA_BAT = r"D:\1_softwares\conda\condabin\conda.bat"   # 为空则用 PATH 里的 conda
    # **首选**启动方式: 直接调用该环境的 python.exe —— 绕开 `conda run` 的 activate。
    # 实测从 jcc_robot 环境里 `conda run -n spine_2d_animation` 会失败:
    #   "Failed to run 'conda activate "D:\conda\envs\spine_2d_animation"'" (父进程带着
    #   CONDA_PREFIX/CONDA_SHLVL 等变量时 activate 容易炸)。直连解释器最稳、也少一层开销。
    # 留空则退回 `conda run -n <SAM3_CONDA_ENV>`。
    SAM3_PYTHON = r"D:\conda\envs\spine_2d_animation\python.exe"
    SAM3_PROJECT_DIR = r"E:\projects\pycharmProjects\spine_2d_animation"  # worker 的 CWD(模型相对它)
    SAM3_MODEL = "models/sam3.pt"    # 相对 SAM3_PROJECT_DIR
    SAM3_CONF = 0.5                  # 概念分割置信度阈值
    SAM3_READY_TIMEOUT = 300.0       # 首次加载模型(冷启动)上限秒
    SAM3_SEGMENT_TIMEOUT = 180.0     # 单次推理上限秒
    SAM3_IO_DIR = "workspace/sam3_io"   # 请求图/掩码/result.json 落盘目录

    # ---- 无血条奕子补检 (recognize/bar_less.py: 血条 + SAM3 结合) ----
    # 背景: 两个奕子重叠时后面那只的**血条可能被前面的大模型遮住**(实测 own_row3-col4),
    # 血条法看不到它 → 点击流水线也不知道它存在 (不会被卖/换位/装备)。
    # 策略: **血条检测为主, SAM3 只补"血条没检出"的己方格位**; 每条结果带 source 字段。
    RECOGNIZE_BAR_LESS = "sam3"      # "off" | "sam3" —— **默认开启**; 服务不可用时自动静默跳过
    BAR_LESS_SCOPE = "board"         # 补检范围: "board" | "bench" | "board,bench"
    BAR_LESS_MIN_AREA = 300          # 单格裁剪里主体掩码面积下限 (小于此视为噪声)
    BAR_LESS_AREA_RATIO = 0.6        # 或达到"最大掩码面积 x 该值"才算主体
    BAR_LESS_TIMEOUT = 20.0          # 单格 SAM3 调用超时秒
    BAR_LESS_EVIDENCE = True         # 存证: 每格裁剪图 + 框叠图 (复现成本 >> 存储成本)
    BAR_LESS_DEBUG_RATIO = 0         # >0 时打印"未达阈值"的格子详情 (调阈值用)
    # 详细调试输出 (默认**关**): 开启后才打印"候选数 / 每条丢弃理由 / 完成汇总 / 未达阈值";
    # 默认只打印**"血条没检出、SAM3 补出了奕子"**那一行 (用户口径: 平时日志别刷屏)。
    # 也可用环境变量 JCC_BAR_LESS_DEBUG=1 临时打开。
    BAR_LESS_DEBUG = False

    # ---- SAM3 概念词的已知脆弱项 (实测触发 RecursionError: maximum recursion depth exceeded) ----
    # SAM3 对**某些概念词**会在后处理里递归爆栈, 与图片大小/内容无关 (query: "question" 必然失败);
    # 另外"小图 + 任意词"也可能触发 (实测 orb 在 32x32/128x128 上会炸, 在整帧正常) —— 所以调用侧
    # 需要"换词 + 换尺寸"重试, 而不是直接把错误抛给用户。draft_sam3 会跳过本表里的词并提示。
    # 实测 (2026-10-11, 整帧 2026x1140): 下面这些词**必然** RecursionError; 与词长/常见度无关
    # (mark/sign/star 又短又常见也炸) → 是 SAM3 词表里某些 token 走了坏路径。
    SAM3_FRAGILE_WORDS = ("question", "question mark", "icon mark", "mark", "sign", "text", "letter",
                          "label", "arrow", "badge", "star", "punctuation",
                          "glowing orb", "ball", "sphere", "item", "sword", "shield")
    # 实测**可用**的概念词 (供 --text 选词参考): symbol/number/icon/circle/logo/orb/character 均正常。
    # 找"?"这类符号图标 → 用 "symbol" (问号属于符号); "icon"/"number"/"circle" 亦可。
    # 概念词候选 (按顺序试): SAM3 在**个别裁剪图**上会对某个词抛 RecursionError (已知怪癖),
    # 换个短常见词往往就能过 → 依次重试, 全失败就跳过该格 (不中断整体补检)。
    BAR_LESS_TEXTS = ("character", "creature")
    # 只接受落在这个矩形内(全局坐标)的 SAM3 框 —— 排除左侧小小英雄头像带/右侧玩家面板/商店,
    # 实测不设白名单会把"小小英雄头像/玩家 HUD"误补成棋盘奕子。
    BAR_LESS_BOARD_AREA = (500, 280, 1530, 700)
    BAR_LESS_MIN_BOX = 32            # 框短边下限 px (滤碎片); 40→38→32: 实测 r3c4 奕子框宽 38、
                                     # 阿卡丽(r2c7)框宽 35, 都是真实奕子却被 40 卡掉; 32 能放过它们、
                                     # 同时挡住装备图标(18~20px)这类真碎片 (用户 2026-10-18 拍板)
    BAR_LESS_MIN_BOX_AREA = 800      # 框面积下限 px^2
    BAR_LESS_MAX_BOX = 200           # 长边上限 px (滤背景/地形大块): 实机 SAM3 把地形当成
                                     # character 检出 238x215 / 245x234, 奕子最大约 120x150
    BAR_LESS_MAX_BOX_AREA = 20000    # 面积上限 px^2 (238x215=51170 这种大块直接挡)
    BAR_LESS_ASPECT = (0.3, 3.5)     # 框宽高比范围
    BAR_LESS_OWN_GAP = 40            # "血条归属"判定: 框顶与血条底的允许间隙 px
                                     # 30→40: 实测 71 对偏移中位 15、IQR 6.5、范围 -3~51;
                                     # 30 卡掉了瘦长奕子阿卡丽(间隙 37, 被判成非归属→重复),
                                     # 40 能覆盖它、同时仍把卡密尔(间隙 46)判成"非归属"(正确补出)
    # ---- 血条 vs SAM3 不一致样例 (用户 2026-10-18) ----
    # 每帧比对"血条检出的奕子"与"SAM3 检出的框", 两类不一致各存一份原图+标注+JSON 到
    # workspace/tmp/sam3_vs_bar_samples/, 供后续分析是"血条遗漏"还是"SAM3 阈值太严/太松":
    #   * 血条有 SAM3 无 (橙框) -> 可能是 SAM3 漏检/阈值太严;
    #   * 血条无 SAM3 有 (蓝框) -> 可能是血条遗漏 (或 SAM3 误检)。
    BAR_LESS_SAVE_SAMPLES = True

    OCR_SERVER_PORT = 8765

    # --- OCR 结果缓存开关 (recognize/ocr.py): 默认开启, 同图同参数的重复识别直接命中.
    # 当前为优化识别速度暂时关闭——缓存键需对整图(约6MB)做md5, 每帧多次OCR调用时开销显著;
    # 速度优化完成后改回 True 即恢复缓存.
    USE_OCR_CACHE = False
    # USE_OCR_CACHE = True

    # --- OCR 设备选择 (torch device 风格): True=强制CPU; False=强制GPU(不可用回落CPU并警告);
    # None=自动, 优先尝试GPU (需 pip install onnxruntime-gpu + NVIDIA 显卡), 不可用静默用CPU.
    # 对 OCR 常驻服务进程同样生效, 改后需重启服务.
    USE_CPU_OCR = None
    # USE_CPU_OCR = True

    # --- OCR 服务进程内存上限 (GB): 超了就丢模型 + gc, 下次请求懒加载重建; **不杀进程**.
    # 实测依据 (2026-10-13): 机器总内存 **31.9GB** (2x16GB@3200); OCR 服务进程
    #   (server/api_server.py) 稳态只有 **约 1.08GB**; 吃内存的大户是 sam3_worker.py (4.9GB)、
    #   游戏本体、PyCharm (2.6GB)、浏览器 —— 跟 OCR 无关 (那次 MemoryError 是**整机**内存压力,
    #   不是 OCR 自己泄漏).  所以这个上限只是"病态泄漏"的兜底, 不是主防线;
    #   主防线是 ocr_backend 里"MemoryError -> 丢模型 + gc + 重试一次".
    # 取值原则: 比稳态高数倍, 且明显低于物理总量 (否则永远不触发 = 等于没装).
    #   默认 4.0 ≈ 稳态 4 倍, 还给其余进程留 28GB.
    # 想调不用改代码: 设环境变量 JCC_OCR_MAX_RSS_GB=6, 再重启 api_server 即可.
    # 0 = 关闭看门狗.
    OCR_MAX_RSS_GB = float(os.environ.get("JCC_OCR_MAX_RSS_GB", "4.0"))

    # --- 整机内存闸门 (用户 m09235 事故 2026-10-14 02:58: CUDA OOM -> CPU 回退 -> `bad allocation`
    #   -> `fatal : Memory allocation failure` **abort**, 整轮采集当场死, 连 Python traceback 都没有) ---
    #   上面那个 OCR_MAX_RSS_GB 只管**服务进程自己**的 RSS (其注释也写明那次是整机内存压力),
    #   而那次死的是**采集进程** —— 它在常驻服务不可用时起了本地 GPU 引擎 ⇒ 闸门必须读整机
    #   (新工具 `utils/mem_guard.py`, psutil)。
    # * `OCR_LOCAL_GPU_WHEN_FALLBACK`: 本地引擎只是"服务不可用时的兜底"时, 默认**用 CPU**
    #   (`JCC_OCR_LOCAL_GPU=1` 才允许抢显存) —— CUDA OOM 会让 ORT 回落到 CPU, 反而把压力全推给
    #   主机内存, 就是事故里那条雪崩链。
    # * `OCR_MIN_FREE_GB`: 可用物理内存低于它就**不许**建 GPU 引擎 (CPU 仍可用, 只慢 ~1s/次)。
    # * `COLLECT_MIN_FREE_GB`: 采集主循环的收工线 —— 低于它干净收工, 别等 CRT abort 丢帧。
    OCR_LOCAL_GPU_WHEN_FALLBACK = os.environ.get("JCC_OCR_LOCAL_GPU", "0") == "1"
    OCR_MIN_FREE_GB = float(os.environ.get("JCC_OCR_MIN_FREE_GB", "2.0"))
    COLLECT_MIN_FREE_GB = float(os.environ.get("JCC_COLLECT_MIN_FREE_GB", "1.0"))
    COLLECT_MEM_LOG_EVERY = int(os.environ.get("JCC_COLLECT_MEM_LOG_EVERY", "10"))

    # --- 分步耗时打印: recognize/extract.py 打 "--- extract.<步骤> --- 耗时s";
    # extract_info/roster.py 的 [计时]/[耗时] 行同受此开关控制 (内部走 logger, DEBUG 级,
    # 默认关闭不刷屏); 运行时改即时生效.
    EXTRACT_TIMING_DEBUG = False

    # ---- 商店快速识别 (2026-11-12 用户要求: 优化商店通用识别, 打掉 1000ms) ----
    # 商店 5 格位置固定 (regions.SHOP_SLOT_LEFTS), 字体固定, 没必要用通用 OCR 的"文字检测(det)"
    # 去全带找文字位置。开启后: 按固定格裁名称行小图 -> ocr.recognize_line (跳过 det, 只识别)
    # 拿卡名 -> 查库属性。识别出的槽位名不完整时**自动回落原 extract_shop 兜底** (数据正确性优先)。
    # 关闭 = 完全走原链路 (默认关, 新增开关一律默认关不影响原行为; 实测验证后再默认开)。
    SHOP_FAST_RECOGNIZE = False

    # ---- 商店图形学加速识别 (2026-11-12 用户要求: 图形学定文字rect + 免det rec + 形近字轻校验) ----
    # 为什么: det(文字检测)是整条商店识别唯一的慢点 (598ms, 实测 DB 检测模型在大横条输入上慢);
    #   而商店 5 格位置/字体完全固定。本路径用"白字图形学"在固定槽内求出每格文字的精确 rect
    #   (<1ms/格), 再逐格裁出 -> 2x -> ocr.recognize_line (use_det=False 只识别不检测) -> 查库。
    #   实测 10 张真商店图: 原 extract_shop 平均 814ms -> 本路径 84ms, 命中 48/49=98%。
    # 数据正确性 (铁律): 本路径**从不猜** —— 读不出就是 None; 只有当读出的名字**不在卡池**时,
    #   才在同字数(长度相等)的卡池名里找**唯一**形近候选做校正 (B, 属"校验"非"猜"; 合法名绝不改,
    #   多候选/等距则不校正保持原样)。任一槽读不出或校正后仍非法 -> 该槽 None, 由上层决定是否回落原路径。
    # 依赖: SHOP_FAST_RECOGNIZE 开关无效时本开关也无效 (二者都在 _extract_shop_narrow 里生效)。
    # 默认开 (2026-11-12 用户拍板: 100 张同刻配对干净图上, 新方法 vs 原 det 方法 vs 内存
    #   三方 500/500 槽 100% 全一致; 新方法中位 111ms vs 原方法 663ms, 提速约 5 倍, 达标)。
    #   读不出仍回落原 extract_shop 兜底 (数据正确性优先, 宁慢不错)。
    SHOP_GEO_RECOGNIZE = True
    # 用户人工标定的 5 个商店槽名称区固定坐标 (labelme, 1920x1080 基准, (x1,y1,x2,y2))。
    # 名字行真实 y≈1025~1058 (上方 998~1024 是羁绊标签残余 + 空白, 不含名字)。
    # 与 regions.SHOP_SLOT_LEFTS 等宽切分不同: 这是"刚好框住名字文字"的紧框, 供图形学路径用。
    SHOP_GEO_SLOT_RECTS = (
        (502, 1025, 715, 1058),   # 槽1
        (730, 1024, 948, 1058),   # 槽2
        (970, 1024, 1181, 1058),  # 槽3
        (1213, 1026, 1414, 1058), # 槽4
        (1440, 1025, 1647, 1058), # 槽5
    )
    # 图形学路径: 槽内白字文字 rect 的最小宽度 (px), 太小视为噪声不裁 (整槽无字)。
    SHOP_GEO_MIN_TEXT_W = 8

    # ---- 回合剩余时间本地预测缓存 (recognize/extract.RemainTimeCache, 用户口径 2026-10-17) ----
    #   开启后: 第一次在某 (回合号, 阶段) 读到 remain_time 时记入缓存, 之后同一 (回合号, 阶段) 内
    #   不再跑 OCR, 直接用 `首值 - 本地已过秒数` 推算剩余时间; prep 的过期门槛 = 0 (推算到 0 才重读,
    #   prep 必然走完不用怕被延长), combat 用 REMAIN_CACHE_COMBAT_KEEP (combat 可能被突然缩短,
    #   也可能被延长 = 加时赛, 所以推算到 5 秒就重读)。默认关闭 (新增开关一律默认关, 不影响原链路)。
    REMAIN_TIME_CACHE = False
    # combat 阶段缓存保留门槛 (秒): 推算剩余 <= 该值 就重读 OCR —— 应对"15s 突然变 3s"/"3-2-1s
    #   又跳回 20s"的加时赛; 用户口径: "提取时间 3-2 阶段剩余 20s, 则改为 remain_time 5s 才过期"。
    REMAIN_CACHE_COMBAT_KEEP = 5.0
    # ---- runner / runner_client 调试开关 (命令行 --debug-cost-time / --debug-yolo-and-blood-bar) ----
    # 两个都不改生产行为, 只在排查时打开; 由入口把命令行参数落到这里, 各模块统一读 default_config.
    # DEBUG_COST_TIME: 给**每条日志行**插 `--- tt.now: [Xs] ---`(脚本运行时间), 形如
    #   `[动作] --- tt.now: [12.4s] --- 奕子详情提取 mode=default` —— 靠包一层 stdout 实现
    #   (见 utils/tt_log.py), 所以 roster/dynamic_probe/runner 内部的 print 也自动带上, 不用逐个改。
    DEBUG_COST_TIME = False
    # DEBUG_YOLO_AND_BLOOD_BAR: 每次动态提取的**起手定位**(roster.locate_roster)后, 把 YOLO 识别框
    #   (绿) 与血条识别框 (红) 画到**截图A**副本上, 存本地临时文件并打印路径 (排查"哪一格漏了/多框")。
    DEBUG_YOLO_AND_BLOOD_BAR = True
    # --- 识别取证 (extract_info/roster.py): 每次提取落一个时间戳目录, **里面分 成功/失败 两个子文件夹**,
    # 每个样本 = 槽位裁剪 + 详情卡裁剪 + .json (证据: 卡面 OCR 原文/解析结果/点击差值/当次阈值/耗时),
    # 便于人工并排对比"成功和失败差在哪"; 路径: workspace/extract_info/recognize_debug/<时间戳>/{成功,失败}/
    SAVE_RECOGNIZE_DEBUG = {"fail": True, "success": True}   # 两个都存; 想省盘就把 success 改 False
    RECOGNIZE_DEBUG_SUCCESS_MAX = 40   # 每次提取最多存多少个"成功"样本 (每个约 200KB, 防爆盘)
    ROSTER_POPULATION_REGION = (905, 207, 1150, 298)
    # 清卡点 (收详情卡用的"点空地") —— 2026-11-02 用户 m01153 改到**小小英雄出生点**:
    #   旧值 (1350, 835) 在备战席下方 HUD, 实测两个毛病: ① 位置太靠右下, 清卡与"点左上角奖励球
    #   → 小小英雄走过去捡"的节奏打架 (用户实机: 2-1 就掉在上面的球, 拖到 3-2 才捡到);
    #   ② 靠近商店/操作区, 点多了容易误触。
    #   新值 = 棋盘左下角、己方(4,1) 左侧一点的空地 (小小英雄出生点旁), 点了没有任何 UI 交互。
    ROSTER_CLEAR_POINT = (470, 640)
    # 清卡最小间隔秒 (用户 m01176: 「点击空白处的操作太频繁了」): 旧实现每次取帧前都点 (≈1s 一次),
    #   既刷屏也长期抢键鼠锁 (捡球线程拿不到锁)。改成"距上次清卡不足该秒数就跳过"。
    ROSTER_CLEAR_MIN_INTERVAL = 3.0
    STAGE_SEARCH_REGION = (560, 0, 850, 65)  # 左界含选装备等顶栏偏左的回合号 (3-4 轮抽实测起点约605, 原620会截断)

    # ---- YOLO 单次前向识别我方奕子 (recognize/yolo_units.py) ----
    # 背景 (2026-10-12): 现网阵容提取 = **逐格点击读详情卡**, 12~18 次点击 × 0.8~1.0s ≈ 16~25s,
    # 这就是"回合开始后要等约 20s 才知道场上有谁"的唯一原因。静态血条路径只要 0.32~0.45s 且
    # 位置 100% 准, 但只认得出 13/74 个棋子 (units/templates 只有 13 张头像)。
    # → 用 YOLO **一次前向**替掉"定位 + 身份", 目标 <1s。训练/导出见 train_yolo/train.py,
    #   验收见 scripts/bench_yolo_roster.py。
    # ---- 动态提取策略 (用户 2026-10-18 要求 config 可控) ----
    # "yolo_detail": YOLO 定位 + 可信格免点击 + 其余点详情卡 (默认; 快慢折中)
    # "detail":      纯点击详情卡 (禁用 YOLO 免点击/快路; 每格都点, 最准但最慢 14~25s)
    EXTRACT_STRATEGY = "yolo_detail"
    YOLO_UNITS_ENABLED = True         # 总开关 (2026-10-15 用户 m09594「你开」: 信任表 v1 就位后打开)
    # 图色方案脚手架默认用 11n (11MB, 速度优先): 11n mAP50 0.774 / 11s 0.805。
    # 想要精度可把 models/yolo_v4.1_units_11s.onnx (38MB) 放进来并把这一行改回 11s;
    # 信任表按模型名自动选 (config/yolo_trust/<模型文件名>.json)。
    YOLO_UNITS_ONNX = PROJECT_ROOT / "models" / "yolo_v4.1_units_11n.onnx"
    # ---- 【功能点 → YOLO 模型】每个功能点用各自的权重 (用户 m02239/m02354, 2026-11-01) ----
    # 背景: 冻结类别表 train_yolo/classes.json 共 73 类, 表尾依次是
    #   ... 基础装备锻造器, 成装锻造器, ball, 小小英雄, unknown, 其他小小英雄, 石皮树, 生命花
    # yolo_v4.1_units_11s 只有 67 类 → **缺表尾 6 类 (含 ball、小小英雄)**, 既不认球也不认小小英雄;
    # yolo_v2.1_balls 有 70 类 → 缺最后 3 类 (其他小小英雄/石皮树/生命花), **ball 与 小小英雄 都有**。
    # 读取入口统一走 `recognize.yolo_units.model_for("<功能点>")`, 不要在业务代码里直接写路径。
    #   功能点     常量                 默认模型
    #   units      YOLO_UNITS_ONNX      11s  认奕子/上阵单位名单 (精度最高, 见上一行)
    #   legend     YOLO_LEGEND_ONNX     v2.1 认小小英雄 (捡球排序用, 11s 缺这个类)
    #   ball       YOLO_BALL_ONNX       v2.1 认奖励球 (捡球用, 11s 缺这个类)
    #   debug      YOLO_DEBUG_ONNX      v2.1 采集/调试图框选 (球 + 小小英雄, 一次推理同时拿到)
    YOLO_LEGEND_ONNX = PROJECT_ROOT / "models" / "yolo_v2.1_balls.onnx"   # 认"小小英雄" (11s 缺这个类)
    YOLO_BALL_ONNX = PROJECT_ROOT / "models" / "yolo_v2.1_balls.onnx"     # 认"奖励球 ball" (11s 缺这个类)
    YOLO_DEBUG_ONNX = PROJECT_ROOT / "models" / "yolo_v2.1_balls.onnx"    # 调试图: 球+小小英雄 (11s 两个都缺)
    YOLO_UNITS_IMGSZ = 1280           # 必须与导出 ONNX 时的 imgsz 一致
    YOLO_UNITS_CONF = 0.35            # 检出置信度阈值
    YOLO_UNITS_IOU = 0.5              # NMS IoU 阈值
    YOLO_UNITS_MIN_SCORE = 0.45       # 低于它视为"没认出类别" → name=None (不猜, 交给兜底探测)
    # ---- 信任表阈值策略 (用户 2026-10-29 m06694「做成 config 可配置, [整体/分类/混合]三策略」) ----
    # global : 全局单一阈值 —— 所有可信类共用一个 min_score, 只保证**总体**正确率达标
    #          (免点率最高, 个别小类可以很差)
    # class  : 逐类独立阈值 —— 每类各自卡正确率, 样本量不足的类永不赦免
    #          (正确率最高, 但小类很容易整类被赦免掉 → 免点率掉得多)
    # hybrid : 全局阈值 + 弱类降级 —— 先取全局阈值, 再把"在全局阈值下实测正确率 < WEAK_ACC
    #          且命中数 >= WEAK_SUPPORT"的类判为永不赦免 (兼顾: 保住小类正确率, 又不像 class 那样
    #          把中等类也一起砍掉)
    # 选哪个由「免点率/正确率 的 F-beta 最高」决定 (scripts/diag/_trust_strategy_pick.py 可复算):
    #   2026-10-29 实测 (11s @conf 0.02, val 730 帧/4997 框; 按实验 5 折 OOS, **10 个随机折分取均值**):
    #     global 93.37%[93.12~93.60]/99.02%   class 90.84%[89.71~91.49]/99.34%   hybrid 92.78%[92.28~93.08]/99.02%
    #   -> F1 / F2 / F0.5 三个指标都选 **global(整体)**; 故默认 global。
    #   **单次折分会漂 1~2 个百分点**, 引用请用均值+范围 (scripts/diag/_trust_strategy_pick.py 默认 10 次重采样)。
    YOLO_TRUST_STRATEGY = "global"
    YOLO_TRUST_METRIC = "f1"           # 选策略时用的指标: f1 / f2 / f0.5 (f0.5 = 偏重正确率)
    YOLO_TRUST_WEAK_ACC = 0.90         # hybrid: 全局阈值下该类正确率低于此值 → 降级为永不赦免
    YOLO_TRUST_WEAK_SUPPORT = 5        # hybrid: 命中数 >= 此值才参与"弱类"判定 (太少就是噪声)
    # ---- 低阈值复核 (用户 2026-10-20 要求) ----
    # 场景: 血条/SAM3 已经定位到"这里确实有个奕子", 但 YOLO 主阈值没给名字、回溯法也补不上。
    # 实测 2-6 的 r2c3 芸阿娜: 模型**分类正确**, 只是 conf=0.236 < 0.35(检出阈值) / 0.45(命名阈值),
    # 被两层闸门挡掉 → unknown。这里用低阈值**再解一次同一帧的检测头输出**
    # (复用同一次前向, 不额外推理), 只在"定位到但无名"的格附近找候选类别; 命中就补名并**标记低置信度**
    # (调试图黄字 `低阈值: 名字 0.24`, prompt 里也标注), 绝不当作正常识别结果。
    YOLO_UNITS_LOWCONF = True
    YOLO_UNITS_CONF_LOW = 0.10        # 低阈值复核的检出阈值
    # 低阈值复核的**命名下限** (用户 2026-10-21 实机反馈): 只看检出阈值 0.10 时, 纯噪声框也会被
    # 当成名字 —— 实测 r1c1 的 峡谷迅捷蟹 被 YOLO 给了个 conf=0.108 的 `乐芙兰` 框, 于是一个
    # 0.11 的胡思乱想被当成真名 (比 unknown 更糟: unknown 不会误导卖牌/上阵)。0.20 是实测分界:
    # 2-6 的 芸阿娜 真名候选 0.236 能过, 0.108/0.13 这种噪声挡掉。
    YOLO_UNITS_LOWCONF_MIN_SCORE = 0.20
    YOLO_UNITS_LOWCONF_RADIUS = 70.0  # 候选框中心离该格中心的像素上限
    YOLO_UNITS_LOWCONF_EXCLUDE = ("unknown", "ball", "小小英雄")   # 这些类不给名字
    # ---- 头像模板匹配的阈值 (用户 2026-10-21 拍板) ----
    # 用户口径原话: "低阈值的就 0.1 吧, 我们必须给低阈值的图片进行分类, 得出 top1; yolo 的低阈值也是 0.1"。
    # 透明模板库 (mask 口径) 的分数整体比旧整图 CCOEFF 低约 0.1 (见 units/matcher.py docstring:
    # r1c1 同图 旧口径 蟹 0.247/乐 0.288 vs 透明库 蟹 0.539/乐 0.281), 所以两档分开:
    #   0.55 = 高信命名线 (能比出来才命名, 不够分留 unknown —— 数据正确性最高优先级);
    #   0.10 = **低阈值复核线**, 落在 (0.10, 0.55) 的格不许留 unknown, 必须按 top1 给名字并标记低置信。
    # 与 YOLO_UNITS_CONF_LOW 对齐, 两条链路同一个口径。
    MATCH_THRESHOLD = 0.55
    MATCH_LOWCONF = 0.10
    # 模板兜底 (tpl_lowconf) 专用的采信线 —— 用户 2026-10-21 阈值扫描结论。
    # 在 132 帧/963 幅 GT 的测试集上扫了 30 组 (YOLO低阈值 0.05~0.30) × (模板阈值 0.10~0.70):
    #   0.10 → 认对 936 / 错名 26 / 未命名 0
    #   0.30 → 认对 936 / 错名 25 / 未命名 1   ← 最优 (错名少 1 个, 代价只是 1 个格留 unknown)
    #   0.50 → 认对 935 / 错名 25 / 未命名 2
    # 也就是"模板兜底低于 0.30 的基本是猜", 只留高分那一档。
    MATCH_LOWCONF_TPL = 0.30

    # 观战采集 debug: 保存每个 slot 的 点击前(b1) / 点击后(b2) 截图 + 截图A 原图。
    #   用户 m08020: "把 b1 和 b2 保存下来... 我问你的时候要能拿的出来 b1/b2 俩图作为对比"。
    #   打开后会**强制**每 slot 多截一张 b2 (约 +0.3s/slot), 只在排查识别问题时开。
    SPECTATE_SAVE_AB = False

    # ---- 弈子位置可信区 (用户 m04726 要求修"涂灰涂到右侧玩家面板") ----
    # 实测依据 (2026-10-22, 观战实验 experiment_20261022_134139_spectate_only_verify 全 124 个落盘
    #   objects + 16 帧 `_locate_units` 结果):
    #     落盘对象中心 x: min/中位/max = 476 / 788.5 / 1263; 右边界 max = 1325
    #     棋盘第 8 行 (最右) 第 7 列中心 = 654 + 6*120 = 1374, 框右边界 ~1374+60 = 1434
    #   右侧「玩家信息面板」实测 x≈1442~1660, **整块都在棋盘右边界之外**, 但老闸门
    #   `320 <= cx <= 1560` 放过了面板中心 1549.5 -> SAM3 把面板当成 character 补进 out
    #   -> 落盘时被当"未标注目标"整块涂灰 (212x313 的大灰块, 三帧: 4-1 / 6-5 / 6-7)。
    #   => 用棋盘几何推导出来的**硬边界**, 而不是拍脑袋的 1560。
    UNIT_CENTER_X_RANGE = (320, 1420)   # 弈子**中心** x 允许范围 (含余量; 面板中心 1549 被挡)
    UNIT_CENTER_Y_RANGE = (200, 900)    # 弈子**中心** y 允许范围 (备席 y≈754/棋盘末端 y≈663)
    # SAM3 单 mask 最大允许尺寸 (像素): 实测棋子框 p95 = 124x156、max = 170x188;
    #   右侧面板给出来的是 211x315 —— 单独这一条也能挡掉, 与位置闸互为双保险。
    UNIT_MAX_SIDE = 200

    # 观战采集: 详情卡上**读得到、但不在棋子库(units/db)里**的衍生物名字 (用户 m01256)。
    #   实测 r1c2/r1c3 的详情卡 OCR 读到 "石皮树" / "生命花" (raw 原文都对), 但
    #   `units.db.find_unit()` 查不到 -> 被主库校验判成 `un_match`("读到文本但不在库")。
    #   这两个是 YOLO 类别表里已经有的召唤物 (train_yolo/classes.json 的 71/72),
    #   属于"认得出来的类别", 不该算未匹配 —— 命中本表就按 ok 收, 名字用读到的原文。
    SUMMON_NAMES = ("石皮树", "生命花")

    # ---- 难例自动登记 (用户 2026-10-21 m06193 要求) ----
    # 场景: 血条/SAM3 定位到该格有单位, 回溯 + 低阈值复核都补不上名字, 但 YOLO 在低 conf 下
    # **最高候选是 unknown 且分够高** —— 说明"模型看到东西了, 但它不认识" (实测 r1c1 峡谷迅捷蟹:
    # unknown 0.217 / 乐芙兰 0.108 / 真值 迅捷蟹 0.079)。这类帧是重训最该补的 hard example,
    # 自动裁小图 + 记候选分布, 人工标注后可直接进训练集。**只登记证据, 不猜名字** (AGENTS §1)。
    HARD_SAMPLE_ENABLED = True
    HARD_SAMPLE_DIR = "workspace/tmp/hard_samples"
    HARD_SAMPLE_MIN_SCORE = 0.15      # unknown 候选分 >= 它才登记 (低于它的多半是背景噪声)
    HARD_SAMPLE_MAX_PER_FRAME = 6     # 单帧最多登记几条, 防刷
    YOLO_UNITS_MAX_DET = 20           # 单帧最多保留几个框
    YOLO_UNITS_PROVIDER = None        # None=自动 (CUDA 优先, 回落 CPU); 或 "CPUExecutionProvider"
    YOLO_UNITS_SHORTCIRCUIT = True    # True: 可信类别免点击; **没被信任的格仍然点击** (用户 m09594 选项 c)
    # 用户 2026-10-22 m00621/m00802: 「点击二次识别」**分两种模式**, 不再是单一开关 ——
    #   · runner 口径 (`PROBE_CLICK_UNKNOWN_RUNNER`): 正式对局跑图, **绝对速度优先**,
    #     YOLO 认不出的格不点详情卡, 直接落 unknown (2026-10-20 用户口径);
    #   · collect 口径 (`PROBE_CLICK_UNKNOWN_COLLECT`): 采集数据, **绝对正确优先**,
    #     认不出的格必须二次点击识别, 不许落 unknown (否则数据集里全是 unknown, 训练无从下手)。
    # 实际生效值 `PROBE_CLICK_UNKNOWN` 由 `set_collect_profile()` 按进程设置:
    # collect_v2 开跑前调 True; 正式 runner 进程不调用 -> 保持 runner 口径。**分进程, 互不影响**。
    # 用户原话 (m00802): "我要求关掉, 是因为, 我在正式runner实际对局中, 需要绝对速度关掉.
    #                  而我们采集数据脚本 collect_v2, 是要保证绝对正确, 所以要开启!"
    PROBE_CLICK_UNKNOWN_RUNNER = False
    PROBE_CLICK_UNKNOWN_COLLECT = True
    PROBE_CLICK_UNKNOWN = PROBE_CLICK_UNKNOWN_RUNNER
    # 用户 2026-10-20: 同一帧只做一次定位 (YOLO∪血条∪SAM3+回溯+debug出图)。
    # 实测 [default 提取] 11.35s 里 SAM3 补检跑了 3 遍 (6.7s): 静态层 1 遍 +
    # 动态层截图A 1 遍 + 点击后 retry 1 遍; 画面没变时纯属白跑。
    LOCATE_FRAME_CACHE = True
    # ---- 免点击的两道硬闸门 (用户 m09473 / m09594) ----
    # ① 逐类白名单 (`recognize/yolo_trust.py` + `config/yolo_trust/v<N>.json`): "名字可不可信"。
    #    离线按类别校准, 只有"过了该类 min_score 且该类实测 99%+ 正确"的名字才免点击。
    # ② 血条 ∪ YOLO 取并集 (`extract_info/roster.py::locate_roster`): "这里到底有没有东西"。
    #    白名单**管不住幻觉框** —— 实测 `extra_pred` 95/1079 = 8.8% 的预测框与任何真值框 IoU < 0.5。
    #    所以 YOLO 框必须有血条佐证才免点击; 血条有而 YOLO 没框的格**补进来照点**(否则就是假负样本)。
    YOLO_UNITS_TRUST_GATE = True      # 关掉 = 退回"所有 YOLO 名字都必须点击确认"
    YOLO_UNITS_UNION_BAR = True       # 关掉 = 只信 YOLO(不跑血条), 快 ~0.4s 但会漏 YOLO 没框的格
    YOLO_UNITS_TIMING = False         # 打印 预处理/推理/后处理 分步耗时
    # ---- prep 过场动画尾巴检测 + 重试 (用户 2026-10-18) ----
    # 战斗结束→prep 的过渡动画会遮挡棋盘, 这时 YOLO/血条都大面积认不出 (全 unknown)。
    # 判定 = prep 阶段 + (unknown 占比超阈值, 或与上一轮识别数量差距过大)
    #   -> 重新截图 + 重跑 YOLO+血条, 避免在过场动画里直接启动逐格点击 (白点 + 认错)。
    PREP_RETRY_YOLO_MAX = 1              # 开场重试次数 (用户要求默认 1; 0 = 关闭)
    PREP_RETRY_YOLO_SLEEP = 3.0          # 每次重试前等多久 (秒), 给过场动画时间结束
    PREP_RETRY_UNKNOWN_RATIO = 0.8       # unknown 占比超过它 → 判定过场 (0.8 = 8 成认不出)
    PREP_RETRY_COUNT_DIFF_RATIO = 0.5    # 与上一轮数量差距 > 50%...
    PREP_RETRY_COUNT_DIFF_MIN = 3        # ...且差距 > 3 个, 才判定"数量骤变"
    # ---- YOLO 漏检 / unknown 帧 → 训练样本 bucket (extract_info/unknown_frames.py) ----
    # 用户口径 (2026-10-17): "如果yolo有漏标, 或者有识别为unknown的类别, 那我们就把这一帧的截图A,
    # 放入我们训练样本中" —— 触发 = `missed>0` (血条有而 YOLO 没框) 或任一框 `model_name=="unknown"`;
    # 落盘到 workspace/train_yolo/collected_dataset/yolo_unknown_and_ignore/
    #   yolo_data/{images,labels,debug_images}  —— 落盘时的 **YOLO 预测** (标注含 ball, 与旧自动采集一致)
    #   real_data/{images,labels,debug_images}  —— **点击校验后**(拿到真名) 的真实帧, 与预测桶同 stamp 对拍
    # 同帧去重 + 每局上限; 复核入口两个 bat: review_pred_data.bat / review_real_data.bat (labelme)。
    YOLO_SAVE_UNKNOWN_FRAMES = True       # False = 关掉落盘 (只做定位, 不写训练样本)
    YOLO_UNKNOWN_FRAMES_PER_GAME = 100    # 每局最多落盘几帧 (新一局开始时归零; 0 = 不限量)
                                          # 15→100 (用户 2026-10-19: 多收 unknown 训练样本)

    # ---- 键鼠输入时序 (control/input.py): 后台 PostMessage 与前台真实输入共用 ----
    CLICK_MOVE_DELAY = 0.1    # 点击: 移动到目标后等待
    CLICK_DOWN_DELAY = 0.075    # 点击: 按下到抬起之间
    CLICK_UP_DELAY = 0.1       # 点击: 抬起后收尾
    SHOP_BUY_CLICK_GAP = 0.1   # buy 多槽连点的槽间间隔 (用户口径 2026-10-17, 动作提速):
                               # 原实现是**每次点击后**固定 sleep 0.25 —— 单槽买卡白等 0.25s,
                               # buy all 五个槽白等 1.25s。现在只有"后面还有槽"时才隔一下,
                               # 且收到 0.1s (与 refresh/levelup 的连点间隔同量级, 防被游戏合并)
    DRAG_STEPS = 14            # 拖拽: 移动小步数 (2026-10-28 由 20 收紧到 14; 2026-10-31 曾试 11 -> 实机
                               # `库存=1->1 拖拽未生效`, 立刻回滚。步数少=单步跨度大, 游戏会丢拖拽)
    DRAG_PRESS_PAUSE = 0.12    # 拖拽: 按住片刻再移动 (过短会被游戏当作点击丢弃; 2026-10-28 由 0.15 收紧;
                               # 2026-10-31 曾试 0.10 -> 同样导致 `库存 1->1`, 已回滚)
    DRAG_STEP_INTERVAL = 0.012  # 拖拽: 每小步间隔 (2026-10-28 由 0.015 收紧)
    DRAG_RELEASE_PAUSE = 0.05  # 拖拽: 落点停留再松开 (等游戏判定; 2026-10-16 由 0.1 收紧)
    # ---- 步数按**距离**自适应 (P0-26 定案, 用户 m00821 实机: 2026-11-02 02:18) ----
    #   定案原话:「就是位置距离问题, 和拖拽速度问题. 位置远, 而我们的 steps 是固定的, 导致拖拽速度过快」
    #   机理: 固定 DRAG_STEPS=14 时, 每步跨度 = 距离/14 —— 近格 690px 是 49px/步(从不失败, 0/37),
    #         远角 `己方(4,7)` 1390px 是 **99px/步** (真失败集中营, 35/47=74% 落在 >=1200px 档),
    #         每步间隔固定 DRAG_STEP_INTERVAL 不变 -> 等效指针速度 ~8000px/s -> 游戏不把这一串
    #         MOVE 当拖拽 -> 装备没离手 (内存签名「库存没少 + 目标格没变」= miss_start/B1 桶)。
    #   所以**固定的应该是"每步跨度上限", 不是步数**: steps = ceil(距离 / DRAG_MAX_STEP_PX)。
    #   取值依据: 78px/步 (己方(4,5), 1100px) 实测 0/9 不失败、99px/步 失败成堆 -> 上限取 60px/步留余量。
    #   代价: 远距拖拽多 (26-14)×0.012 = 0.14s, prep 窗口装得下 (一次 equip_to 现在也要 2~3s 校验)。
    DRAG_MAX_STEP_PX = 60.0    # 每步最大跨度(px), 0 = 关闭自适应、退回固定 DRAG_STEPS 的旧行为
    DRAG_STEPS_MAX = 26        # 自适应上限: 再远也不超过这个步数 (防单动作时长失控)
    # 拖拽首尾等待与点击分开: 点击要 0.1s 是防连点合并, 拖拽首尾只需要"事件落定"。
    # 两者原来共用一个常量, 于是每个 place/swap 白等 0.2s (2026-10-28 用户 m01492 提速)。
    # 2026-10-31: 首尾各收 0.02s (0.05->0.03) —— 这两段纯粹是"事件落定"等待, 不参与拖拽轨迹,
    # 实测未影响生效; 而 DRAG_STEPS / DRAG_PRESS_PAUSE 是这个游戏的硬约束, 一动就丢拖拽, 别再收。
    #   ^ 这句 2026-11-02 已修正口径 (P0-26 定案): "动 DRAG_STEPS 就丢拖拽"的**真变量是每步跨度**，
    #     不是步数本身 —— 10-31 那次把 14 收到 11，近距离(690px)就从 49px/步涨到 63px/步而开始丢；
    #     所以**不许再全局减步数**，远距要加步数（见上面 DRAG_MAX_STEP_PX 的 `drag_steps()` 自适应）。
    #     DRAG_PRESS_PAUSE 0.15->0.12->0.10 的往返是另一件事 (按住太短会被当点击)，与本条无关。
    DRAG_MOVE_DELAY = 0.03     # 拖拽: 移动到起点后等待 (原用 CLICK_MOVE_DELAY=0.1)
    DRAG_END_DELAY = 0.03      # 拖拽: 松开后收尾 (原用 CLICK_UP_DELAY=0.1)
    # ---- 通用渐入渐出剖面 (默认**关**; 只有 equip_to 通过 `timing="equip"` 那一档开启) ----
    # 用户 m01024 (2026-11-02 03:11) 原话:「我们现在是线性拖拽. 实际上应该渐入渐出, 尤其最后几个
    #   step, 挪动的px应该 20->16->10->6->3->2->1 ... 保证最后几个像素是[1~3px 挪过去]的效果」
    # 机理猜测: 游戏按指针**末段速度**判断"是不是要落在这"——99px/步 一路砸进目标格, 松手那一下
    #   还在高速移动, 判定就丢了 (与 P0-26 定案的"远距速度过快"同一件事的另一面)。
    # 剖面单位是**每步像素**, 不是比例; 头/尾合计 = 28 + 58 = 86px, 中段用 max_step_px 匀速铺满剩余距离。
    DRAG_EASE_IN_OUT = False       # 全局默认关: place/swap/sell/combine 行为一字不变 (用户 m01032 只要求 equip_to 应用)
    DRAG_EASE_HEAD_PX = (2.0, 4.0, 8.0, 14.0)              # 起步渐入 (合计 28px)
    DRAG_EASE_TAIL_PX = (20.0, 16.0, 10.0, 6.0, 3.0, 2.0, 1.0)  # 收尾渐出 (合计 58px, 最后一步 1px)
    DRAG_EASE_MIN_DIST_PX = 160.0  # 距离短于此不启用 (剖面都铺不满, 线性更划算)
    # ---- equip_to **单独一套**拖拽时序 (用户 m01032: 「equip_to 的 drag 速度, 单独设置一套」) ----
    #   为什么单独一套: 装备是"从栏里抓一件、拖到远处棋子上"的长距离精确落点, 和 place/swap
    #   (近距离、落点是整格) 不是一类问题。
    #   按住时长口径 (2026-11-02 用户 m00597 当场修正): 早先 m01022 记的「press 0.12/0.3 都还会丢,
    #   0.4 基本 100% 成功」**是误判** —— 用户原话:「丢的原因不在开头的按住, 那次是我失误了」。
    #   故 0.4 收回 0.10 (与全局同量级)，别再拿"0.4 才稳"当结论去加时; 真变量是每步跨度/末段速度 (P0-26)。
    EQUIP_DRAG_ENABLED = True          # False = equip_to 退回全局 DRAG_* 那一套 (一键对照)
    EQUIP_DRAG_PRESS_PAUSE = 0.10      # 按住 (全局 0.12; 2026-11-02 由 0.40 收回, 用户 m00597)
    EQUIP_DRAG_STEP_INTERVAL = 0.012   # 每步间隔
    EQUIP_DRAG_RELEASE_PAUSE = 0.10    # 落点停留 (全局 0.05; 用户 m00625: 「落点停留 也改为 0.1」)
    EQUIP_DRAG_STEPS = 14              # 步数下限 (与全局同)
    EQUIP_DRAG_MAX_STEP_PX = 60.0      # 中段匀速的每步跨度上限 (与全局同口径)
    EQUIP_DRAG_STEPS_MAX = 44          # 自适应上限 (渐入渐出本身要吃掉 11 步, 所以比全局 26 宽松)
    EQUIP_DRAG_EASE_IN_OUT = True      # **只有这一档默认开**渐入渐出
    EQUIP_DRAG_EASE_HEAD_PX = (2.0, 4.0, 8.0, 14.0)                   # 装备档自己的剖面 (可与全局分开调)
    EQUIP_DRAG_EASE_TAIL_PX = (20.0, 16.0, 10.0, 6.0, 3.0, 2.0, 1.0)  # 最后一步 1px
    EQUIP_DRAG_EASE_MIN_DIST_PX = 160.0  # 短于此不铺剖面 (装备栏→棋子最短也 ~540px, 这条只是兜底)
    # ---- equip_to 纯 no-op 重拖 (2026-10-31 用户 m01118: 给 r4c7 女警装备经常失败) ----
    # 长距离拖拽偶发丢事件: 库存该件没少 + 目标格也没多 = 拖拽根本没把装备拖出去(装备仍在栏里)。
    # 这种"纯 no-op"允许放宽时序重拖(装备还在栏里, 重拖安全, 不会一件装备给两次);
    # 而"库存少了但目标格没多"(已消耗/落错格)绝不重拖, 避免二次拖拽放大错误。
    EQUIP_REDRAG_MAX_ATTEMPTS = 2      # 纯 no-op 时最多重拖几次 (0 = 关闭重拖, 退回旧行为)
    EQUIP_REDRAG_STEPS = 18            # 重拖步数 (比 DRAG_STEPS=14 更多 -> 单步跨度更小, 更不易丢)
    EQUIP_REDRAG_PRESS_PAUSE = 0.18    # 重拖按住时长 (比 0.12 更长, 防被游戏当点击丢弃)
    EQUIP_REDRAG_RELEASE_PAUSE = 0.12  # 重拖落点停留 (比 0.05 更长, 等游戏判定)
    EQUIP_REDRAG_SETTLE = 0.4          # 重拖后等内存刷新再对账

    # ---- place/swap 棋盘拖拽"重拖放宽档" (2026-11-06, 第二轮实机 73 次 NG 中 68 次为拖拽零变化) ----
    # 实测: swap/place 成功率仅 65%, 失败时前后棋盘状态**完全不变**(拖拽整串没被游戏接收),
    # 不是落错格; 根因同 equip 重拖: 后台 PostMessage 注入的分步移动太快时游戏不识别为拖拽。
    # 首次拖拽原先走全局 DRAG_*; 2026-11-11 用户 m00224 起, 首拖也直接采用这套慢时序(见下)。
    # 现在首拖(timing="board")与"内存判未生效"后的**重拖**都用这组放宽时序(更长按住 + 更多步 + 更长落点停留)。
    BOARD_REDRAG_STEPS = 22            # 重拖步数 (比 DRAG_STEPS=14 多 -> 单步跨度更小, 更不易丢)
    BOARD_REDRAG_PRESS_PAUSE = 0.20    # 重拖按住时长 (比 0.12 更长, 防被游戏当点击丢弃)
    BOARD_REDRAG_STEP_INTERVAL = 0.018 # 重拖每步间隔 (比 0.012 略慢, 降低等效指针速度)
    BOARD_REDRAG_RELEASE_PAUSE = 0.14  # 重拖落点停留 (比 0.05 更长, 等游戏落点判定)
    # ---- place/swap **首拖**直接采用上面的重拖慢时序 (用户 m00224, 2026-11-11) ----
    # 用户实机观察: 首拖飞快 → 频繁失败 → 再自动重拖反而更耗时(单次 5~6s)。诉求原话:
    #   「就按重拖的那个慢一点的速度就很稳了…一旦拖动失败反而要花更多时间」。
    # 实现: operations.units.swap 首拖(drag_kw=None)传 timing="board", control.input._drag_timing
    #   的 "board" 档读上面的 BOARD_REDRAG_* 四个值(步数/按住/间隔/落点)作为首拖时序。
    # 回退开关: True = 首拖退回旧的全局 DRAG_*(快时序), 便于实机 A/B 对照; False = 用重拖慢时序(默认)。
    BOARD_FIRST_DRAG_FAST = False
    KEY_TAP_DURATION = 0.05    # tap_key 按下到释放间隔
    # 拖拽前鼠标体检: 光标**压在模拟器窗口矩形内**时, 要不要把它搬走 (用户 m00001, 2026-10-31)
    #   背景: 模拟器常被压在**最下层**, 用户在上层浏览器里操作; 老判据只看"矩形包含",
    #         于是用户鼠标一进模拟器那块矩形就被搬出去, 直接打断他正在做的事。
    #   False = 只有光标**真的压在模拟器窗口可见区域上** (该点最顶层窗口属于本实例) 才搬;
    #           矩形内但被别的窗口盖住时不碰用户光标 —— 这是当前默认 (用户诉求: 别打扰)
    #   True  = 退回老行为: 只要几何上落在矩形内就搬 (用于实机验收时对照/落点保底)
    DRAG_PARK_WHEN_OCCLUDED = False

    # ---- 后台鼠标指示图标 (overlay/mouse_icon.py): 后台点击/拖拽时在屏幕对应位置闪现提示图标 ----
    # 背景: 后台输入 PostMessage 不动真实光标, 屏幕上看不到机器人点了哪里, 用图标辅助观察
    MOUSE_ICON_SHOW = True      # False 关闭; 运行时改 default_config.MOUSE_ICON_SHOW 即时生效
    MOUSE_ICON_SIZE = (200, 300)   # 图标显示画布 (宽, 高) px: 素材等比缩放后**底部居中**放进去
    #                              (改后需重启生效; 底部对齐=人物脚底贴齐, 手指指着点击点的相对位置不漂)
    MOUSE_ICON_OFFSET = (-30, 10)  # 图标左下角对齐点击中心后, 再向右上偏移 (x, y) px (运行时改即时生效)
    MOUSE_ICON_CLICK_SECONDS = 0.75  # 点击图标停留秒数 (运行时改即时生效)
    MOUSE_ICON_DRAG_SECONDS = 0.75   # 拖拽落点停留秒数; 拖拽途中每次移动重置计时→持续显示整个轨迹
    MOUSE_ICON_DRAG_CLICK_DELAY = 0.25  # 拖拽起点: 先显示点击图标, 该秒数后切换为拖拽图标 (0=直接拖拽图标, 即时生效)
    # 多开时"一个模拟器窗口一个卡通人物" (用户 m00064): 按**雷电实例 ID** (ld_index) 对套数取模,
    # 于是 0 号窗口显示第 1 套、1 号窗口显示第 2 套、2 号又回到第 1 套…… 每套 = (点击图标, 拖拽图标)。
    # 模板库命名规范 (用户 m00303): images/icons/click_mouse_<i>.png + drag_mouse_<i>.png 成套,
    # 第 <i> 套给 window_<i> 显示; 加人物 = 往 images/icons/ 放一对同号文件, 不用改代码。
    # 外部素材图 (棋盘格底/纯色底) 用 `python -m scripts.import_mouse_icons --index <i> --click … --drag …`
    # 抠成透明 PNG; 临时画人物用 `python -m scripts.make_mouse_char_icon --index <i> --style b`。
    MOUSE_ICON_DIR = PROJECT_ROOT / "images" / "icons"
    MOUSE_ICON_GLOB = "click_mouse*.png"   # 发现规则: 同号的 drag_mouse_<i>.png 必须存在才成套
    MOUSE_ICON_SETS = None   # None = 按上面规则扫目录自动发现; 也可显式写成 [(点击图, 拖拽图), ...] 覆盖
    # 点击位置被别的窗口盖住时是否还画图标 (用户 m00064 第 2 条): True = 被遮住就不显示。
    # 判定只认"目标模拟器自己的窗口树" (该点最顶层窗口的顶层祖先 == 该实例主窗口);
    # 指示图标自身是 WS_EX_TRANSPARENT 鼠标穿透窗口, WindowFromPoint 会跳过它, 不会自己遮自己。
    # 注意: 这一条只在 MOUSE_ICON_ONLY_FOREGROUND=True 时才生效 (总闸关掉就什么都不判)。
    MOUSE_ICON_SKIP_OCCLUDED = True
    # 卡通小人是否**只能在前台展示** (用户 m00569; 当前按要求 m00572 设为 False):
    #   True  = 仅当目标模拟器窗口是当前活动窗口 (GetForegroundWindow) 时才画; 此时再按
    #           MOUSE_ICON_SKIP_OCCLUDED 判"点击位置有没有被别的窗口压住"
    #   False = 前台和遮挡都不判, 只要 MOUSE_ICON_SHOW 开着就画 —— 多开时你盯着别的窗口、
    #           甚至把模拟器切到后台, 也照样能看见小人在点哪里
    MOUSE_ICON_ONLY_FOREGROUND = False

    # ---- 实时调试绘制层 (overlay/debug_overlay.py + overlay/live_draw.py, 用户 m00172) ----
    # 在雷电模拟器画面上**实时叠加**绘制识别结果 (纯外部 Win32 分层透明窗口, 不注入不 hook,
    # 贴 RenderWindow 客户区, 自动 hide)。跟 runner 一起启动: 开启后按 OVERLAY_DRAW_INTERVAL
    # 秒起一条独立守护线程自动「截图 -> 识别 -> 绘制」, 与对局主流程完全解耦; 主循环决策/动作时
    # 该线程礼让跳过 (见 overlay/live_draw.py::main_work), 不抢 OCR/YOLO。
    # 2026-11-06 用户 m01602「开启下 yolo 绘制功能, config中开关」-> 总开关置 True。
    # 想临时关掉不用改这里: `python -m runner --no-overlay` (命令行覆盖, 见 runner.py:8877)。
    # **2026-11-08 用户要求先整体关闭**: 「把我们的 yolo 和商店显示等绘制功能先关闭了 — 这个
    # 绘制窗口太占用 ocr 了」。实机 2-6 combat 复现: 绘制线程持续打 OCR (棋子 0.5s/商店 2s/
    # 装备栏 1s), 把 OCR 通道长期占住 (`在跑=是`), 导致 combat 主动大搜(可选请求)连续 8 轮
    # 让路、整段 40s 没买到一张牌, 直到最后 7s 通道空下来才买到。故总开关置 False:
    # 绘制线程不启动 -> YOLO/商店/装备栏/内存GT 一律不再识别+绘制, 零 OCR 开销。
    # 要恢复: 把这里改回 True (单项仍由下面 OVERLAY_SHOW_* 控制, 值保持原样未动)。
    OVERLAY_DRAW_ENABLED = True         # 总开关: True = 随 runner 启动实时绘制线程
    # ★ 2026-11-14 调试阶段（用户 m04000）：「调试阶段一律给我打开 draft 的那个，别全给我打开」
    #   → 总开关开、**只有选秀(draft)一项为 True**，其余全关。这样既能看到选秀轮盘的
    #   轨道/装备/我方/规划点击点，又不会因为 YOLO/商店/装备栏那几个贵项把画面糊满、把帧率拖死。
    #   调试结束把 OVERLAY_DRAW_ENABLED 改回 False 即可恢复原状（下面的值不用动）。
    OVERLAY_DRAW_INTERVAL = 2.0         # 兜底间隔 (秒, float): 分项间隔没配时的默认值
    OVERLAY_LD_INDEX = None             # 目标雷电实例 ID; None = 自动取第一个可用实例
    # 各项识别结果的绘制开关 (用户 m00172: "目前, 先显示 棋子、商店、装备栏、棋子携带装备 这几个")
    OVERLAY_SHOW_UNITS = False          # Yolo 棋子识别结果 (蓝框) —— 调试阶段只留 draft
    OVERLAY_SHOW_SHOP = False           # 商店识别结果 (橙框)
    OVERLAY_SHOW_EQUIP_BAR = False      # 装备栏识别结果 (青绿框)
    OVERLAY_SHOW_BOARD_EQUIPS = False   # 棋子携带装备识别结果 (品红框)
    OVERLAY_SHOW_BLOOD_BAR = False      # 血条识别结果 (深灰框) —— 默认关: board.extract_units 较慢
    OVERLAY_SHOW_BALLS_YOLO = False     # balls-yolo: 用 YOLO 检索场上球 (浅蓝框) —— 用户 m00656, 默认开
    OVERLAY_SHOW_BALLS_COLOR = False    # balls-color: 用 HSV 颜色法找奖励球 (浅蓝框) —— 用户 m00656, 默认关
    OVERLAY_SHOW_STAR_BADGE = True      # 血条左侧星级徽记框 + 大号 ★×N (用户 m01029: 星级要看得出对错)
    OVERLAY_SHOW_HERO = True            # 小小英雄: 等级数字块框 + "我方/敌方 · Lv6" 标签 (用户 m01362)
    # 注: 下面的 OVERLAY_SHOW_MEM_GT / OVERLAY_SHOW_DRAFT / OVERLAY_MEM_GT_INTERVAL /
    # OVERLAY_DRAFT_INTERVAL / OVERLAY_GT_* 是父项目"内存 GT + 选秀轮盘"遗留键, 纯图色工程
    # 已删掉那两条链路, 这里保留只为不影响既有调用方 (tools/config_audit.py 会列出未用键)。
    OVERLAY_SHOW_MEM_GT = False         # (遗留) 内存 GT 格位框
    # ---- 分项降频 (用户 m00532) ---- 各项识别耗时差异极大 (实测 YOLO 棋子 66ms / 装备栏 475ms /
    # 商店 1003ms), 全项每拍都跑会到 2.2~2.8s 一帧。故各项各有独立刷新间隔 (秒, float):
    # 线程节拍自动取**启用项里的最小值**(下限 0.2s), 每拍只跑到期项, 未到期项复用上次结果重画,
    # 所以真正落地的刷新频率由这里决定, 不再是"一刀切"的 OVERLAY_DRAW_INTERVAL。
    # 注意: 棋子携带装备要用棋子的 units 结果 —— 两者都开时, 携带装备刷新那一拍会自动补一次
    # 棋子识别 (除非缓存里已有), 不需要把 OVERLAY_UNITS_INTERVAL 调成一样。
    OVERLAY_UNITS_INTERVAL = 0.5        # 棋子 (YOLO) 刷新间隔 —— 最便宜, 刷最快
    OVERLAY_EQUIP_BAR_INTERVAL = 1.0    # 装备栏刷新间隔
    OVERLAY_SHOP_INTERVAL = 2.0         # 商店刷新间隔 —— 最贵, 刷最慢
    OVERLAY_BOARD_EQUIPS_INTERVAL = 2.0 # 棋子携带装备刷新间隔
    OVERLAY_BLOOD_BAR_INTERVAL = 2.0    # 血条刷新间隔 (默认关)
    OVERLAY_BALLS_YOLO_INTERVAL = 0.5   # balls-yolo 刷新间隔 (用户 m00656: 0.5s 一次; 跟随棋子识别结果)
    OVERLAY_BALLS_COLOR_INTERVAL = 0.5  # balls-color 刷新间隔 (用户 m00656: 0.5s 一次)
    OVERLAY_HERO_INTERVAL = 0.5         # 小小英雄刷新间隔 (等级数字块 + 红/绿小血段, 实测 8~18ms)
    OVERLAY_MEM_GT_INTERVAL = 0.5       # 内存 GT 刷新间隔 (读的是主循环 TTL 缓存里的 state, 不额外读内存)
    OVERLAY_SHOW_DRAFT = True           # 选秀轮盘实时绘制 (椭圆轨道/装备图标虚线圆/棋子实心圆/小小英雄) —— 用户 m01507
    OVERLAY_DRAFT_INTERVAL = 0.5        # 选秀轮盘刷新间隔 (秒)
    # 内存 GT 格位框半边长 (像素): 内存只给格位(own_row/column 或 bench slot), 由棋盘几何换成格心后
    # 画一个以格心为中心的方框。棋盘格距 91~120px、备席格距 112px, 取 42 能框住差不多一个棋子。
    OVERLAY_GT_BOX_HALF = 42
    OVERLAY_GT_BENCH_BOX_HALF = 50      # 备席格心间距 112, 用大一点的框把备席棋子框住
    # 实时格位框比 GT 框再大这么多像素 (用户 m01278 第 3 点): combat 期棋子会走动,
    # 布阵格(GT 黄框)与此刻真正站的格(青框)画在同一屏对照 —— 青框画在外圈, 两个框
    # 同心时也分得出"这是两套位置且此刻没位移", 不会被黄框整块盖住。
    OVERLAY_RT_BOX_PAD = 8
    # 棋子本体框口径 (用户 m01602, 2026-11-06 像素实测): 内存的「格心」是棋子**脚底**
    # 落点, 拿它当中心画对称正方形会整块偏低约 50px (框罩在脚下空地上, 和 YOLO 的棋子
    # 框永远错位)。棋子本体框 = 上边 格心+TOP_DY、下边 格心+BOTTOM_DY、半宽 HALF_W,
    # 与 `train_yolo/collect_data_agent/mem_truth.py` 的 BOX_* / BENCH_* 同值。
    OVERLAY_UNIT_BOX_HALF_W = 39
    OVERLAY_UNIT_BOX_TOP_DY = -105
    OVERLAY_UNIT_BOX_BOTTOM_DY = 5
    OVERLAY_UNIT_BENCH_TOP_DY = -69
    OVERLAY_UNIT_BENCH_BOTTOM_DY = 5
    # 自动 hide (用户 m00172 第 1 条): 目标窗口最小化 / 不可见 / 被别的窗口**整体盖住**时自动收起,
    # 恢复可见后自动重新贴回。窗口线程内定时器每 OVERLAY_WATCH_INTERVAL 秒巡检一次。
    OVERLAY_WATCH_INTERVAL = 0.5        # 可见性/遮挡巡检间隔 (秒)
    OVERLAY_HIDE_WHEN_OCCLUDED = True   # True = 整体被别的窗口盖住就收起; False = 只要可见就画

    # ---- 选秀(draft)轮盘识别 (recognize/draft_ring.py, 用户 m01507) ----
    # 口径与 verify/carousel_draft 模拟器一致: 装备图标(虚线圆) 在棋子上方, 棋子本体(实心圆)在
    # 图标正下方 DRAFT_RING_ITEM_DY 像素处; 轨道 = 用棋子中心点拟合的椭圆(少于 5 点退化成圆)。
    # 只有检出够多图标 (>= DRAFT_RING_MIN_UNITS) 才认成选秀帧 —— 非选秀帧一律不画, 不浪费噪点。
    DRAFT_RING_ROI = (500, 130, 1430, 770)  # 轮盘活动区 (1920x1080 基准)
    DRAFT_RING_IMGS = 0.6       # 匹配前整窗缩放比 (0.6 倍速度/精度平衡)
    DRAFT_RING_SIDES = (40, 52)  # 要试的图标边长 (px, 原图基准), 多档覆盖远近两种棋子
    DRAFT_RING_THR = 0.62       # 模板匹配阈值 (TM_CCOEFF_NORMED)
    DRAFT_RING_THR_SURE = 0.78  # 高置信线: 只用来上色区分, 不影响检出
    DRAFT_RING_NMS = 28         # 同一件装备的去重半径 (px)
    DRAFT_RING_TOPK = 2         # 每张模板每个尺度取几个峰
    # 粗检并行度(用户 m01590「例如并行」)：cv2.matchTemplate 在 C++ 里跑并释放 GIL,
    # 线程池能真并行。1 = 串行(老行为), 4 = 实测粗检 103ms -> 约 40ms。
    DRAFT_RING_MATCH_THREADS = 4
    DRAFT_RING_ITEM_DY = 94     # 装备图标中心 -> 棋子本体中心的垂直偏移 (px)
    DRAFT_RING_MIN_UNITS = 3    # 至少检出几件图标才当"这是选秀帧"
    DRAFT_RING_FIT_MIN_SCORE = 0.68  # 参与椭圆拟合的最低匹配分
    DRAFT_RING_FIT_TOL = 40.0        # 拟合后剔离群点的容差 (px)
    DRAFT_RING_MIN_R = 110.0         # 轨道半径合理下限 / 上限 (px), 超出视为拟合失败
    DRAFT_RING_MAX_R = 380.0
    DRAFT_RING_HERO_W = 66           # 小小英雄本体框宽/高 (px)
    DRAFT_RING_HERO_H = 96

    # ---- 小小英雄定位: 等级数字块 + 右侧血条颜色 (recognize/draft_hero.py) ----
    # 用户 m02435 的思路: 「只要匹配文字图标的右边一点点, 判断它是红色还是绿色, 就能分辨出来
    # 是否为敌我了?」 实测 5 帧全对、10~16ms (旧 _find_bars 那套 150~200ms 且战斗帧 3 检出
    # 里 2 个假阳性)。坐标口径 1920x1080。
    DRAFT_HERO_USE_LEVEL_MATCH = True    # 有等级模板时用它区分「等级数字」和「血量数字」
    DRAFT_HERO_LEVEL_DX = 60.0           # 等级数字块中心 -> 英雄落点 的水平偏移 (px)
                                         # 标定: 我方等级块 (894,821,12x17) 中心 (900,829.5)
                                         #      -> 标注 hero 中心 (960,884) => dx=+60.0 dy=+54.5
    DRAFT_HERO_LEVEL_DY = 54.5
    DRAFT_HERO_LEVEL_TOL = 40.0          # 同英雄去重半径 (px): 等级数字与血量数字相距 ~50px
    DRAFT_HERO_SEG_MAX_X = 1600          # 只认 x 小于它的候选 (排除右侧玩家排行榜 UI)
    DRAFT_RING_BAND = 55.0           # 画轨道时内/外两条淡线相对中线椭圆的偏移 (px), 口径同模拟器
    DRAFT_RING_MAX_GAP_DEG = 170.0   # 棋子沿轨道的最大空档 (度): 超过就判假选秀
                                     # (普通对局地上零散装备会凑出轨道, 但挤在一小段上; 实测假 215°/222°, 真 63~158°)
    DRAFT_RING_STRONG_ITEMS = 8      # "轮盘特征极强"的件数门槛 (2026-11-07 20:52 实机教训):
                                     # 选秀开场 1~3 秒 OCR 阶段还读作 combat, 硬否决会把整个 2-4 错过。
                                     # 轨道内件数 >= 它 且 gap 已过 170° 闸 → 即使阶段不是 draft 也认作选秀。
                                     # 实测假轨道(普通战斗画面凑的)只有 4~6 件, 真轮盘 9~11 件。

    # ---- 选秀(draft)自动拾取 (control/carousel_walker.py, 用户 m00146) ----
    # 用户口径: 这里一个总开关; 关掉 = 完全退回原来的处理方式 (runner 在 draft 阶段只读等待);
    # 打开 = 只在**选秀回合** (stage = n-4 且 n >= DRAFT_AUTO_PICK_STAGE_MIN) 自动操控小小英雄
    #        去拾取散件。1-4 是野怪回合, 不在此列 (判据同 recognize/phase.py:251)。
    DRAFT_AUTO_PICK = True           # 总开关（用户 m00921：n-4 选秀回合默认不挂机，直接接管）
    DRAFT_AUTO_PICK_STAGE_MIN = 2    # x-4 回合里, 大阶段 x >= 这个值才算"选装备" (= stage > 1)
    # 阶段上限已取消（用户 m00921，2026-11-11 02:08）：选秀判据统一为
    # 「stage > 1 且 stage 是 n-4」——任何 n>=2 的 n-4 都算选秀，不再写死 6。
    # 5-4 成装 / 6-4 混合 / 7-4 以后由 CarouselWalker 按 stage_major 切 items_mode
    # （>=6 mixed, >=5 full），见 control/carousel_walker.py::resolve_items_mode。
    DRAFT_AUTO_PICK_COUNT = 3        # 本回合最多拾几件 (拿够就走)
    DRAFT_AUTO_PICK_MAX_SECONDS = 32.0   # 单回合最多耗时 (秒), 超时收手并交还主循环
                                         # 32s 而非 20s (2026-11-14): 3 件散件按几何约束
                                         # (hero 85px/s、一圈~14s) 要 15~25s, 20s 会超时只捡 2 件;
                                         # 选秀阶段结束 is_draft=False 会自然收手, 调大不会白等。
    DRAFT_AUTO_PICK_MAX_LEGS = 12    # 单回合最多走几条腿 (一次点击序列算一条)
                                     # 12 而非 8 (2026-11-14): 每件可能"先贴边再穿越"=2 腿,
                                     # 3 件 + 护栏 ≈ 7~8 腿, 8 太紧会提前收手少捡。
    DRAFT_AUTO_PICK_OMEGA_DEG = 18.0     # 轨道角速度 (度/秒, 正 = 极角增大 = 顺时针); 有实测就覆盖
    DRAFT_AUTO_PICK_HERO_V = 85.0    # 小小英雄移动速度 (px/s, 1920x1080 基准); 走一条腿后实测慢修
    DRAFT_AUTO_PICK_HERO_DY = 0.0    # 我方英雄落点 = 本体框底边中点 bc 再加这个垂直偏移 (px)
    # 开局出生点先验: 选秀开始时我方小小英雄恒在画面底部中央 (实机 2-4 / 3-4 两局实测都是
    # ≈(947,903))。我方绿血条被 detect_heroes 漏检时, 就靠这个先验 + 航位推算定位
    # (实机 3-4 那局 detect_heroes 只找到 5 个 hero, 我方整条没进列表, 旧的"最靠下"
    #  兜底抓到了别人, 前两腿全废)。
    DRAFT_AUTO_PICK_SPAWN_X = 947.0
    DRAFT_AUTO_PICK_SPAWN_Y = 903.0
    # ---- 护栏（开局锁时）判定：用户 2026-11-13 m01936 / m02047 亲自 labelme 标注 ----
    # 标注源 tempdir/labelme_hero/draft_2_4_enter.json（1920x1080）：
    #   my_hero 中心 (960,884)；guard 框 (839,767,1080,1009) -> 半宽 120.5 / 半高 121。
    # 规则：hero 走出这个范围 = 护栏关闭；到了 waypoint = 离开护栏成功，可以开始拾取。
    DRAFT_AUTO_PICK_HERO_GUARD_CX = 960.0
    DRAFT_AUTO_PICK_HERO_GUARD_CY = 884.0
    DRAFT_AUTO_PICK_HERO_GUARD_R = 120.0
    # 出护栏后先走到的第一个落脚点（用户标注，本来就不在轨道上）：
    # 「只要我们的 hero 出了护栏, 到了这个点, 就可以判定离开护栏成功」(m02047)
    DRAFT_AUTO_PICK_WAYPOINT_X = 1138.0
    DRAFT_AUTO_PICK_WAYPOINT_Y = 685.0
    DRAFT_AUTO_PICK_WAYPOINT_TOL = 25.0
    # 轨道**写死**（用户 2026-11-08 11:31 明确要求）：
    #   「这个游戏的轨迹应该每一局都不会变动，只要我们不动这个摄像机的位置和这个高度，
    #     它应该是不会变才对 … 我们要不要就把这个轨迹定成一个写死的轨迹？」
    # 依据：4 次实机锁定的椭圆 c/a/b 分别
    #   (972,453,222,170) (969,465,215,162) (956,455,231,171) (962,452,213,158)
    #   → 均值 ≈ (965, 456, 220, 165)，离散度 a±9 / b±7 px，对角度/间隙判定影响很小。
    # 效果：**跳过"等刷完 / 等椭圆稳定"**，第一帧有效选秀帧就用它算角度，起手快好几秒。
    #   None = 回退到"自动拟合"老行为。
    # 安全阀：`DRAFT_AUTO_PICK_RING_FIXED_TOL` —— 实测点对它的残差中位数超过阈值就打警告
    #   （不自动回退，免得在两种轨道间来回跳；日志里能看到，人工判断）。
    DRAFT_AUTO_PICK_RING_FIXED = (965.0, 456.0, 220.0, 165.0)
    DRAFT_AUTO_PICK_RING_FIXED_TOL = 60.0
    # 轨道自校验阈值（用户 2026-11-13）：「我的这个轨道能不能够支持这个自动识别？就是如果偏差
    # 实在太大，可能游戏更新之后偏差太大，能不能自动识别？」—— 拿实测棋子位置量"到锁定椭圆的
    # 距离"取中位数，超过这个值就认为**这一局/这一版轨道对不上**，用当前帧的点自动重拟合一次。
    # 30px 的来历：识别本身定位抖动 ±5px，椭圆拟合误差 ~10px，留一倍余量；旧值 60 太松
    # （11-10 那局实机偏 77px 才报警，那时已经 0 件了）。
    DRAFT_AUTO_PICK_RING_FIT_TOL_PX = 30.0
    # 面板持久化(utils/draft_params) 用的开关/模板(用户 m02012)：可覆盖实机默认。
    DRAFT_AUTO_PICK_USE_DEFAULT_TRACK = False    # 默认: 用首帧实机拟合的椭圆(每局长短轴都不同, 写死模板会差 30~50px 导致全盘误判离场)
    DRAFT_AUTO_PICK_RING_TEMPLATE = ""           # 默认轨道模板名(u13/u9...); 空=用上面 RING_FIXED 常量
    DRAFT_CAROUSEL_OMEGA_FAR_DEG = None          # 近快远慢 a(远) 角速度(°/s); None=自动拟合
    DRAFT_CAROUSEL_OMEGA_NEAR_DEG = None         # 近快远慢 b(近) 角速度(°/s); None=自动拟合
    DRAFT_AUTO_PICK_STATIC_SCENE = False         # 静态场景(录像暂停): 不提前/不移动/不显示已离场
    DRAFT_AUTO_PICK_RING_TEMPLATE_AUTO = False   # 默认轨道按件数自动切换模板(u13/u9...)
    DRAFT_AUTO_PICK_RING_COUNT_WINDOW_SEC = 5.0  # 数量累计窗口(秒): 窗口内取"检测到的最大件数"选 u{n} 模板
    DRAFT_AUTO_PICK_COUNT_FROM_MEMORY = True     # 件数优先用内存(RoundSelect); 关/读不到则用图色识别的件数
    # 选秀抢装顺序 —— **由大模型给, 不是这里算的** (用户 m01436:
    # 「你的最优目标应该是大模型给的, 不是你计算的!!!」)。
    # 口径来源: trae_agent/prompts/主宰女警prompts_v2/equipment_agent_prompt.md
    #   1.9 版本女警(主宰)核心装 = 鬼索的狂暴之刃(俗称"羊刀") = **反曲之弓 + 无用大棒**。
    # 用户 2026-11-07 21:00 当场纠正: **只要反曲之弓 + 无用大棒，不要暴风大剑**
    #   ("我要那个反曲之弓, 我要那个无用大棒 … 我不要暴风大剑")。
    #   —— 之前写成 (反曲之弓, 无用大棒, 暴风大剑) 是照抄了 prompt 里那句自相矛盾的
    #   「反曲之弓 > 暴风大剑 (羊刀必做)」，与同文件成装表「羊刀 = 反曲之弓 + 无用大棒」冲突。
    # 顺序有意义: 前面的优先，程序只在表内挑、挑不到就原地等，绝不降级。
    # 留空 () = 回退到 recognize/draft_priority 的公式推算(按成装档位反推散件分)。
    DRAFT_AUTO_PICK_WANTED = ("反曲之弓", "无用大棒")
    # 5-4 成装选秀的抢装顺序（用户 m00368：5 阶段只有成装）。
    # 留空 () = 用 recognize/draft_priority 从 wanted_equipments.json 的 tier 派生
    #   （必备 → 功能 → 优先 → 一般，同档保持 JSON 顺序）。
    # 6-4 混合回合把 WANTED_FULL 与散件 WANTED 合起来用（同档成装优先，见 mixed_priority）。
    DRAFT_AUTO_PICK_WANTED_FULL: tuple = ()
    # 抢装清单**来源开关**（用户 2026-11-13 m01612）：
    #   False(默认) = 用上面两项本地清单 —— 散件 `DRAFT_AUTO_PICK_WANTED`（反曲之弓 > 无用大棒），
    #                 成装 `DRAFT_AUTO_PICK_WANTED_FULL` 空 → 由 draft_priority 从
    #                 wanted_equipments.json 的 tier 派生。**临时测试选秀用这个**，
    #                 不依赖「装备角色/大模型」有没有给出 wanted。
    #   True        = 用 Runner 里「装备角色」给出的 `self.wanted_equips`（正式跑局用，
    #                 见 trae_agent/runner.py::_draft_auto_pick_step）。清单为空时自动退回默认。
    DRAFT_AUTO_PICK_WANTED_FROM_LLM = False
    # 一条腿允许的最小安全间隙(px)。`carousel_plan.Params.plan_margin` 是 6px 的几何下限，
    # 但实机里"擦到谁就等于捡了谁"（用户 m01513：反曲之弓在场，却因为路径擦到拳套而捡了拳套，
    # 选秀只能拿一件 → 这一轮直接报废）。所以实车要求更高的余量, 宁可这一轮不拿也不许捡错。
    DRAFT_AUTO_PICK_MIN_CLEAR = 26.0
    # 一条腿"会合时刻"的上限(秒)。实机 4-4 那轮用户当场指出「你动的太慢, 而且反曲之弓已经被
    # 其它hero 拾取了」：一条 stage=A 等窗口的腿 t_pick=13.85s，等人先到就先被抢光。
    # 选秀只有一次机会, **先动手** 比"挑最漂亮的路线"重要，所以超过这个时长的腿只当兜底。
    DRAFT_AUTO_PICK_MAX_LEG_SECONDS = 6.5
    # 连等几次"最优先那件只有慢腿"才硬走它(用户 m01734 口径: 绝不因此换目标)。
    DRAFT_AUTO_PICK_SLOW_WAIT_TRIES = 6
    # 慢不起来时的放宽间隙(px)：宁可蹭得近一点也要赶在别人之前拿到(优先级最高的那件)。
    DRAFT_AUTO_PICK_MIN_CLEAR_FAST = 12.0
    # 移动预测判定"这件已经被拾取" (用户 m01665): 轨道锁定后每件装备的位置是可预测的。
    #   ① 连续 MOTION_STRIKES 次检查"位置几乎没动"、而按轨道它本该移动一段 → 判离场;
    #   ② 实测位置偏离轨道预测超过 MOTION_DEV_PX → 判离场。
    # 判离场 = 已被人拿走, 从目标候选里剔掉, 也可作为"我们真的拾到了"的确认依据。
    DRAFT_AUTO_PICK_MOTION_STILL_PX = 6.0     # 两帧实测位移小于它 = "没动"
    DRAFT_AUTO_PICK_MOTION_DEV_PX = 90.0      # 实测位置偏离轨道预测超过它 = 不符合轨迹
# 为什么是 90 而不是 45（用户 2026-11-13 19:40 打回「一堆 xxx 已离场」）：
# 匹配容差 MATCH_TOL_RAD = 18°（carousel_walker.py L66），在半径 ~200px 的椭圆上
# 对应弧长 ≈ 63px。也就是说「角度差 17° 仍算匹配成功」是正常情况，像素偏差本来就可以到 60px。
# 判离场阈值却只有 45px < 63px → **合法匹配也会被判"偏离轨道"**，2 次就判死 → 满屏红点。
# 现在 90px = 匹配容差弧长的 1.4 倍；配合 _motion_check 的 strict 逻辑（件数没减少时再 ×3）。
    DRAFT_AUTO_PICK_MOTION_STRIKES = 2        # 连续几次成立才判离场

    # ---- 判离场参数（用户 2026-11-13 19:50 要求：秒制 + 距离/角度双判据 + 面板可调可存默认）----
    # 用户原话：「超出我们轨道很多的范围，我们才判他离场…第二个就是他长时间不动，我们也判他离场…
    #   我觉得你这个值是不是设置的太小了？你把它也设置成一个参数…偏移的距离和角度…放到控制设台上面，
    #   我给他自己手动调一下，然后我可以保存为那个默认设置」
    # 判据（任一成立且持续够久 → 判离场）：
    #   ① 实测位置偏离轨道预测 > GONE_DEV_PX **或** 偏离角度 > GONE_DEV_DEG，持续 GONE_HOLD_SEC 秒；
    #   ② 实测位置连续 GONE_STILL_SEC 秒几乎没动，而按轨道它本该移动一段。
    DRAFT_AUTO_PICK_GONE_DEV_PX = 120.0       # 偏离轨道预测超过这么多像素 → 算"飞出轨道"
    DRAFT_AUTO_PICK_GONE_DEV_DEG = 30.0       # 或偏离角度超过这么多度 → 同样计入（距离/角度取"或"）
    DRAFT_AUTO_PICK_GONE_HOLD_SEC = 1.5       # 偏离要连续持续这么久才判死（防抖，别一帧就杀）
    DRAFT_AUTO_PICK_GONE_STILL_SEC = 3.0      # 连续这么久"该动没动" → 判死
    # 画面整体静止（录像暂停 / 模拟器卡帧）：本帧所有检出位置和上一帧比都 < 这个像素 → 一律不判离场。
    # 用户 m01160：「现在我就简单的静止了录像, 也一直显示 xxx 已经离场???」就是这个场景。
    DRAFT_AUTO_PICK_STILL_SCENE_PX = 2.0
    # 等刷完 (用户 m00630): 刚进选秀时装备/棋子还在生成, 第一帧件数偏少且位置在漂;
    # 持续识别, 直到「装备数量连续 SETTLE_SECONDS 秒不再增加」才算刷完, 才允许规划点击。
    DRAFT_AUTO_PICK_SETTLE_SECONDS = 3.0      # 件数多久不增加 = 刷完了
    DRAFT_AUTO_PICK_SETTLE_MAX_SECONDS = 14.0 # 等刷完最多等这么久, 超时就用当前帧硬上
    # 护栏探测 (用户 m00634): 选秀开局我方被"护栏"锁住, 点哪都不动;
    # 靠"点一个离出生点足够远的等待点 + 观察我方是否真的移动"来判断锁是否已解除。
    DRAFT_AUTO_PICK_UNLOCK_MIN_DIST = 130.0   # 等待点离出生点的最小距离 (px), 太近位移量测不出来
    DRAFT_AUTO_PICK_UNLOCK_MOVE = 28.0        # 我方位移超过这么多 px = 护栏已解除
    DRAFT_AUTO_PICK_UNLOCK_TIMEOUT = 3.0      # 单次确认最多等这么久 (秒), 超时补点一次再等；6→3 省选秀窗口
    # 用户 2026-11-14：实机「点击太稀(约5s一次)导致走不出护栏」，且一次 tap 只推英雄
    # ~75~120px 就停 —— 必须按 1s 节奏连续补点/重拍，而不是点一次等 timeout。
    DRAFT_AUTO_PICK_UNLOCK_CLICK_INTERVAL = 1.0   # 护栏阶段补点间隔(秒)
    DRAFT_AUTO_PICK_UNLOCK_TOTAL = 8.0            # 护栏阶段总预算(秒)
    DRAFT_AUTO_PICK_RETAP_INTERVAL = 1.0          # 走位/等待期间对当前目标点的重拍间隔(秒)
    # 点击通道 (用户 m00604 实测"完全没动弹"后定的): click_left 默认走 _bg_click = win32
    # PostMessage, 游戏(Unity)基本不认; adb 才是真正能驱动游戏、且不抢前台键鼠的那条。
    #   "adb"   = control.input.adb_tap (ldconsole adb shell input tap, 后台注入, 默认)
    #   "click" = control.input.click_left (按 BACKGROUND_INPUT 分发, 老行为)
    DRAFT_AUTO_PICK_CLICK = "adb"
    # 「最优等待点」的极角范围（度，用户 2026-11-08 定稿）：我方在本极角 ±该角度内挑等待点。
    #   · 模拟器 v1.9.1 = 30°（"不绕将近整圈"）；
    #   · 用户 2026-11-08 拍板放宽到 **90°** —— 这样能走到"侧后方迎头等"，
    #     实机场景 scene_draft_new_7 的单腿 t_pick 从 17.44s 降到 13.78s；
    #   · 越小越保守（只在原地附近等，目标在对面时要干等半圈），越大越激进（要绕远路）。
    DRAFT_AUTO_PICK_WAIT_MAX_DEG = 90.0
    # 「到等待点后允许站着干等多久」（秒，用户 2026-11-08 18:2x 硬要求：< 0.5s）。
    # 实机实测：「我看到的就起码等了 3 秒！」—— 原因是规划给的是"一次到位"的长腿：
    #   走过去 5.4s → 到点后 wait_until(hold_until) 干等 7.4s → 才切入目标。
    # 现在：只要「到达等待点 → 可以切入」之间要干等超过这个值，就把这条腿**拆成两段**——
    #   先只走到等待点（人一直在动），下一轮重新规划；始终不出现"站着不动"。
    DRAFT_AUTO_PICK_WAIT_STAND_MAX = 2.0
    # 整体提前量 (秒, 用户 m01xxx 2026-11-07 19:40 明确要求)
    #   「我希望的是[整体提前], 而不是点击提前。因为我观察到, 是你的 equip 和 对应 unit
    #     都落后 0.2s 左右。」
    # 采集→识别→规划这条链路本身就是 0.2s 的历史: 截到的一帧在识别完时已经过期 ms 毫秒,
    # 而 adb 点击下发到游戏生效又要再等 100~300ms。只在"点击时刻"上补(老做法)会让
    #   · 规划用的还是旧位置的装备/我方坐标(画面上也照样落后)
    #   · 复核时刻、hold_until 全部偏晚
    # 所以改成**整条时间轴前移**: 装备角位置、我方起点、规划 t0、绘制时刻统一 + LEAD,
    # 面板里可实时改(utils/draft_params.py), 默认 0.20s。
    # 整体提前量的**微调滑块**（秒，-3 ~ +3，用户 2026-11-10 02:2x）：
    # 基准不再手调 —— 用**实测识别延迟**（`CarouselWalker.recog_lag` = 截图→识别完成的间隔），
    # 这里只加一个偏移量微调：
    #   0    = 完全用实测延迟（推荐默认）
    #   > 0  = 再往前多推（画面跟上/超前）
    #   < 0  = 往后推（画面略滞后，用来对齐肉眼看到的游戏）
    DRAFT_AUTO_PICK_LEAD = 0.0
    # 同步间隔（秒，用户 2026-11-10 02:4x，默认 0.5）：
    # 「识别出来之后看一下跟上次是不是间隔了 0.5 秒，就开始重新同步」
    # 外推最多这么长，超过就**不再往外推**、等下一次识别重新锚定
    # （角速度有误差，推越久越偏 —— 用户原话"后面都是外推，随便乱推，完全对不上"）。
    DRAFT_AUTO_PICK_SYNC_INTERVAL = 0.5
    # 选秀里要不要识别"我方小小英雄"（绿血条）（用户 2026-11-10 01:5x 质疑：
    # 「我们为什么要识别英雄？不是只识别装备吗」）。
    # 实测 `detect_heroes` 占 detect 耗时的 **60%**（361ms vs 144ms），而实机日志里
    # 绿血条几乎每次都认不出来（`hero_src` 一直是 `dr` / `near-spawn`）—— 纯白花 217ms。
    # 我方位置本来就有两条稳定兜底：出生点常量 (947,903) + 走位航位推算(dr)。
    # 所以**默认关**；想试"绿血条能不能认出我方"时再打开。
    DRAFT_AUTO_PICK_DETECT_HEROES = False
    # 识别**易失灵区**（画面坐标 1920x1080）：用户 2026-11-10 01:5x 实测
    # 「到了左下角这个位置，装备图标识别就经常失灵」（被地形/特效遮挡）。
    # 该区域内判"离场/偏离"时放宽：偏离阈值 ×2、连续次数 +2 ——
    # 避免把"这一块认不出来"当成"装备被人捡走了"。
    DRAFT_AUTO_PICK_BLIND_ROI = (300.0, 600.0, 980.0, 1080.0)
    # 点击额外提前量 (秒). 整体提前已覆盖 adb 下发延迟, 这里默认 0 = 不再额外提前;
    # 若整体提前调完后仍然"慢半拍", 也可以在这里再加一点(面板可调)。
    DRAFT_AUTO_PICK_CLICK_LEAD = 0.0
    # 拾取确认的几何判据 (px, 用户 2026-11-07 21:00 当场打回后加的):
    # 「目标轨迹消失」不等于「我们捡到」—— 别人抢走同样会消失，实机就误报过
    # "拾取成功 暴风大剑"而其实没拿到。复核时**我方英雄离目标最后被看到的位置**
    # 超过这个距离，就不算我们拾到（改判"被人抢走/识别漂移"）。
    # 口径: 目标 `unit_xy`(棋子脚下地面点) vs 我方 `hero_xy`(落点), 两者都是落点口径。
    DRAFT_AUTO_PICK_PICK_REACH_PX = 90.0
    # 实机调试参数外置文件 (用户 m01821): 存在就用它的值覆盖上面这些 DRAFT_AUTO_PICK_*,
    # 由 `utils/draft_params.py` 统一读写; 模拟器 HTML 面板通过 scripts/draft_params_server.py 改它。
    DRAFT_PARAMS_JSON = "config/draft_live_params.json"

    # ---- 对局结束判断 (recognize/game_over.py): InfoTypes.IsOver / Game 组 ----
    GAME_OVER_STRATEGY = "both"  # players=自己条目(name=None)判死: 血量<=0 或 行内红色淘汰标记× /
                                 # end_screen=结算画面[现在退出][继续观看]按钮模板匹配 / both=任一命中
    GAME_OVER_TEMPLATE_THRESHOLD = 0.75  # 结算按钮模板匹配阈值 (实测结束画面 1.00, 对局中 ≤0.48)
    # is_over 结果短 TTL 缓存 (用户口径 2026-10-17, 动作提速): 未传 players 时 is_over 要做
    # 玩家列表区域 OCR (~0.5s), 而轮询每 2~3 秒就调一次 —— 实测它占 poll_state 一半耗时。
    # 只缓存**非结束(False)**结果; 命中"已结束(True)"一律实时复核, 免得把一次误判延长成多次。
    GAME_OVER_CACHE = True       # 总开关 (False = 每次实时判定, 与改造前逐字节等价)
    GAME_OVER_CACHE_SECONDS = 2.0  # 缓存有效期 (秒): 结算判定最多晚这么久反映
    # 动作前**阶段检查结果**的短 TTL 缓存 (用户口径 2026-10-17, 动作提速): 阶段检查
    # (observer.poll_state 最轻档) 实测 0.49~0.83s, 大头是顶栏窄带 OCR + phase.detect;
    # 人工单步台常连点同一个动作 (buy 连点/refresh 连点), 1s 内阶段不可能翻脸 —— 命中就整趟
    # 跳过 OCR。只作用于 runner_client 的 do_action (采集主链路 / Runner 轮询照旧每次实读)。
    PHASE_CHECK_CACHE = True        # 总开关 (False = 每次动作都实读, 与改造前等价)
    PHASE_CHECK_CACHE_SECONDS = 1.0  # 缓存有效期 (秒): 阶段切换最多晚这么久反映
    PLAYERS_EXPECT_COUNT = 8  # 右侧玩家列表期望行数, 不足即抛错 (淘汰玩家整行变暗 OCR 常漏检, 缺行必须显式暴露)
    # 自己行判据 (血量视图, 供 extract.own_row_index): 自己行的血量数字是大字号且明显左移 —
    # 实测右缘 1781~1798; 其他玩家 (含名字 OCR 漏检、甚至被读成假名如 "X7" 的行) 恒右对齐到 1819~1828.
    # 取两者之间分界. 判据只看几何而非名字, 因为自己行上方的图标/大字可能被 OCR 读成一个假名.
    PLAYERS_OWN_ROW_X2_MAX = 1810
    # **自己行主判据: 血量数字的字号高** (用户 m05143-2 实测 37 帧失败案例后新增):
    # 自己行血量是大字号 (框高 30~36px), 其他玩家 20~29px → "最高且唯一, 且至少比其它行中位数
    # 高这么多" 即自己行. 实测 margin=3.0 时命中 25/37、**错行 0**; 降到 2.0/1.0 会开始判错
    # (1022_140852 判到第 3 行) —— 所以宁可判不出 (返回 None 由调用方降级), 也不猜.
    PLAYERS_OWN_ROW_HP_H_MARGIN = 3.0
    # 自己行血量的**绝对字号下限** (px): 实测自己行 30~36, 其他行 20~29。低于这个值一律
    # 认为"没有大字号行"(全行都小的帧) → 判不出自己行, 由调用方降级, 不猜。
    # 实测 37 帧: 加这条后错行从 1 → **0** (25/37 → 26/37 命中, 代价是多几个 None)。
    PLAYERS_OWN_ROW_HP_H_MIN = 29.0
    # 自己行血量的**绝对值上限** (px): 实测自己行 30~36。**已淘汰玩家显示 "-99" 这类负血量**,
    # 负号会把 OCR 框撑到 43~47px, 比存活行(含自己行)都高 → 抢走"最高且唯一"判据 →
    # `game_over._by_players` 把该淘汰行当成我、读到 hp<=0 → **正在打的对局被误判结束**
    # (2026-11-05 第 7 局实测: 4-4 局中误退、假吃鸡、下一局接管残局, P0)。
    # 超过这个高度的框一律不参与"字号最高"竞争 (判不出就 None, 不猜)。
    PLAYERS_OWN_ROW_HP_H_MAX = 40.0

    # ---- 雷电模拟器窗口标识: 类名优先匹配, 标题关键字兜底 ----
    LDPLAYER_CLASS = "LDPlayerMainFrame"
    LDPLAYER_TITLE_KEYWORDS = ("雷电模拟器", "LDPlayer")
    # 多开时指定操作哪个模拟器实例 (2026-10-14, 用户 m04034 起第二个实例之后加的):
    #   ""              = 自动: **用最旧创建的那个实例** (按实例进程创建时间升序, 用户 m05080;
    #                     m04916 原口径是"最新的", 已反转 —— 游戏一直跑在旧实例里)
    #   "0" / "1"       = 按**雷电多开器里的实例 ID**选 (也就是多开器左边那列 / 标题里的后缀),
    #                     取不到 ID 时退到枚举下标, 再退到十进制句柄
    #   "12345678" / "0x00bc614e" = 按窗口句柄选
    #   "雷电模拟器-0"   = 按标题选 (精确优先, 再退回子串; 雷电多开时标题形如 "雷电模拟器-0" / "雷电模拟器-0-1")
    # 生效方式: 选中的实例被排到 `find_ldplayer()` 返回列表的第 0 位, 于是 `capture/screen.py`
    # 与 `control/input.py` 里所有 `wins[0]` 调用点自动跟着走 —— 多开不需要再改坐标换算.
    LDPLAYER_WINDOW = ""
    # 多开"共享机器"模式 (2026-10-24 事故后加): 为 True 时, 上面 `LDPLAYER_WINDOW`
    # 指定的实例**匹配不上就报错**, 而不是静默退回最旧实例。
    # 背景: 两条线各跑一台模拟器 (实例-0 / 实例-1) 时, 实例-1 的 RenderWindow 只因游戏崩溃
    # 短暂消失, `find_ldplayer()` 就会把 `wins[0]` 换成实例-0 —— runner 于是把游戏开到
    # 另一条线的机器上, 还对着那边点击。宁可报错停下, 也不能误伤别的实例。
    # `scripts/runner_by_mem.py` 会置 True。
    LDPLAYER_SHARED_MACHINE = False
    # 雷电自带的命令行工具 (ldconsole.exe) 路径; 留空 = 自动从正在运行的 dnplayer.exe 所在目录找.
    # 用途: `ldconsole.exe list2` 是拿"雷电实例 ID <-> 窗口句柄 <-> 进程 pid"的权威来源.
    LDPLAYER_CONSOLE = ""

    # ---- AI 兜底 Agent (utils/ai_agent.py): 多模态 LLM 找可点击控件 ----
    # 背景 (用户 2026-10-15): 旧兜底链路"AI 识别 -> 输出 rect/center -> 点击 -> 再判断"里,
    # 点错了只会"反省理由"却**不给新坐标**, 于原地打转。现在改成**有状态 Agent**: 同一任务
    # 共用一个短期记忆 (messages), 每轮把新截图回灌, AI 必须反省并给出**新**坐标或判定完成;
    # 期间可调 mark_rect (位置锚点标注图) 与 ocr_region (指定区域 OCR) 两个工具辅助校验。
    AI_AGENT_ENABLED = True        # 总开关; False 时 ai_fallback 直接返回 None (回到纯规则识别)
    # 工具调用协议 (用户拍板: "要求参数可配置吧, 默认func-calling"):
    #   "fc"    = OpenAI 原生 function calling (默认) —— 走 tools 参数, 收 tool_calls;
    #   "react" = prompt 里约定 JSON 工具协议 (兼容不支持 fc 的模型)。
    # fc 调用抛异常或没返回 tool_calls 时会**自动降级 react** 并打 `[!]` 行, 不因协议问题卡死。
    AI_AGENT_TOOL_MODE = "fc"
    AI_AGENT_MAX_ROUNDS = 5        # 单个任务允许的「截图 -> 校验 -> 点击」轮数上限 (用户: "暂定为5")
    AI_AGENT_CHECK_TIMES = 5       # ai_recognizer 自检修正轮数默认值 (每轮修正后**必须复检**)
    AI_AGENT_MAX_TOOL_STEPS = 4    # 单轮截图内 AI 允许的工具调用步数上限 (防一次请求里无限调工具)
    AI_AGENT_LEARN_TEMPLATES = True  # AI 兜底把界面推进后, 把该控件写进模板库 (文字锚点 + PNG 双写)
    AI_AGENT_RECORD_DIR = WORK_SPACE_DIR / "agent_recognized_result"  # 会话回放: 原图/标注图/session.jsonl

    # ---- AI 视觉标注 (utils/ai_chess_annotator.py): 输入一张图 -> 所有弈子 + 小小英雄的框 ----
    # 背景 (用户 2026-10-22): "我们已经能通过 deepseek-v4.1-flash 的能力, 进行准确的数据标注了",
    # 要求把会话「🔍 探索｜查看 show_rect_in_img 脚本的功能」里的标注能力打包成一个方法。
    # 标注口径 (用户在原会话里逐条纠正): 框只框「人物模型本体」那一小块精准区域,
    #   不要包含血条 / 血条下方紧跟的装备图标 / ID 文字 / 台座; 小小英雄单独标一种颜色。
    # 模型: api.deepseek.com 上叫 `deepseek-flash` = deepseek-v4.1-flash (实测支持图片输入);
    #   同列表里的 `deepseek-v4-pro` **不支持视觉** (会回"无法查看图片")。
    # 2026-11-04 用户 m01061「config 中的模型设置为 auto/deepseek-v4.1-flash」: 改走本地 magpie
    #   聚合代理 (authinfo.yaml 的 magpie 段)。实测该模型经代理读图正常 (2.4s, 正确读出手写文字),
    #   故视觉标注链路可直连; 想回官方直连就把下面两行改回 deepseek / deepseek-flash。
    AI_CHESS_PROVIDER_ID = "magpie"   # tools/my_llm/authinfo.yaml 里的 provider
    AI_CHESS_MODEL = "auto/deepseek-v4.1-flash"   # = deepseek-v4.1-flash
    AI_CHESS_CHECK_TIMES = 3     # 视觉自检-修正轮数 (每轮修正后必须复检; 0 = 不自检)
    AI_CHESS_MAX_SIDE = 1920     # 送模型的最长边上限 (超过才缩放, 坐标再换算回原图)
    # 标注图 + 结果 json 落盘目录: 放项目内 tempdir/ (gitignore 的擦写目录) —— 这是"人工核查用"的
    # 临时产物, 不是要提交的资产; 且受限沙箱下 workspace/ 根不可写 (实测 Permission denied)。
    AI_CHESS_RECORD_DIR = PROJECT_ROOT / "tempdir" / "ai_chess_annotations"

    # ---- 僵死画面唤醒 (capture/window.py::wake_window; 用户 2026-10-15) ----
    # 背景 (用户现场): 模拟器窗口被拖到桌面左下角 / 大半移出屏幕下沿后, Windows 不再给那块
    #   区域发重绘, 模拟器渲染器冻在最后一帧 —— 采集端每帧读出来的都是同一张旧图, `phase=unknown`
    #   连着 25 个回合读不出 (unknown 持续 126s > 30s 门), 被误判成"已离开对局", 白跑一遍开局流程。
    # 对策: 连续 `WINDOW_WAKE_AFTER_ROUNDS` 个回合读不出阶段 → 把窗口**挪到桌面左上角 (0,0)**
    #   (超屏尺寸一并压回工作区: 尺寸一变必然触发重绘) + 置前 + 在渲染区送几下真鼠标, 逼它重绘;
    #   随后探测 `WINDOW_WAKE_PROBE_SECONDS` 秒, 画面恢复 (读得到回合号/阶段) 就按**原 rect** 挪回
    #   去继续采集; 仍读不出则保持原流程; 累计 `WINDOW_WAKE_RESTART_ROUNDS` 个回合都读不出 →
    #   判实例卡死, quit + launch **重启这个实例** (用户口径: "连续到50回合都读取不出来...杀掉模拟器重新启动")。
    WINDOW_WAKE_ENABLED = True          # 总开关 (False = 退回"读不出就照旧判离局/靠多开守卫重启"的老行为)
    WINDOW_WAKE_AFTER_ROUNDS = 15       # 连续读不出多少个回合后触发一次唤醒 (与 unknown_streak_give_up 同口径)
    WINDOW_WAKE_PROBE_SECONDS = 20.0    # 唤醒后最多探测多少秒 (读到回合号/阶段即算恢复)
    WINDOW_WAKE_PROBE_INTERVAL = 2.0    # 探测的抓帧间隔秒数
    WINDOW_WAKE_RESTART_ROUNDS = 50     # 累计读不出多少回合 → 判实例卡死, 杀实例重启 (用户口径: 50)
    # 唤醒时送真鼠标 (移动/点击/拖拽) —— 用户明确要求 "鼠标也随便点击、拖拽几下, 让程序苏醒"。
    # **注意**: 这是 `ALLOW_FOREGROUND_FALLBACK` 那条"全项目不许前台键鼠"禁令的**唯一例外**,
    #   只在"画面已僵死、后台输入注定无效"时用; 不想被借走键鼠就置 False (那样只挪窗口不送鼠标)。
    WINDOW_WAKE_SEND_MOUSE = True
    WINDOW_WAKE_CLICKS = 2              # 唤醒时在渲染区点击几次
    WINDOW_WAKE_DRAGS = 1               # 唤醒时在渲染区拖拽几次
    # "挪回屏幕内"的**前提** (用户口径 2026-10-17): 只有窗口"离屏面积占比 >= 该值"才把窗口挪回
    #   屏幕内 —— "检测到雷电模拟器有 2/3 都在屏幕外, 就需要把模拟器移动到屏幕内"。**激活是无条件
    #   的** (激活完立刻释放前台锁, 不锁用户); 窗口在屏幕内时不挪窗口/不送鼠标, 只激活。
    #   置 0 = 退回旧行为 (无条件挪窗口 + 激活)。
    WINDOW_WAKE_OFFSCREEN_RATIO = 2.0 / 3.0

    # ---- 运行控制 ----
    STOP_FLAG = False  # 置 True 时各工作循环应暂停/退出
    # 超过该回合主动重开下一局 (自动重开阈值, 改这里即可调试; runner 与 scripts.restart_after_stage 共用)
    RESTART_AFTER_STAGE = (9, 6)
    # 键鼠输入方式: True=win32 PostMessage 后台发送到渲染子窗口 (不抢前台, 不占真实鼠标);
    # False=SetCursorPos+mouse_event 前台真点击 (会短暂置前模拟器窗口)
    BACKGROUND_INPUT = True
    # **禁止任何前台真实键鼠** (用户 m05001/m05003): "你丫的现在怎么占用我前台键鼠了?
    #   不是后台么? 全部要用后台键鼠方案!" —— 起因是内嵌 WebView (月卡/支付/活动页的关闭 X)
    #   PostMessage 点不动, 当时退回 `activate()` + `SetCursorPos` + `mouse_event` 的前台真实鼠标,
    #   而那会激活模拟器窗口并抢走用户正在用的键鼠。现在这条退路改走**雷电 adb 注入**
    #   (`control/input.py::adb_tap`, 对 WebView 一样有效, 宿主键鼠全程不动)。
    #   本开关只留给"真的要前台调试"的场合, 默认 False = 全项目不再有任何前台键鼠操作。
    ALLOW_FOREGROUND_FALLBACK = False


default_config = DefaultConfig()


def set_collect_profile(enabled: bool) -> None:
    """切换「采集模式」口径 (用户 m00802) —— 只影响 `PROBE_CLICK_UNKNOWN` 这类"速度 vs 正确"开关。

    用户原话: "我要求关掉, 是因为, 我在正式runner实际对局中, 需要绝对速度关掉.
              而我们采集数据脚本 collect_v2, 是要保证绝对正确, 所以要开启!
              这个参数应该分开, 分为 runner 模式, 和 collect 模式"

    - `set_collect_profile(True)` (collect_v2 开跑前调用): 正确优先 —— YOLO 认不出的格
      必须点击详情卡二次识别, 不许直接落 unknown;
    - 不调用 (正式 runner 进程): 保持 `PROBE_CLICK_UNKNOWN_RUNNER` = 速度优先。
    两个进程各自独立, 互不影响; 单测/离线脚本可来回切。
    """
    default_config.PROBE_CLICK_UNKNOWN = bool(
        default_config.PROBE_CLICK_UNKNOWN_COLLECT if enabled
        else default_config.PROBE_CLICK_UNKNOWN_RUNNER)


class InfoTypes:
    """extract_info 提取类型选择 (recognize/extract.py): 传单个字符串或其列表.

    按需选择可跳过无关的整图 OCR / 棋盘检测 / 裁剪 OCR, 大幅提速;
    默认 InfoTypes.All 输出全部键, 行为与旧版 extract_info 一致.
    """
    Stage = "stage"          # 回合号 2-1
    Phase = "phase"          # 对局阶段: prep/draft/augment/combat/unknown + 中文说明
    Hex = "hex"              # 海克斯选择状态: hex_0 未选择 / hex_1 已选择 / None 非海克斯界面
    RoundTime = "round_time"  # 回合剩余秒数
    Level = "level"          # 等级
    Xp = "xp"                # 经验 [当前, 所需]
    Gold = "gold"            # 金币
    XpBuyCost = "xp_buy_cost"    # 购买经验按钮费用
    RefreshCost = "refresh_cost"  # 刷新按钮费用
    FreeRefresh = "free_refresh"  # 免费刷新次数
    Traits = "traits"        # 左侧羁绊列表
    Players = "players"      # 右侧玩家血量列表
    Shop = "shop"            # 商店 5 张卡牌
    Board = "board"          # 场上奕子
    Bench = "bench"          # 备战席奕子
    LittleLegend = "little_legend"  # 小小英雄位置 (棋盘+备战席)
    Hextech = "hextech"      # 海克斯强化符文卡 (仅海克斯选择阶段-未选择时有内容)
    Equip = "equip"          # 左侧装备栏 (equipment.extract_equipment, 含消耗品/道具)
    IsOver = "is_over"       # 对局是否已结束 (game_over.is_over, 策略见 config.GAME_OVER_STRATEGY)
    Game = "game"            # 组合: stage/phase/hex/round_time/level/xp/gold + 三项按钮费用 + is_over
    All = "all"              # 组合: 全部键


# ---------------------------------------------------------------------------
# 子进程解释器 (2026-10-18 exe 打包)
# ---------------------------------------------------------------------------
def python_exe() -> str:
    """能跑 ``-m <模块>`` 的**项目 Python 解释器**绝对路径 (给"开子进程跑脚本"的功能用).

    背景: ``scripts.runner_client`` 有几个按钮是靠 `python -m xxx` 起子进程的 ([整体运行 runner.py] /
    [启动 api_server] / OCR 服务兜底拉起)。源码运行时 ``sys.executable`` 就是项目解释器; 但打包成
    exe 后 ``sys.executable`` 是 **exe 自己**, 拿它拼 `python -m trae_agent.runner` 会去跑 exe
    (参数全被 runner_client 的 argparse 拒掉), 所以这里按下面的顺序另找:

      1. 环境变量 ``JCC_PYTHON`` —— 最高优先, 换机器/便携部署时手动指定;
      2. 项目根的 ``.venv/Scripts/python.exe`` / ``venv/Scripts/python.exe``;
      3. 常见 conda 环境 ``<用户目录>/miniconda3/envs/jcc_robot/python.exe`` (本机默认环境);
      4. PATH 里的 ``python``。
    都找不到就抛 ``RuntimeError`` (界面 ``timed`` 会打 ``[X] ... 路径: ...``, 不会静默乱起进程)。
    """
    env = os.environ.get("JCC_PYTHON", "").strip()
    if env:
        cand = Path(os.path.expandvars(env)).expanduser()
        if cand.is_file():
            return str(cand)
        raise RuntimeError(f"JCC_PYTHON 指向的解释器不存在 路径: {cand}")
    if not getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        # **pythonw.exe 要换成同目录的 python.exe** (用户 2026-10-20 报的 bug): 客户端默认用
        # pythonw 启动 (无控制台), 若拿它去起子进程, 子进程的 ``sys.stdout`` 是 ``None``,
        # ``print`` 全部静默丢弃 —— 外面看着就是"新开一个全黑的控制台窗口, 一直没输出",
        # 而 ``_run_cli_streaming`` 也一行都读不到 (界面像卡死)。pythonw 只适合"自己这个 GUI 进程",
        # 凡是拿 ``-m`` 起脚本的地方都必须用带控制台的 python.exe。
        if exe.name.lower() == "pythonw.exe":
            console = exe.with_name("python.exe")
            if console.is_file():
                return str(console)
        return sys.executable                     # 源码运行: 就是当前项目解释器
    home = Path.home()
    root = default_config.PROJECT_ROOT   # 模块级没有 PROJECT_ROOT (那是 DefaultConfig 的类属性)
    cands = [
        root / ".venv" / "Scripts" / "python.exe",
        root / "venv" / "Scripts" / "python.exe",
        home / "miniconda3" / "envs" / "jcc_robot" / "python.exe",
        home / "anaconda3" / "envs" / "jcc_robot" / "python.exe",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        cands.append(Path(local_appdata) / "miniconda3" / "envs" / "jcc_robot" / "python.exe")
    for cand in cands:
        if cand.is_file():
            return str(cand)
    found = shutil.which("python")
    if found:
        return found
    raise RuntimeError("打包版找不到项目 Python 解释器 (可设环境变量 JCC_PYTHON 指向 python.exe)")

