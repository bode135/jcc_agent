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



    # ---- 路径 ----
    # 工程根 = 本文件 (config/config.py) 的上两级目录。
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    CAPTURE_DIR = PROJECT_ROOT / "workspace" / "captures"
    SCRAPE_INFO_DIR = PROJECT_ROOT / "workspace" / "scrape_info"  # 爬虫数据根目录
    EXTRACT_INFO_DIR = PROJECT_ROOT / "workspace" / "extract_info"  # 对局信息抽取结果目录


    # ---- 临时文件根 ----
    # 所有临时目录/临时文件一律建在 tempdir/ 下 (整目录递归清理不漏; 已在 .gitignore 中忽略)。
    TEMP_ROOT = PROJECT_ROOT / "tempdir"





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
    # **奖励球识别方案 (2026-10-13)**: 本工程只保留**颜色法** (蓝/银/金三色阈值,
    # 见 recognize/reward_ball.py) —— 银球只能靠它 (没有银球模板)。实测单帧 0.27s。
    # **奖励球检测区域 (2026-11-04 用户 m02276)**: 颜色法原先扫 (250,85,1510,710) 整块,
    # 战斗期会把**对方备战席**上的棋子/旗子误当成奖励球点过去 —— 对方备战席根本没有球。
    # 收紧到"己方棋盘可玩区" (2026-10-13 标定: 真球 x∈[639,1238] y∈[212,475]):
    #   x 560~1310 去掉左右边缘装饰; y 190~645 去掉上方对方半场/石墙(y<190) 与下方备战席(y>645)。
    # 只影响 utils.reward_ball.detect_balls 这条生产链路; recognize.reward_ball.find_balls 的单测不受影响。
    BALL_REGION = (560, 190, 1310, 645)
    BENCH_SLOT_CENTERS = tuple((510 + 112 * i, 754) for i in range(9))
    BENCH_SLOT_MAX_DISTANCE = 65
    BENCH_ITEM_TEMPLATE_DIR = PROJECT_ROOT / "recognize" / "templates" / "bench_items"
    # **是否锻造器**的判定阈值 (用户口径 2026-10-12: 优先识别[是否锻造器], 不允许遗漏;
    # 即使误判成装/基础也可接受)。实测 60 帧/63 个真锻造器: 真品下限 0.849, 非锻造器上限 0.848;
    # 0.85 -> 召回 98.4% (会漏), 0.80/0.75 -> 召回 100% (误报 ~1%)。故取 0.75 更偏召回。
    BENCH_ITEM_FORGE_THRESHOLD = 0.75
    OCR_INTRA_OP_THREADS = 2  # 模拟器同时运行时避免三个ONNX模型争抢全部CPU线程
    OCR_INTER_OP_THREADS = 1





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


    # --- 整机内存闸门 (utils/mem_guard.py, 依赖 psutil) ---
    # 可用物理内存低于该值时**不建** GPU 引擎 (CPU 仍可用, 只慢约 1s/次):
    # CUDA OOM 会让 ORT 回落到 CPU, 反而把压力全推给主机内存。
    OCR_MIN_FREE_GB = float(os.environ.get("JCC_OCR_MIN_FREE_GB", "2.0"))

    # --- 分步耗时打印: recognize/extract.py 打印 "--- extract.<步骤> --- 耗时s" (默认关).
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
    # 默认开 (2026-11-12 用户拍板: 100 张同刻配对干净图上, 对照原 det 方法
    #   500/500 槽 100% 全一致; 新方法中位 111ms vs 原方法 663ms, 提速约 5 倍, 达标)。
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
    # 价格槽的免 det 快路径 (用户 2026-11-19 要求): 白掩码取"最右那段白字" -> 白二值化 -> 4x
    # -> `ocr.recognize_line`(use_det=False)。实测同一帧 5 槽 24ms 且 5/5 读对(含原 det 路径
    # 漏掉的槽4), 原路径 340~460ms 只读到 4 槽。关掉则回到 `recognize_region(upscale=4)` 合并带。
    # 读不出的槽仍会自动回落原路径 (宁慢不错)。
    SHOP_PRICE_GEO_RECOGNIZE = True

    # ---- 回合剩余时间本地预测缓存 (recognize/extract.RemainTimeCache, 用户口径 2026-10-17) ----
    #   开启后: 第一次在某 (回合号, 阶段) 读到 remain_time 时记入缓存, 之后同一 (回合号, 阶段) 内
    #   不再跑 OCR, 直接用 `首值 - 本地已过秒数` 推算剩余时间; prep 的过期门槛 = 0 (推算到 0 才重读,
    #   prep 必然走完不用怕被延长), combat 用 REMAIN_CACHE_COMBAT_KEEP (combat 可能被突然缩短,
    #   也可能被延长 = 加时赛, 所以推算到 5 秒就重读)。默认关闭 (新增开关一律默认关, 不影响原链路)。
    REMAIN_TIME_CACHE = False
    # combat 阶段缓存保留门槛 (秒): 推算剩余 <= 该值 就重读 OCR —— 应对"15s 突然变 3s"/"3-2-1s
    #   又跳回 20s"的加时赛; 用户口径: "提取时间 3-2 阶段剩余 20s, 则改为 remain_time 5s 才过期"。
    REMAIN_CACHE_COMBAT_KEEP = 5.0
    STAGE_SEARCH_REGION = (560, 0, 850, 65)  # 左界含选装备等顶栏偏左的回合号 (3-4 轮抽实测起点约605, 原620会截断)

    # ---- YOLO 单次前向识别我方奕子 (recognize/yolo_units.py) ----
    # 一次前向同时给出"定位 + 身份", 比逐格点击读详情卡快一个数量级; 训练/导出见 train_yolo/。
    # ---- 动态提取策略 (用户 2026-10-18 要求 config 可控) ----
    # "yolo_detail": YOLO 定位 + 可信格免点击 + 其余点详情卡 (默认; 快慢折中)
    # "detail":      纯点击详情卡 (禁用 YOLO 免点击/快路; 每格都点, 最准但最慢 14~25s)
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
    YOLO_UNITS_CONF = 0.35            # 检出置信度阈值 (overlay 客户端可调, 下限 0.01)
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
    # ---- 低阈值复核 (用户 2026-10-20 要求) ----
    # 场景: 血条已经定位到"这里确实有个奕子", 但 YOLO 主阈值没给名字、回溯法也补不上。
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
    MATCH_LOWCONF = 0.10
    # ---- 头像模板匹配总开关 (用户 2026-11-19 拍板: 默认关) ----
    # 棋子名字一律以 YOLO 为准 (父项目默认也是关的)。关掉后 `board.extract_units`
    # 照常给血条框/星级/格位, 只是 name/score 恒为 None —— 身份由 YOLO 那条链路提供。
    # 打开代价: 全棋子库 138 变体 x 5 尺度 x 2 翻转 的暴力比对 (单只比对 ~50ms) +
    # GrabCut 抠图 (~31ms, iters=2)。9 只约 0.75s。
    # 收益很低: 实测 6 根血条只有 1 根分数过 0.55 命名线, 且 bar0 把可酷伯认成蕾欧娜。
    PORTRAIT_MATCH_ENABLED = False
    # 模板兜底 (tpl_lowconf) 专用的采信线 —— 用户 2026-10-21 阈值扫描结论。
    # 在 132 帧/963 幅 GT 的测试集上扫了 30 组 (YOLO低阈值 0.05~0.30) × (模板阈值 0.10~0.70):
    #   0.10 → 认对 936 / 错名 26 / 未命名 0
    #   0.30 → 认对 936 / 错名 25 / 未命名 1   ← 最优 (错名少 1 个, 代价只是 1 个格留 unknown)
    #   0.50 → 认对 935 / 错名 25 / 未命名 2
    # 也就是"模板兜底低于 0.30 的基本是猜", 只留高分那一档。




    # ---- 难例自动登记: 血条定位到有单位、但 YOLO 低阈值复核也补不上名字时, 裁小图 + 记候选分布,
    # 归档成训练难例 (**只登记证据, 不猜名字**)。
    YOLO_UNITS_MAX_DET = 20           # 单帧最多保留几个框
    YOLO_UNITS_PROVIDER = None        # None=自动 (CUDA 优先, 回落 CPU); 或 "CPUExecutionProvider"
    # ---- 免点击的两道硬闸门 ----
    # ① 逐类白名单 (recognize/yolo_trust.py + config/yolo_trust/<模型名>.json): "名字可不可信"。
    # ② 血条 ∪ YOLO 取并集: "这里到底有没有东西" —— 白名单管不住幻觉框, YOLO 框必须有血条佐证。
    YOLO_UNITS_UNION_BAR = True       # 关掉 = 只信 YOLO(不跑血条), 快 ~0.4s 但会漏 YOLO 没框的格
    YOLO_UNITS_TIMING = False         # 打印 预处理/推理/后处理 分步耗时
    # ---- prep 过场动画尾巴检测 + 重试 (用户 2026-10-18) ----
    # 战斗结束→prep 的过渡动画会遮挡棋盘, 这时 YOLO/血条都大面积认不出 (全 unknown)。
    # 判定 = prep 阶段 + (unknown 占比超阈值, 或与上一轮识别数量差距过大)
    #   -> 重新截图 + 重跑 YOLO+血条, 避免在过场动画里直接启动逐格点击 (白点 + 认错)。

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
    #         MOVE 当拖拽 -> 装备没离手 (前后帧比对: 库存没少 + 目标格没变 = 纯 no-op)。
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

    # ---- place/swap 棋盘拖拽"重拖放宽档" (2026-11-06, 第二轮实机 73 次 NG 中 68 次为拖拽零变化) ----
    # 实测: swap/place 成功率仅 65%, 失败时前后棋盘状态**完全不变**(拖拽整串没被游戏接收),
    # 不是落错格; 根因同 equip 重拖: 后台 PostMessage 的分步移动太快时游戏不识别为拖拽。
    # 首次拖拽原先走全局 DRAG_*; 2026-11-11 用户 m00224 起, 首拖也直接采用这套慢时序(见下)。
    # 现在首拖(timing="board")与"复核未生效"后的**重拖**都用这组放宽时序(更长按住 + 更多步 + 更长落点停留)。
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

    # ---- 实时调试绘制层 (overlay/debug_overlay.py + overlay/live_draw.py) ----
    # 在模拟器画面上**实时叠加**绘制识别结果 (纯外部 Win32 分层透明窗口, 只读屏幕、不碰游戏进程,
    # 贴 RenderWindow 客户区, 窗口隐藏/被遮挡时自动收起)。
    # 脚手架里由 `python -m scripts.overlay_info --ld-index 0` 启动 (被动观察: 只截图/识别/绘制)。
    OVERLAY_LD_INDEX = None             # 目标雷电实例 ID; None = 自动取第一个可用实例
    # 各项识别结果的绘制开关 (用户 m00172: "目前, 先显示 棋子、商店、装备栏、棋子携带装备 这几个")
    OVERLAY_SHOW_UNITS = False          # Yolo 棋子识别结果 (蓝框) —— 调试阶段只留 draft
    OVERLAY_SHOW_SHOP = False           # 商店识别结果 (橙框)
    OVERLAY_SHOW_EQUIP_BAR = False      # 装备栏识别结果 (青绿框)
    OVERLAY_SHOW_BOARD_EQUIPS = False   # 棋子携带装备识别结果 (品红框)
    OVERLAY_SHOW_BLOOD_BAR = False      # 血条识别结果 (深灰框) —— 默认关: board.extract_units 较慢
    OVERLAY_SHOW_BALLS_YOLO = False     # balls-yolo: 用 YOLO 检索场上球 (浅蓝框) —— 用户 m00656, 默认开
    OVERLAY_SHOW_BALLS_COLOR = False    # balls-color: 用 HSV 颜色法找奖励球 (浅蓝框) —— 用户 m00656, 默认关
    OVERLAY_SHOW_STAR_BADGE = True      # 血条左侧星级徽记框 (金框描边; 星级并进名字牌前缀)
    OVERLAY_SHOW_HERO = True            # 小小英雄: 等级数字块框 + "我方/敌方 · Lv6" 标签 (用户 m01362)
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
    # 自动 hide (用户 m00172 第 1 条): 目标窗口最小化 / 不可见 / 被别的窗口**整体盖住**时自动收起,
    # 恢复可见后自动重新贴回。窗口线程内定时器每 OVERLAY_WATCH_INTERVAL 秒巡检一次。
    OVERLAY_WATCH_INTERVAL = 0.5        # 可见性/遮挡巡检间隔 (秒)
    OVERLAY_HIDE_WHEN_OCCLUDED = True   # True = 整体被别的窗口盖住就收起; False = 只要可见就画


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



    # ---- 对局结束判断 (recognize/game_over.py): InfoTypes.IsOver / Game 组 ----
    GAME_OVER_STRATEGY = "both"  # players=自己条目(name=None)判死: 血量<=0 或 行内红色淘汰标记× /
                                 # end_screen=结算画面[现在退出][继续观看]按钮模板匹配 / both=任一命中
    GAME_OVER_TEMPLATE_THRESHOLD = 0.75  # 结算按钮模板匹配阈值 (实测结束画面 1.00, 对局中 ≤0.48)
    # is_over 结果短 TTL 缓存 (用户口径 2026-10-17, 动作提速): 未传 players 时 is_over 要做
    # 玩家列表区域 OCR (~0.5s), 而轮询每 2~3 秒就调一次 —— 实测它占 poll_state 一半耗时。
    # 只缓存**非结束(False)**结果; 命中"已结束(True)"一律实时复核, 免得把一次误判延长成多次。
    GAME_OVER_CACHE = True       # 总开关 (False = 每次实时判定, 与改造前逐字节等价)
    GAME_OVER_CACHE_SECONDS = 2.0  # 缓存有效期 (秒): 结算判定最多晚这么久反映
    # 动作前**阶段检查结果**的短 TTL 缓存: 阶段检查 0.49~0.83s (顶栏窄带 OCR + phase.detect),
    # 1s 内阶段不可能翻脸 —— 命中就整趟跳过 OCR。
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
    # 短暂消失, `find_ldplayer()` 就会把 `wins[0]` 换成实例-0 —— 程序于是把游戏开到
    # 另一条线的机器上, 还对着那边点击。宁可报错停下, 也不能误伤别的实例。
    LDPLAYER_SHARED_MACHINE = False
    # 雷电自带的命令行工具 (ldconsole.exe) 路径; 留空 = 自动从正在运行的 dnplayer.exe 所在目录找.
    # 用途: `ldconsole.exe list2` 是拿"雷电实例 ID <-> 窗口句柄 <-> 进程 pid"的权威来源.
    LDPLAYER_CONSOLE = ""



    # ---- 僵死画面唤醒 (capture/window.py::wake_window; 用户 2026-10-15) ----
    # 背景 (用户现场): 模拟器窗口被拖到桌面左下角 / 大半移出屏幕下沿后, Windows 不再给那块
    #   区域发重绘, 模拟器渲染器冻在最后一帧 —— 采集端每帧读出来的都是同一张旧图, `phase=unknown`
    #   连着 25 个回合读不出 (unknown 持续 126s > 30s 门), 被误判成"已离开对局", 白跑一遍开局流程。
    # 对策: 连续 `WINDOW_WAKE_AFTER_ROUNDS` 个回合读不出阶段 → 把窗口**挪到桌面左上角 (0,0)**
    #   (超屏尺寸一并压回工作区: 尺寸一变必然触发重绘) + 置前 + 在渲染区送几下真鼠标, 逼它重绘;
    #   随后探测 `WINDOW_WAKE_PROBE_SECONDS` 秒, 画面恢复 (读得到回合号/阶段) 就按**原 rect** 挪回
    #   去继续采集; 仍读不出则保持原流程; 累计 `WINDOW_WAKE_RESTART_ROUNDS` 个回合都读不出 →
    #   判实例卡死, quit + launch **重启这个实例** (用户口径: "连续到50回合都读取不出来...杀掉模拟器重新启动")。
    # 唤醒时送真鼠标 (移动/点击/拖拽) —— 用户明确要求 "鼠标也随便点击、拖拽几下, 让程序苏醒"。
    # **注意**: 这是 `ALLOW_FOREGROUND_FALLBACK` 那条"全项目不许前台键鼠"禁令的**唯一例外**,
    #   只在"画面已僵死、后台输入注定无效"时用; 不想被借走键鼠就置 False (那样只挪窗口不送鼠标)。
    # "挪回屏幕内"的**前提** (用户口径 2026-10-17): 只有窗口"离屏面积占比 >= 该值"才把窗口挪回
    #   屏幕内 —— "检测到雷电模拟器有 2/3 都在屏幕外, 就需要把模拟器移动到屏幕内"。**激活是无条件
    #   的** (激活完立刻释放前台锁, 不锁用户); 窗口在屏幕内时不挪窗口/不送鼠标, 只激活。
    #   置 0 = 退回旧行为 (无条件挪窗口 + 激活)。
    WINDOW_WAKE_OFFSCREEN_RATIO = 2.0 / 3.0

    # 键鼠输入方式: True=win32 PostMessage 后台发送到渲染子窗口 (不抢前台, 不占真实鼠标);
    # False=SetCursorPos+mouse_event 前台真点击 (会短暂置前模拟器窗口)
    BACKGROUND_INPUT = True
    # **禁止任何前台真实键鼠** (用户 m05001/m05003): "你丫的现在怎么占用我前台键鼠了?
    #   不是后台么? 全部要用后台键鼠方案!" —— 起因是内嵌 WebView (月卡/支付/活动页的关闭 X)
    #   PostMessage 点不动, 当时退回 `activate()` + `SetCursorPos` + `mouse_event` 的前台真实鼠标,
    #   而那会激活模拟器窗口并抢走用户正在用的键鼠。现在这条退路改走**雷电 adb 点击**
    #   (`control/input.py::adb_tap`, 对 WebView 一样有效, 宿主键鼠全程不动)。
    #   本开关只留给"真的要前台调试"的场合, 默认 False = 全项目不再有任何前台键鼠操作。
    ALLOW_FOREGROUND_FALLBACK = False


default_config = DefaultConfig()


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
