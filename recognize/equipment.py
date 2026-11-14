"""左侧装备栏识别: 定位凹槽内装备图标, 匹配图标库得到装备身份与详情.

方案 (todolist 装备识别 / equipment_info_extract):
    1. 归一化 1920x1080, 在左边缘条带内用颜色掩码+连通域定位装备凹槽 (自上而下排序);
    2. 每个凹槽先匹配消耗品模板 (装备拆卸器/重铸器, 图标库无此数据, 需单独模板匹配),
       再匹配图标库 workspace/scrape_info/v18.3b/images/items/{equipId}.png (64x64 RGBA);
    3. 命中 equipId 后到 chess_info/items.json 查名称/类型/效果等详情;
    4. 消耗品左上角可能有数量角标 (如拆卸器 "5"), 裁剪放大 OCR 提取.

运行示例 (项目根目录):
    python -m scripts.extract_equipment_info --img "images/examples/装备示例图.png"
"""

import hashlib
import json
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from PIL import Image

from config.config import default_config
from recognize import ocr, regions

# 版本化爬虫数据: 图标库 + 装备详情 (workspace/ 不入 git, 本地需先执行爬虫)
SCRAPE_VERSION = "v18.3b"
ITEMS_JSON = default_config.SCRAPE_INFO_DIR / SCRAPE_VERSION / "chess_info" / "items.json"
ITEM_ICON_DIR = default_config.SCRAPE_INFO_DIR / SCRAPE_VERSION / "images" / "items"
# 消耗品模板 (图标库缺失的特殊消耗品), 沿用 bench_items 的模板目录约定
CONSUMABLE_TEMPLATE_DIR = default_config.PROJECT_ROOT / "recognize" / "templates" / "equipment"
# ---- 实机模板库 (用户 2026-11-01 拍板: 用实机图标替代爬虫原画) ----
# 为什么: 原画是"扁平 2D 设计图", 游戏里是"3D 渲染体"(透视/高光/铜框/合成后变色), 两者不是
# 同一张图的两个分辨率 → 像素相关性上不去 (实测护卫纹章同尺寸只有 0.39)。换成从真实截图裁出来的
# 图标后, 自匹配分中位 0.814→0.914、≥0.90(批内早停) 命中率 21.5%→51.7%。
# 策略: **实机优先、原画兜底** —— 只覆盖实机库里有的那几件, 其余仍用原画, 缺模板不会"认不出"。
# 目录里文件名 = 装备名 (允许 `名字__变体.png` 形式多份变体, 取 __ 前段做名字)。
GAME_TEMPLATE_DIR = default_config.PROJECT_ROOT / "recognize" / "templates" / "equipment_game"
USE_GAME_TEMPLATES = True
_GAME_TPL_OVERLAID = 0   # 本次进程实际覆盖了多少个 equipId (仅用于启动日志)
# ---- 数量角标「数字模板」: 用本地模板匹配读 1~9, 替代 OCR (用户 2026-11-01 P0-18) ----
# 为什么: 数量角标 OCR 每次 70~300ms, 而且必须串行 (并发会把远程 OCR 服务打崩 WinError 10054),
# 于是它成了整栏并行的下限 —— 纯模板匹配已经 3.3x, 整栏却只有 1.1~1.4x。
# 做法见 scripts/build_digit_templates.py; 运行时 `_read_count` 先走模板, margin 不够/无模板才回退 OCR。
DIGIT_TEMPLATE_DIR = default_config.PROJECT_ROOT / "recognize" / "templates" / "digits"
USE_COUNT_TEMPLATES = True
COUNT_TEMPLATE_MIN_SCORE = 0.995   # 与模板的相关性要接近"逐像素相等"才采信 (实测同数字跨帧=1.000, 不同数字 5↔6 只有 0.965)
COUNT_BADGE_BOX = 34               # 从槽左上角取多大的方形当角标区 (角标实测 ~30x30)
COUNT_DIGIT_W, COUNT_DIGIT_H = 20, 28   # 数字归一化尺寸 (字形偏竖长)
COUNT_DIGIT_DARK = 95              # 金色底上的深色数字: 灰度低于此值算笔画

# 装备凹槽搜索条带与列分界: 坐标统一在 recognize/regions.py 维护 (手工标定只改那一处),
# 这里保留同名别名, 供 equipment 内部与历史调用方 (utils/draw_regions.py 等) 继续使用。
EQUIP_STRIP = regions.EQUIP_STRIP
SLOT_MIN_W, SLOT_MIN_H, SLOT_MIN_AREA = 30, 40, 1300   # 凹槽连通域尺寸量级 (图标约 60x60, 暗色图标亮区约 1480)
MAX_FILL_SLOTS = 12  # 槽位补插上限 (装备栏实际最多10槽): 异常帧下防止无限补插内存爆炸
SLOT_FRAME_RATIO_MIN = 0.3  # 装备槽铜框判据: 边界V>VALUE_MIN占比 (真槽实测0.56+, 羁绊图标0.02)
VALUE_MIN = 105   # 亮度阈值: 凹槽间隙/内衬是暗绿 (V≈65-95), 图标内容与铜框 V>110
CONSUMABLE_HUE_THRESHOLD = 0.85  # 色相直方图相关性阈值 (角标感知比对下正确对 0.87-1.00, 交叉对 ≤0.83 且 NCC<0.3)
#: 像素 NCC 门槛 —— **这才是消耗品通道真正的判别量** (色相 corr 对"蓝金配色"这类同色系图标几乎不分).
#: 2026-11-02 用户 m00245/m00248 实测 (实例0 5-5 局, 同一帧):
#:   真消耗品「装备拆卸器」 对自身模板 corr 0.882 / **NCC 0.455**  -> 必须过
#:   成装「海妖之怒」      撞「次级英雄复制器」模板 corr 0.868 / **NCC 0.331** -> 必须挡
#: 旧值 0.30 挡不住 0.331, 于是成装被当消耗品拒收 (`equip_to 3 -> board[4,7]` 那次事故)。
#: 0.42 在两者正中间, 两侧各留 ~0.04/0.09 余量。
CONSUMABLE_PIXEL_GATE = 0.42
#: 两通道都命中时的裁决余量: 只有消耗品**像素 NCC 明显高于**图标库分 (差 ≥ 这个值) 才允许
#: 消耗品覆盖图标库结论。两套分数都是像素级归一化相关, 量纲接近, 留 0.05 防抖。
CONSUMABLE_NCC_MARGIN = 0.05
BADGE_CROP = 0.45  # 消耗品比对时两侧对称剔除的左上角比例 (数量角标区, 角标边长约占图标 40-50%)
# --- 消耗品只可能排在装备栏最前面 3 格 (拆卸器/重铸器/宝箱/药水), 用户 2026-11-01 m01678 ---
# 只有前 3 格做消耗品匹配 + 数量角标 OCR; 更深格的 OCR 调用纯属浪费 (还会把远程 OCR 打崩),
# 同时也避免深格里的普通散件/成装被消耗品路径误判。
CONSUMABLE_MAX_SLOT = 3
# 前 3 格做"去数字"装备识别: 匹配前把 target 与 template 的左上角数量角标区涂成中性色,
# 消除"同件消耗品、角标数字不同"造成的匹配分差异; OCR 只作数量补充。见 _mask_badge。
BADGE_MASK_ENABLED = False
BADGE_MASK_MAX_SLOT = 3
ICON_THRESHOLD = 0.68         # 图标库匹配阈值 (blur+细尺度下真实命中 ≥0.84, 库外未知 ≤0.58)
# 溢出列 (col2, x>105) 单独提高门槛: 该 x 带与**羁绊图标列**重叠, 羁绊图标带铜色六边形边框,
# 亮度 (VALUE_MIN) 与边框 (SLOT_FRAME_RATIO_MIN) 两道闸门都可能放它进来, 而纹章类装备图标
# 与羁绊图标同形 → 低分错配。实机实测 (2-7 帧): 羁绊图标 (144,149) 被判成槽位并匹配成
# "护卫纹章" 0.705, 而真溢出列装备实测 0.865-0.921 (负极斗篷/锁子甲/无用大棒/主宰纹章/魔女纹章),
# 故 0.82 可干净分开两者 (真命中仍按 ICON_THRESHOLD 那档的 0.84+ 标准)。
ICON_THRESHOLD_COL2 = 0.82
ICON_INSET = 5                # 消耗品匹配的内缩 px, 跳过铜色边框再与模板比对
# 库图标是满幅裁切 (徽章占 ~0.76), 游戏内渲染的图标更小 (~0.73) 且内容在槽内
# 位置有偏移 (实测护卫纹章偏移 ~5px, 同尺寸模板滑动余量不足只有 0.39 分),
# 因此模板按绝对尺度缩小后在整槽滑窗取峰值 (与 board.extract_bench_items 同思路);
# 低分辨率样本放大后发虚, 3x3 高斯模糊两侧再比对可拉回 ~0.1 分 (0.66→0.84)
ICON_SCALES = tuple(round(0.60 + 0.025 * i, 3) for i in range(15))  # 0.60-0.95, 方形模板
ICON_MAX_TEMPLATE = 56        # 模板边长上限 px (搜索窗为整个凹槽 61x66, 留滑动余量)
ICON_STD_SIDE = 64            # 图标库统一边长: 库图实际混杂 64(142张)/72/78/96/128/256,
                              # 不归一化时超长图标会被 ICON_MAX_TEMPLATE 逐尺度 continue 掉
                              # (实测 128/256px 的 6 件**一个模板都不剩**, 永远匹配不到),
                              # 78px 的还会因 78*0.775>56 连粗筛池都进不去 → 读入时统一缩到 64。
ICON_BLUR_KSIZE = (3, 3)      # 匹配前对搜索窗与模板同时做的高斯模糊核 (None=不模糊)
# 排序预筛 (用户思路): 先用单一粗尺度给全库打分, 再**按分降序**做多尺度匹配 —— 只排序不淘汰,
# 召回与全库扫描完全一致, 但最像的图标排在前面, ICON_EARLY_EXIT 能更早命中 → 省时间。
# 这里曾有"粗尺度 top12 ∪ 色相 top20"的**过滤**式预筛 + ICON_RESCAN_FLOOR 复扫门槛,
# 实测会丢真命中 (阿尔法印记 32544 全库 0.802, 预筛只给 0.570 又因低于门槛放弃复扫 →
# 直接返回错答案), 已删除: 准确率不可接受的优化一律不要。
ICON_COARSE_SCALE = 0.775     # 粗尺度 (scales 中位; 64px 库图 → 50px 模板, 全库 ~150 次滑窗 ~12ms)
ICON_EARLY_EXIT = 0.90        # 滑窗早停分: 分数已足够高就返回 (不减召回, 只省时间;
                              # 真命中实测 0.84+, 库外未知 ≤0.58)
ICON_ROI_DARK_RATIO = 0.05    # ROI 内部亮像素占比低于此值 → 空槽/暗占位, 跳过匹配
                              # (实测空槽内部亮占比 0.00-0.02, 有物品 ≥0.18)
# ---- 空槽判据: 已撤销 (2026-10-31, 血泪教训) ----
# 曾经试过两条"先判空槽、不进精匹"的捷径, **都会把真装备判成空槽**, 已全部删除:
#   (a) 内圈 Laplacian 均值 (EMPTY_SLOT_EDGE=12): 在 8 张 1920x1080 真截图上标的
#       "空槽 0.55~6.21 / 有货 19.17~46.01" 看似两段隔 3 倍, 但那是那几张图里真装备都
#       碰巧是亮色图标的巧合。扩到仓库全部样例图 42 个槽位重标:
#           有货 36 槽 norm 1.29 ~ 48.22 (min = 锁子甲 1.29 / 巨人腰带 1.58)
#           空槽 6 槽  norm 0.72 ~ 21.59
#       **完全重叠** —— 深灰色散件 (锁子甲/巨人腰带) 的高频能量比空槽还低。
#   (b) 粗分线 (ICON_COARSE_GATE=0.55, 粗筛 top1 低于它就不进精匹): 同样误伤,
#       实测真命中槽位的粗分 top1 可以是 **0.000** (装备示例图 slot8 锁子甲)、
#       0.348 (image_2_a slot3 巨人腰带)、0.437 (装备示例图 slot6 巨人腰带)、
#       0.496 (奕子详情__2星 slot2 护卫纹章, 精分 0.840 明明过线), 而空槽能到 0.357
#       —— 两段也重叠, 判据不成立 (当年 [过滤式预筛丢真命中] 的同一个坑)。
# 现在唯一可靠的"这格没东西"证据仍然是**全库扫完都没过阈值**, 只能用不损召回的手段提速:
# 粗筛排序 + 分批递进 + 早停 (见 ICON_BATCH / ICON_STOP_SCORE)。
# ---- 分批递进提速 (2026-10-31 实测) ----
# 为什么需要: 装备栏 8 个槽位逐个跑全库多尺度精匹 ~306ms/槽, 整栏 1795ms, 一次给装
# 前后各认一次 → 3.6s (单动作 4.07s 里占 88%)。而真正需要的只是"这一格是什么"。
# 实测 4 张真截图 43 个有货槽位: 真装备的粗筛排序**第 0 位就命中** (23/23) → 排序够准,
# 第一批 32 个扫完 (77ms) 就能定; 空槽则必须扫完全库才能确认 (但 prep 阶段
# `detect_slots` 只画有货的槽, 空槽根本不会进来)。
# 「分批递进」而不是「只取前 N 个」: 前 N 个没命中就继续下一批, **直到全库扫完**,
# 召回与全库扫描完全等价 (历史教训见上面 ICON_COARSE_SCALE 那段: 过滤式预筛会丢真命中)。
ICON_BATCH = 32               # 分批递进: 每批精匹多少个图标 (粗筛排序后逐批推进)
ICON_STOP_SCORE = 0.75        # 分批早停线: 本批已出现 ≥ 此分的命中就停.
                              # 2026-11-01 由 0.82 下调到 0.75, 依据离线调研 (31 帧 461 格 + 16 帧 132 格):
                              #   排除「光明版/普通版同图」这一对之后, 最强"错候选"只有 0.703 (p95 0.688),
                              #   任何 0.70~0.85 的线实测都是 0 格误停; 而真命中里 top1<0.82 的占 20.5%
                              #   (深色散件如负极斗篷 0.788 永远过不了 0.82 → 每格扫完整库 ~280ms)。
                              #   降到 0.75 后慢格比例 20.5% → 7.6%, 且留 0.047 余量给"将来出现更强错候选"。
                              # 早停只影响"要不要继续扫", 不影响召回 (不达标就扫完, 结果恒等于全库扫描)。

# 逐槽并行匹配 (2026-11-01, 用户要求): 每个槽的识别彼此独立, 结果与串行**完全一致**
# (已实测 10 槽逐槽一致); cv2.matchTemplate 在 C 层释放 GIL, 数量角标 OCR 又是网络 I/O,
# 所以线程池能真并行。实测整栏 995ms -> 394ms (2.5x, 12 逻辑核); 剩下的瓶颈是**最慢那一格**
# (深色散件分低过不了早停线, 要全库扫 ~280ms), 因此并行与"改早停判据/内存查表"要一起做。
ICON_PARALLEL = True          # 关掉即退回逐槽串行 (排错/对比用)
ICON_MATCH_WORKERS = 8        # 并行线程数上限 (实际取 min(槽数, 此值))
_COUNT_OCR_LOCK = threading.Lock()   # 串行化数量角标 OCR (并发打 OCR 服务会把它打崩, 见 _read_count)
# 数量角标「数字模板」缓存: 模板库 (懒加载) + 已识别角标的哈希缓存 (同一角标跨帧反复出现, 命中即 0 开销)
_COUNT_DIGIT_TPL: dict[int, np.ndarray] | None = None
_COUNT_TPL_CACHE: dict[bytes, int | None] = {}
_COUNT_TPL_LOCK = threading.Lock()

# ---- 常量网格定位 (2026-11-01, 用户要求) -------------------------------------------
# 背景: 旧 detect_slots 用"亮度 V>VALUE_MIN(105) + 连通域"找槽, 实测 160 帧里 67 帧整体失败:
#   - 4 帧整条带被海克斯强化说明面板压暗 (Vmax=102 < 105) → mask 全空;
#   - 63 帧亮背景(选秀地图/加载/战斗特效) → 亮背景也被选进来, 连通域并成 w=164(整宽)h=283~775
#     (整高) 的巨型 blob → 被 SLOT_MAX_W/H 否掉。**装备图标其实一直都在**。
# 装备栏本来就是固定网格: 597 张正常帧统计 → 主列 x=23 / 溢出列 x=110 / 首格 top y=119 /
# 槽 66x66 / 顶点间距 78 (众数占比 65~100%)。故新路径**按常量网格生成候选格**, 再用"图标匹配
# 得分"判该格有没有货(与 extract_equipment 同一套判定, 含亮度归一化), 每列遇到连续空格即停。
# 网格路径拿不到任何有货格时才回退旧的连通域实现(旧路径代码保持原样, 行为不变)。
EQUIP_GRID_ENABLED = True        # False = 完全退回旧的亮度连通域定位(排错/对比用)
EQUIP_GRID_COL1_X = 23           # 主列槽左边缘 x
EQUIP_GRID_COL2_X = 110          # 溢出列槽左边缘 x (与羁绊图标列重叠区之外)
EQUIP_GRID_TOP_Y = 119           # 首格上边缘 y
EQUIP_GRID_W = 66                # 槽宽
EQUIP_GRID_H = 66                # 槽高
EQUIP_GRID_PITCH = 78            # 同列相邻槽顶点间距
EQUIP_GRID_MAX_PER_COL = 10      # 单列最多几个槽 (条带到 890, 实际最多 10)
EQUIP_GRID_EMPTY_TOLERANCE = 1   # 连续几个空格才判该列到底 (容忍中间偶发单格认不出)

SLOT_STD_H = 66               # 标准槽高中位 px (实测 61-73, 中位 66)
SLOT_STD_PITCH = 77           # 标准槽顶点间距 px (实测装备栏 y 差 77-78)
SLOT_MAX_W = 100              # 单槽宽度上限 px (实测 61-73; 超过为巨型假槽)
SLOT_MAX_H = 110              # 单槽高度上限 px (实测 61-73; 两槽粘连 h≈143 走拆分,
                              # 拆不开的巨型框走网格重建)
GRID_PITCH_RANGE = (60, 90)   # 可信槽顶点间距范围 (实测 77-78; 界外视为不可信节奏)
SPLIT_NO_RHYTHM_MAX = 150     # 无节奏拆分参照: 单框高度超过此值视为粘连巨型框/遮挡
OBSCURED_HUG_PX = 10          # 巨型框顶部贴条带上缘 ≤此 px → 整列被覆盖层遮挡, 按无槽处理
# 详情卡下方 3 个装备槽: 图标比装备栏大 (槽框 90x90, 图标约 80px), 用独立尺度表;
# 空槽是暗色马甲占位且无图标内容, 库内最高误分 0.714, 阈值 0.80 隔开 (真装备命中 ≥0.93)
DETAIL_EQUIP_SCALES = tuple(round(0.75 + 0.05 * i, 3) for i in range(11))  # 0.75-1.25
DETAIL_ICON_MAX = 84          # 详情槽模板边长上限 px (搜索窗 90x90, 留滑动余量)
DETAIL_EQUIP_THRESHOLD = 0.80
PANEL_BG = (28, 30, 42)       # 凹槽内衬底色 (深蓝黑), 用于 RGBA 图标合成

# items.json type2 -> 展示类型
KIND_NAMES = {"part": "散件", "daoju": "消耗品", "remove": "消耗品", "guangming": "光明成装",
              "zhuanzhi": "纹章", "zhuanzhiJgg": "纹章", "zhuanzhi2": "纹章", "common": "成装",
              "aoen": "成装", "other": "成装"}

_ICON_LIB: dict | None = None   # {equipId: (ICON_STD_SIDE, ICON_STD_SIDE, 3) uint8 合成底色后的 RGB}
_ITEMS_META: dict | None = None # {equipId: 详情 dict}
_ICON_SCALED: dict | None = None # {(scales, max_px): {equipId: [多尺度方形模板]}}
_ICON_COARSE: dict | None = None # {(scale, max_px): {equipId: 单一粗尺度模板}} (排序用)
_ICON_SCALED_BADGE: dict | None = None  # 同上, 但模板已做 _mask_badge (角标区蒙色), 供前3格去数字匹配
_ICON_COARSE_BADGE: dict | None = None  # 同上, 但模板已做 _mask_badge


def _normalize(img: Image.Image) -> np.ndarray:
    if img.size != (1920, 1080):
        img = img.resize((1920, 1080), Image.LANCZOS)
    return np.asarray(img.convert("RGB"))


def _median(values: list) -> float:
    """中位数 (列内不足两个样本时返回 0, 调用方自己判断)."""
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _snap_column(slots: list[tuple], mh: int | None, pitch: int | None) -> list[tuple]:
    """把列内槽位吸附到规则网格: (y0 + k*pitch, 高 mh), 消除 blob 上下缘漂移.

    为什么必须做 (用户口径: 准确率优先): 同一件装备所在的检出框高度实测会在 64-87px 之间漂
    (暗色图标亮区连通到相邻槽的铜框), 而下游 `_normalize_slots` 只按**框中心**缩回中位尺寸
    → 87px 的框缩成 66px 后整体下移 ~10px, ROI 切掉图标上半、混进下一格, 图标库分数从
    0.89/0.74/0.70 掉到 0.60/0.54/0.45 → 三格全部漏检。装备栏本身是等距网格 (实测 pitch 77-78),
    所以这里直接按网格重建: 取高度最接近中位高的框做参考, 拟合网格原点时对各框残差取**中位数**
    (单个异常框带不偏整列), 再把每框对齐成 (y0 + k*pitch, 高 mh)。

    间距修正: 列内只有 2-3 框时相邻差的中位数会被 ±5px blob 噪声带偏 (实测溢出列 73/82 → 中位 82,
    比真实 77.5 大 4.5px, 足以把首格推出条带上界而丢槽), 故框数 <4 时按"整列跨度 / 取整格数"
    重算间距 (155/2 = 77.5); 框数 ≥4 时中位数已足够可信, 保持原值不动。
    """
    if len(slots) < 2 or not mh or not pitch or pitch <= 0:
        return slots
    tops = sorted(b[1] for b in slots)
    span = tops[-1] - tops[0]
    if len(slots) < 4 and span > 0:
        pitch = span / max(1, round(span / pitch))
    ref = min(slots, key=lambda b: abs((b[3] - b[1]) - mh))
    y0 = int(_median([b[1] - round((b[1] - ref[1]) / pitch) * pitch for b in slots]))
    out, seen = [], set()
    for b in sorted(slots, key=lambda b: b[1]):
        k = round((b[1] - y0) / pitch)
        y = y0 + k * int(round(pitch))
        if k in seen or not EQUIP_STRIP[1] <= y <= EQUIP_STRIP[3] - mh:
            continue
        seen.add(k)
        out.append((b[0], y, b[2], y + mh))
    return out


def detect_slots(img: Image.Image) -> list[tuple]:
    """左边缘装备条带内定位装备凹槽, 返回按 y 排序的 (x1, y1, x2, y2) 列表.

    判据用纯亮度 V (凹槽间隙是暗绿色但饱和度高, 饱和度判据会把背景全连起来):
    图标内容与铜框 V>110, 间隙/内衬 V<100.
    """
    arr = _normalize(img)
    if EQUIP_GRID_ENABLED:
        _ents = _slots_by_grid(arr, stop=ICON_STOP_SCORE, batch=ICON_BATCH)
        if _ents is not None:
            return [e["rect"] for e in _ents]
    x1, y1, x2, y2 = EQUIP_STRIP
    roi = arr[y1:y2, x1:x2]
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    mask = (hsv[:, :, 2] > VALUE_MIN).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))  # 图标内容可能分裂
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    slots = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w >= SLOT_MIN_W and h >= SLOT_MIN_H and area >= SLOT_MIN_AREA:
            slots.append((x1 + int(x), y1 + int(y), x1 + int(x + w), y1 + int(y + h)))
    slots.sort(key=lambda b: b[1])
    # 分列处理: 列1 x1<=EQUIP_COL1_MAX_X (主列), 列2 x1>EQUIP_COL2_MIN_X (溢出列);
    # 两者之间是聊天按钮等非槽位. 判据坐标在 recognize/regions.py, 与 extract_equipment 同源.
    # 补插/拆分节奏按列独立 (两列的 pitch/槽高中位数分别计算);
    # 铜框过滤剔除羁绊图标误检 (羁绊边界亮占比~0.02, 装备槽有铜框 0.56+)
    col1 = [s for s in slots if s[0] <= regions.EQUIP_COL1_MAX_X and _has_frame(arr, s)]
    col2 = [s for s in slots if s[0] > regions.EQUIP_COL2_MIN_X and _has_frame(arr, s)]
    c1 = _clean_column(arr, col1)
    mh1, pitch1 = _rhythm(c1)
    c1 = _snap_column(c1, mh1, pitch1)
    # 列2 可能只检出 1 个粘连框 (不足两框无法自算节奏): 借列1 的槽高/间距拆分与吸附
    c2 = _clean_column(arr, col2, mh1, pitch1)
    mh2, pitch2 = _rhythm(c2)
    c2 = _snap_column(c2, mh2 or mh1, pitch2 or pitch1)
    return c1 + c2


def _clean_column(arr: np.ndarray, boxes: list[tuple],
                  mh: int | None = None, pitch: int | None = None) -> list[tuple]:
    """列内槽位整理: 剔除巨型假槽后按节奏拆分/补插.

    巨型假槽 (宽>SLOT_MAX_W 或高>SLOT_MAX_H): 特效/面板把多个槽连成重叠大框
    (实测 154x174、顶点间距仅 47px), 会把 _normalize_slots 的中位数、拆分节奏
    与匹配结果全部带歪 (同一图标在相邻重叠窗重复命中成鬼影, 且每槽全库滑窗
    ~1s). 处理: 丢弃巨型框, 以可信槽为锚按标准节奏向条带两端铺网格, 铜框判据
    (_has_frame) 逐格把关, 补回被吞掉的真实槽位; 无可信槽时整列按遮挡处理.
    """
    plaus = [b for b in boxes
             if b[2] - b[0] <= SLOT_MAX_W and SLOT_MIN_H <= b[3] - b[1] <= SLOT_MAX_H]
    if len(plaus) != len(boxes):  # 存在巨型假槽: 网格重建
        if not plaus:
            return []
        x1 = sorted(b[0] for b in plaus)[len(plaus) // 2]
        x2 = sorted(b[2] for b in plaus)[len(plaus) // 2]
        hs = sorted(b[3] - b[1] for b in plaus)
        mh = hs[len(hs) // 2]
        tops = sorted(b[1] for b in plaus)
        diffs = sorted(d for d in (b - a for a, b in zip(tops, tops[1:]))
                       if GRID_PITCH_RANGE[0] <= d <= GRID_PITCH_RANGE[1])
        pitch = diffs[len(diffs) // 2] if diffs else SLOT_STD_PITCH
        taken = set(tops)
        grid = []
        for direction in (-1, 1):
            y, steps = tops[0], 0
            while steps < MAX_FILL_SLOTS:
                y += direction * pitch
                steps += 1
                if not EQUIP_STRIP[1] <= y <= EQUIP_STRIP[3] - mh:
                    continue
                if any(abs(y - t) <= 10 for t in taken):
                    continue
                if _has_frame(arr, (x1, y, x2, y + mh)):
                    grid.append((x1, y, x2, y + mh))
        return sorted(plaus + grid, key=lambda b: b[1])
    return _fill_missing(_split_tall(boxes, *_rhythm(boxes) if len(boxes) >= 2 else (mh, pitch)))


def _has_frame(arr: np.ndarray, rect: tuple) -> bool:
    """装备槽四周边界的铜色亮框判据; 羁绊图标无框 (实测亮占比 0.02 vs 真槽 0.56+)."""
    x1, y1, x2, y2 = rect
    v = cv2.cvtColor(arr[y1:y2, x1:x2], cv2.COLOR_RGB2HSV)[:, :, 2]
    m = v > VALUE_MIN
    edge = (m[0].mean() + m[-1].mean() + m[:, 0].mean() + m[:, -1].mean()) / 4
    return edge >= SLOT_FRAME_RATIO_MIN


def _rhythm(slots: list[tuple]) -> tuple[int | None, int | None]:
    """列内槽高/顶点间距的中位数; 框数不足两个时返回 (None, None)."""
    if len(slots) < 2:
        return None, None
    hs = sorted(s[3] - s[1] for s in slots)
    tops = sorted(s[1] for s in slots)
    diffs = sorted(b - a for a, b in zip(tops, tops[1:]))
    return hs[len(hs) // 2], diffs[len(diffs) // 2]


def _split_tall(slots: list[tuple], mh: int | None = None,
                pitch: int | None = None) -> list[tuple]:
    """相邻凹槽溢出时会被 MORPH_CLOSE 连成竖长框 (实测 h=143): 按中位节奏拆回单槽.

    列内不足两框时无法自算节奏 (列2 可能只检出 1 个粘连框), 用另一列的
    节奏兜底; 两者皆缺时槽高量级的单框原样保留, 整条带巨型框按遮挡丢弃
    或按标准节奏 (SLOT_STD_H/PITCH) 拆分.
    """
    own_mh, own_pitch = _rhythm(slots)
    mh = own_mh if own_mh is not None else mh
    pitch = own_pitch if own_pitch is not None else pitch
    if mh is None or pitch is None or pitch <= 0:
        # 无节奏 (单框): 槽高量级 (<SPLIT_NO_RHYTHM_MAX) 原样保留; 巨型框两种情形:
        # 顶部贴条带上缘 → 整列被亮色面板/覆盖层淹没 (实测 154x883, 左侧弹窗时
        # 亮度掩码全连通), 槽位不可见, 按无槽处理; 否则按标准节奏拆分 (顶槽=y1).
        out = []
        for x1, y1, x2, y2 in slots:
            h = y2 - y1
            if h <= SPLIT_NO_RHYTHM_MAX:
                out.append((x1, y1, x2, y2))
            elif y1 - EQUIP_STRIP[1] <= OBSCURED_HUG_PX:
                continue
            else:
                k = min(MAX_FILL_SLOTS, max(2, round((h - SLOT_STD_H) / SLOT_STD_PITCH) + 1))
                for j in range(k):
                    out.append((x1, y1 + j * SLOT_STD_PITCH, x2,
                                min(y1 + j * SLOT_STD_PITCH + SLOT_STD_H, y2)))
        return out
    out = []
    for x1, y1, x2, y2 in slots:
        h = y2 - y1
        if h > mh * 1.4:
            # 按标准节奏对齐拆分 (顶槽=y1, 之后每 pitch 一槽, 槽高=mh):
            # h/k 均分会产生 ~6px 边界错位, ROI 混入相邻槽导致图标错配 (拳套→泪)
            k = max(2, round((h - mh) / pitch) + 1)
            for j in range(k):
                out.append((x1, y1 + j * pitch, x2, min(y1 + j * pitch + mh, y2)))
        else:
            out.append((x1, y1, x2, y2))
    return out


def _fill_missing(slots: list[tuple]) -> list[tuple]:
    """暗色图标 (如灰色拳套) 可能低于亮度阈值而漏检: 槽位等距排布,
    相邻 top 差≈标准间距的整数倍时按间距补插缺失槽.

    防御: 检出框粘连会把 min 相邻差污染成几像素, 用中位数取 pitch;
    只向"检出在期望下方"方向补插 (expect 单调递增, abs 判定遇上方偏离
    会永不收敛导致内存爆炸死机); 补插总数设上限兜底.
    """
    if len(slots) < 2:
        return slots
    tops = [s[1] for s in slots]
    diffs = sorted(b - a for a, b in zip(tops, tops[1:]))
    pitch = diffs[len(diffs) // 2]
    if pitch <= 0:
        return slots  # 检出框重叠/乱序: 检测异常, 原样返回不补插
    h = max(s[3] - s[1] for s in slots)
    x1, x2 = slots[0][0], slots[0][2]
    out, expect = [], slots[0][1]
    for s in slots:
        fills = 0
        while s[1] - expect > pitch * 0.3 and fills < MAX_FILL_SLOTS:
            out.append((x1, expect, x2, expect + h))
            expect += pitch
            fills += 1
        out.append(s)
        expect = s[1] + pitch
    return out


def _fit_icon(rgba: np.ndarray) -> np.ndarray:
    """库图标统一到 ICON_STD_SIDE × ICON_STD_SIDE (等比缩放 + 居中贴到 PANEL_BG 画布).

    为什么必须做: 库图边长实测 64/72/78/96/128/256 混杂, 而模板上限是 ICON_MAX_TEMPLATE=56,
    超过的图标会在 _scaled_library 里**每个尺度都被跳过** → 模板列表为空, 该装备永远匹配不到
    (幸运装备宝箱 32311/32312 = 128px, 毛绒大礼包 32546 = 256px 等 6 件实测为 0 个模板);
    78px 的阿尔法印记还会因 78*0.775>56 连粗筛池都进不去。统一到 64 后, 尺度表语义才一致。
    """
    h, w = rgba.shape[:2]
    if h == 0 or w == 0:
        return rgba
    scale = ICON_STD_SIDE / max(h, w)
    tw, th = max(1, round(w * scale)), max(1, round(h * scale))
    resized = cv2.resize(rgba, (tw, th), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((ICON_STD_SIDE, ICON_STD_SIDE, 4), dtype=np.uint8)
    canvas[:, :, :3] = PANEL_BG
    y0, x0 = (ICON_STD_SIDE - th) // 2, (ICON_STD_SIDE - tw) // 2
    canvas[y0:y0 + th, x0:x0 + tw] = resized
    return canvas


def _load_library() -> tuple[dict, dict]:
    """惰性加载图标库与装备详情, 返回 ({equipId: rgb数组}, {equipId: 详情dict})."""
    global _ICON_LIB, _ITEMS_META
    if _ICON_LIB is None:
        _ICON_LIB, _ITEMS_META = {}, {}
        if ITEMS_JSON.exists():
            data = json.loads(ITEMS_JSON.read_text(encoding="utf-8"))["items"]
            for it in data:
                _ITEMS_META[it["equipId"]] = it
        if ITEM_ICON_DIR.exists():
            for path in ITEM_ICON_DIR.glob("*.png"):
                try:
                    eid = int(path.stem)
                except ValueError:
                    continue
                rgba = _fit_icon(np.asarray(Image.open(path).convert("RGBA")))
                alpha = rgba[:, :, 3:4].astype(float) / 255.0
                rgb = rgba[:, :, :3].astype(float) * alpha + np.array(PANEL_BG) * (1 - alpha)
                _ICON_LIB[eid] = rgb.astype(np.uint8)
        if USE_GAME_TEMPLATES:
            _overlay_game_templates(_ICON_LIB, _ITEMS_META)
    return _ICON_LIB, _ITEMS_META


def _overlay_game_templates(lib: dict, meta: dict) -> int:
    """用**实机模板**覆盖同名原画模板 (实机优先、原画兜底), 返回覆盖的 equipId 数.

    只覆盖"实机库里确实有这件"的 equipId, 其余保持原画 —— 所以缺模板只是没提速, 不会认不出。
    名字取自文件名 (`名字.png` / `名字__变体.png` 取 `__` 前段), 通过 items.json 的 name→equipId
    反查 (同一名字可能对应多个 equipId, 例如幸运装备宝箱 32311/32312, 全部覆盖)。
    实机模板要求已是 ICON_STD_SIDE 见方的 RGB; 尺寸不符时按 ICON_STD_SIDE 缩放兜底。
    """
    global _GAME_TPL_OVERLAID
    if not GAME_TEMPLATE_DIR.exists():
        return 0
    by_name: dict[str, list[int]] = {}
    for eid, it in (meta or {}).items():
        nm = str((it or {}).get("name") or "").strip()
        if nm:
            by_name.setdefault(nm, []).append(eid)
    overlaid: set[int] = set()
    for path in sorted(GAME_TEMPLATE_DIR.glob("*.png")):
        name = path.stem.split("__")[0].strip()
        eids = by_name.get(name)
        if not eids:
            continue            # 名字对不上 items.json: 跳过 (不猜 equipId)
        try:
            rgb = np.asarray(Image.open(path).convert("RGB"))
        except Exception:       # noqa: BLE001 - 单张坏图不影响整库
            continue
        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.shape[0] < 8 or rgb.shape[1] < 8:
            continue
        if rgb.shape[0] != ICON_STD_SIDE or rgb.shape[1] != ICON_STD_SIDE:
            rgb = cv2.resize(rgb, (ICON_STD_SIDE, ICON_STD_SIDE), interpolation=cv2.INTER_AREA)
        for eid in eids:
            lib[eid] = rgb
            overlaid.add(eid)
    _GAME_TPL_OVERLAID = len(overlaid)
    if overlaid:
        print(f"[equip] 实机模板覆盖 {len(overlaid)} 个 equipId (实机优先/原画兜底), 目录={GAME_TEMPLATE_DIR}")
    return len(overlaid)


def _hue_hist(a: np.ndarray) -> np.ndarray:
    """内衬区域 36-bin 色相直方图 (V>60 有效像素, 归一化)."""
    if a.size == 0:  # 空 ROI (战斗干扰帧的越界假槽框): 零直方图, 相关性自然为 0
        return np.zeros(36, dtype=np.float32)
    hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV)
    m = (hsv[:, :, 2] > 60).astype(np.uint8)
    h = cv2.calcHist([hsv], [0], m, [36], [0, 36]).flatten()
    return (h / (h.sum() or 1)).astype(np.float32)


def _badge_crop(a: np.ndarray) -> np.ndarray:
    """剔除左上角数量角标区后的子图.

    角标有无随机 (拆卸器 0-7 个, 重铸器 0-2 个) 且为大块金黄纯色, 会单侧污染
    色相直方图 (重铸器带角标帧 corr 0.964→0.248)."""
    h, w = a.shape[:2]
    return a[int(h * BADGE_CROP):, int(w * BADGE_CROP):]


def _has_badge(a: np.ndarray) -> bool:
    """左上角数量角标检测: 角标是大块高亮金黄纯色 (H≈15-35, S>120, V>140, ≥50px).

    检测区取左上 1/4 象限; 金色系图标 (大剑/复制器) 可能误报, 但对称裁剪后
    这些对的色相 corr 反而更稳, 不影响判定."""
    h, w = a.shape[:2]
    quad = a[: h // 2, : w // 2]
    hsv = cv2.cvtColor(quad, cv2.COLOR_RGB2HSV)
    m = (hsv[:, :, 0] >= 15) & (hsv[:, :, 0] <= 35) & (hsv[:, :, 1] > 120) & (hsv[:, :, 2] > 140)
    return int(m.sum()) >= 50


def _mask_badge(a: np.ndarray) -> np.ndarray:
    """把左上角数量角标区涂成"中性纯色", 消除同件消耗品因角标数字不同带来的匹配分差异.

    用户口径 (2026-11-01 m01678): 前 3 格先"用颜色蒙上 target 和 template, 去除数字不同的
    干扰"再做装备识别。target 与 template 蒙成各自非角标区的中位色后, 该区域两侧都变成
    平坦纯色、不贡献相关性 —— 等价于把角标区从比对中屏蔽掉, 但保留了滑窗对齐 (不像
    `_badge_crop` 那样裁剪后改变尺寸/尺度)。空图/过小图原样返回。
    """
    if a.size == 0 or a.ndim != 3:
        return a
    h, w = a.shape[:2]
    bh, bw = int(h * BADGE_CROP), int(w * BADGE_CROP)
    if bh < 1 or bw < 1:
        return a
    out = a.copy()
    rest = a[bh:, bw:]
    src = rest.reshape(-1, a.shape[2]) if rest.size else a.reshape(-1, a.shape[2])
    fill = np.median(src, axis=0).astype(a.dtype)
    out[:bh, :bw] = fill
    return out


_TPLS: list[tuple[str, np.ndarray, np.ndarray, bool]] | None = None
# (名称, 模板RGB, 内衬区, 是否带角标); 名称取文件名 __ 前段, 支持 同名多变体模板


def _load_templates() -> list[tuple[str, np.ndarray, np.ndarray, bool]]:
    global _TPLS
    if _TPLS is None:
        # 2026-11-01: 逐槽并行后, 这里必须**先建好本地列表再一次性赋值给 _TPLS** ——
        # 旧写法 `_TPLS = []` 后再 append, 多线程首次同时进入时会有一个线程拿到"空列表",
        # `_match_consumable` 于是返回 (0, "") → 冷启动那**第一帧**的消耗品(拆卸器/重铸器)
        # 被漏识别成 None (实测 poll_0004 首跑 slot2 重铸器 None, 第 2 次起才 0.964)。
        if not CONSUMABLE_TEMPLATE_DIR.exists():
            _TPLS = []
        else:
            tpls: list[tuple[str, np.ndarray, np.ndarray, bool]] = []
            for path in sorted(CONSUMABLE_TEMPLATE_DIR.glob("*.png")):
                tpl = np.asarray(Image.open(path).convert("RGB"))
                if min(tpl.shape[0], tpl.shape[1]) <= ICON_INSET * 2:
                    continue
                t_in = tpl[ICON_INSET:-ICON_INSET, ICON_INSET:-ICON_INSET]
                tpls.append((path.stem.split("__")[0], tpl, t_in, _has_badge(t_in)))
            _TPLS = tpls          # 原子替换: 其它线程要么看到 None 要么看到完整列表
    return _TPLS


def _consumable_score(roi: np.ndarray) -> tuple[float, str, float]:
    """消耗品通道的三个量: `(色相 corr, 模板名, 像素 NCC)` —— 名字为 "" 表示未过双门槛。

    为什么把 ncc 也交出去 (2026-11-02, 用户 m00245/m00248 事故后): 旧版只把 `corr` 当主分,
    而 corr 对"同色系不同图标"几乎不分 —— 成装「海妖之怒」(蓝金) 撞「次级英雄复制器」模板
    拿到 corr **0.868**, 真消耗品「拆卸器」对自身模板是 **0.882**, 两个数根本拉不开;
    但像素 NCC 是 0.331 vs 0.455, 差别一眼可见。所以**裁决必须看 ncc**, corr 只当前筛。

    角标处理沿用旧口径 (任一侧带角标 -> 两侧对称剔除左上角), 否则同一件消耗品的
    角标数字会把 corr 从 0.964 拉到 0.248 (见 `_match_consumable` 历史注释)。
    """
    if not CONSUMABLE_TEMPLATE_DIR.exists():
        return 0.0, "", 0.0
    if roi.size == 0:  # 空 ROI (越界假槽框): 直接不匹配
        return 0.0, "", 0.0
    inner = roi[ICON_INSET:-ICON_INSET, ICON_INSET:-ICON_INSET]
    if inner.size == 0:
        return 0.0, "", 0.0
    roi_badge = _has_badge(inner)
    h_full = _hue_hist(inner)
    h_crop = _hue_hist(_badge_crop(inner))
    best = (0.0, "", 0.0)
    for name, tpl, t_in, t_badge in _load_templates():
        if roi_badge or t_badge:  # 任一侧带角标: 两侧对称剔除 (ROI 侧用裁剪直方图)
            corr = float(cv2.compareHist(h_crop, _hue_hist(_badge_crop(t_in)), cv2.HISTCMP_CORREL))
        else:
            corr = float(cv2.compareHist(h_full, _hue_hist(t_in), cv2.HISTCMP_CORREL))
        if corr < CONSUMABLE_HUE_THRESHOLD:
            continue
        stretched = cv2.resize(tpl, (roi.shape[1], roi.shape[0]))
        a = roi.astype(float) - roi.mean()
        b = stretched.astype(float) - stretched.mean()
        denom = np.sqrt((a * a).sum() * (b * b).sum())
        ncc = float((a * b).sum() / denom) if denom else 0.0
        if ncc >= CONSUMABLE_PIXEL_GATE and corr > best[0]:
            best = (round(corr, 3), name, round(ncc, 3))
    return best


def _match_consumable(roi: np.ndarray) -> tuple[float, str]:
    """消耗品模板匹配: 角标感知的色相直方图相关性为主分 + 像素 NCC 门槛.

    数量角标有无随机且为大块金黄纯色, 会单侧污染色相直方图 (重铸器带角标帧
    corr 0.964→0.248): 任一侧检出角标时两侧对称剔除左上角角标区再比对 (视野
    一致), 均无角标保持全图比对 (盲目剔除会误伤无角标对, 实测 0.91→0.64).
    色相分布对缩放/模糊/光效鲁棒; 用户模板裁剪自不同分辨率截图, 像素级
    matchTemplate 上限仅 0.4-0.7 且与交叉分不可分, 故只作门槛兜底防同色相
    异图标误报与暗色图标空直方图 (corr 恒 1.0) 误报.
    返回 (最高分=色相相关性, 名称); 无模板返回 (0, "")。

    ⚠️ 新代码请用 `_consumable_score` (还带 ncc)。本函数只为兼容旧调用/单测保留。
    """
    corr, name, _ncc = _consumable_score(roi)
    return corr, name


def _coarse_library(scale: float = ICON_COARSE_SCALE,
                    max_px: int = ICON_MAX_TEMPLATE) -> dict[int, np.ndarray]:
    """惰性构建单一粗尺度模板缓存 (给全库**排序**用, 不淘汰任何图标)."""
    global _ICON_COARSE
    lib, _ = _load_library()
    if _ICON_COARSE is None:
        _ICON_COARSE = {}
    key = (scale, max_px)
    if key not in _ICON_COARSE:
        coarse = {}
        for eid, icon in lib.items():
            tw, th = round(icon.shape[1] * scale), round(icon.shape[0] * scale)
            if tw > max_px or th > max_px:
                continue
            t = cv2.resize(icon, (tw, th))
            if ICON_BLUR_KSIZE:
                t = cv2.GaussianBlur(t, ICON_BLUR_KSIZE, 0)
            coarse[eid] = t
        _ICON_COARSE[key] = coarse
    return _ICON_COARSE[key]


def _coarse_rank(roi: np.ndarray) -> tuple[list[int], float]:
    """按单一粗尺度滑窗分**降序**返回 (全库 equipId 列表, 最高粗分) —— 只排序, 不淘汰.

    用户思路 (准确率优先的前提下提速): 旧实现是"过滤"式预筛 (top12 ∪ 色相 top20), 候选集里
    没有真命中就直接给错答案 (阿尔法印记实测 0.802 → 0.570); 改成**排序**后全库召回不变,
    只是让最像的排在前面, 配合 ICON_EARLY_EXIT 提前返回 → 高置信格子省下大部分滑窗。

    返回的粗分最高值**只做信息**, 不能当"空槽门槛"用 —— 实测真命中槽位的粗分 top1 可以是
    0.000 (锁子甲) / 0.348 (腰带) / 0.496 (护卫纹章), 而空槽能到 0.357, 两段重叠 (见文件头
    「空槽判据已撤销」)。这里只输出排序, 谁都不许拿它淘汰候选。
    """
    r = cv2.GaussianBlur(roi, ICON_BLUR_KSIZE, 0) if ICON_BLUR_KSIZE else roi
    ih, iw = r.shape[:2]
    scored = []
    for eid, t in _coarse_library().items():
        if t.shape[0] > ih or t.shape[1] > iw:
            continue
        _, score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(r, t, cv2.TM_CCOEFF_NORMED))
        scored.append((score, eid))
    scored.sort(reverse=True)
    top = float(scored[0][0]) if scored else 0.0
    return [eid for _, eid in scored], top


def _coarse_order(roi: np.ndarray) -> list[int]:
    """按粗尺度滑窗分降序返回全库 equipId (只排序不淘汰; 见 `_coarse_rank`)。"""
    return _coarse_rank(roi)[0]


def _scaled_library(scales=ICON_SCALES,
                    max_px: int = ICON_MAX_TEMPLATE) -> dict[int, list[np.ndarray]]:
    """惰性构建多尺度模板缓存: 按 (scales, max_px) 分键, 详情页大图标复用同一套库.

    库图标读入时已统一到 ICON_STD_SIDE (见 _fit_icon), 方形缩放保持比例;
    模板边长不超过 max_px 以保证在搜索窗内有足够滑动余量; 缓存内已按 ICON_BLUR_KSIZE 预模糊.
    """
    global _ICON_SCALED
    lib, _ = _load_library()
    if _ICON_SCALED is None:
        _ICON_SCALED = {}
    if (scales, max_px) not in _ICON_SCALED:
        scaled: dict[int, list[np.ndarray]] = {}
        for eid, icon in lib.items():
            h, w = icon.shape[:2]
            tmpls = []
            for s in scales:
                tw, th = round(w * s), round(h * s)
                if tw > max_px or th > max_px:
                    continue
                t = cv2.resize(icon, (tw, th))
                if ICON_BLUR_KSIZE:
                    t = cv2.GaussianBlur(t, ICON_BLUR_KSIZE, 0)
                tmpls.append(t)
            scaled[eid] = tmpls
        _ICON_SCALED[(scales, max_px)] = scaled
    return _ICON_SCALED[(scales, max_px)]


def _scaled_library_badge(scales=ICON_SCALES,
                          max_px: int = ICON_MAX_TEMPLATE) -> dict[int, list[np.ndarray]]:
    """`_scaled_library` 的"去数字"版本: 每个模板先 `_mask_badge` 再分尺度缩放/模糊。

    只给前 3 格 (可能含消耗品的格子) 用。蒙色必须在缩放**前**做 (在 64x64 标准图上按
    `BADGE_CROP` 比例定位角标区), 与 target 侧同比例, 保证两侧角标区完全对齐。
    """
    global _ICON_SCALED_BADGE
    lib, _ = _load_library()
    if _ICON_SCALED_BADGE is None:
        _ICON_SCALED_BADGE = {}
    if (scales, max_px) not in _ICON_SCALED_BADGE:
        scaled: dict[int, list[np.ndarray]] = {}
        for eid, icon in lib.items():
            masked = _mask_badge(icon)
            h, w = masked.shape[:2]
            tmpls = []
            for s in scales:
                tw, th = round(w * s), round(h * s)
                if tw > max_px or th > max_px:
                    continue
                t = cv2.resize(masked, (tw, th))
                if ICON_BLUR_KSIZE:
                    t = cv2.GaussianBlur(t, ICON_BLUR_KSIZE, 0)
                tmpls.append(t)
            scaled[eid] = tmpls
        _ICON_SCALED_BADGE[(scales, max_px)] = scaled
    return _ICON_SCALED_BADGE[(scales, max_px)]


def _coarse_library_badge(scale: float = ICON_COARSE_SCALE,
                          max_px: int = ICON_MAX_TEMPLATE) -> dict[int, np.ndarray]:
    """`_coarse_library` 的"去数字"版本 (模板先 `_mask_badge`), 供前 3 格排序用。"""
    global _ICON_COARSE_BADGE
    lib, _ = _load_library()
    if _ICON_COARSE_BADGE is None:
        _ICON_COARSE_BADGE = {}
    key = (scale, max_px)
    if key not in _ICON_COARSE_BADGE:
        coarse = {}
        for eid, icon in lib.items():
            masked = _mask_badge(icon)
            tw, th = round(masked.shape[1] * scale), round(masked.shape[0] * scale)
            if tw > max_px or th > max_px:
                continue
            t = cv2.resize(masked, (tw, th))
            if ICON_BLUR_KSIZE:
                t = cv2.GaussianBlur(t, ICON_BLUR_KSIZE, 0)
            coarse[eid] = t
        _ICON_COARSE_BADGE[key] = coarse
    return _ICON_COARSE_BADGE[key]


def _coarse_rank_badge(roi: np.ndarray) -> tuple[list[int], float]:
    """前 3 格用: 先把 roi 蒙角标, 再按去数字粗尺度库排序 (只排序不淘汰)。"""
    r = _mask_badge(roi)
    r = cv2.GaussianBlur(r, ICON_BLUR_KSIZE, 0) if ICON_BLUR_KSIZE else r
    ih, iw = r.shape[:2]
    scored = []
    for eid, t in _coarse_library_badge().items():
        if t.shape[0] > ih or t.shape[1] > iw:
            continue
        _, score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(r, t, cv2.TM_CCOEFF_NORMED))
        scored.append((score, eid))
    scored.sort(reverse=True)
    top = float(scored[0][0]) if scored else 0.0
    return [eid for _, eid in scored], top


def _roi_is_dark(roi: np.ndarray) -> bool:
    """槽内部几乎无亮像素 → 空槽/暗占位, 无需进匹配 (阈值见 ICON_ROI_DARK_RATIO)."""
    h, w = roi.shape[:2]
    inner = roi[h // 8:h - h // 8 or None, w // 8:w - w // 8 or None]
    if inner.size == 0:
        return True
    v = cv2.cvtColor(inner, cv2.COLOR_RGB2HSV)[:, :, 2]
    return float((v > VALUE_MIN).mean()) < ICON_ROI_DARK_RATIO


def _brightness_norm(roi: np.ndarray) -> np.ndarray:
    """逐槽亮度/对比度归一化 (z-score): 只改亮度/对比, 不动色相与相对饱和度。

    为什么加 (2026-11-01 用户要求, 实测): 装备图标模板是正常亮度下截的, 一旦整屏被压暗
    (海克斯强化说明面板遮罩, Vmax≈102) 或极亮, 直接比对的相似度整体下滑 → 漏认。把 ROI 拉成
    mean=128 / std=48 后, 真实压暗帧 5 件装备从 1/5 恢复到 5/5 (得分 0.857~0.903), 而 3 张正常
    帧 34 槽加不加归一化结果 0 差异 —— 对正常画面是恒等变换级别的安全改动。
    """
    if roi.size == 0:
        return roi
    x = roi.astype(np.float32)
    m = float(x.mean())
    sd = float(x.std())
    if sd < 1e-3:            # 纯色(空槽/纯黑) 不处理, 否则放大噪声
        return roi
    y = (x - m) / sd * 48.0 + 128.0
    return np.clip(y, 0, 255).astype(np.uint8)


def _slot_is_empty(roi: np.ndarray) -> bool:
    """已废弃, 恒返回 False (保留符号只为不打断外部引用; 2026-10-31 撤销)。

    为什么撤: 曾经用「内圈 Laplacian 均值 < 12」判空槽, 在 8 张 1920x1080 真截图上标定
    (空槽 0.55~6.21 / 有货 19.17~46.01, 看似隔 3 倍) 就上线了。扩到仓库全部样例图 42 个
    槽位重标后发现**两段完全重叠**:

        有货 36 槽: 1.29 ~ 48.22  (最小两个恰是深灰色散件: 锁子甲 1.29 / 巨人腰带 1.58)
        空槽  6 槽: 0.72 ~ 21.59

    即"深灰图标的纹理能量比空槽还低", 任何阈值都会把真装备判成空槽 → 漏采 + 后校验以为
    "库存没动" → 假失败。数据正确性 > 速度, 这条捷径整条删掉。

    另外「空槽做成模板入库」也不可行: 空槽半透明透出背后战场, 跨帧 TM_CCOEFF_NORMED
    只有 **-0.22~-0.13** (毫无相关性), 同一帧内多个空槽才是 1.000, 没有可复用模板。

    现在判"这格没东西"的唯一可靠证据 = 全库扫完都没过阈值 (见 `_match_library`)。
    """
    return False


def _match_library(roi: np.ndarray, scales=ICON_SCALES,
                   max_px: int = ICON_MAX_TEMPLATE, *,
                   coarse: tuple[list[int], float] | None = None,
                   stop_score: float | None = None,
                   batch: int = 0,
                   mask_badge: bool = False) -> tuple[float, int | None]:
    """整个凹槽矩形与图标库多尺度滑窗比对, 返回 (最高分, equipId).

    用户口径 (准确率优先): **全库 150 图标全部参与匹配, 不做任何淘汰**。提速只用不损召回的手段:
      1. **排序预筛** (`_coarse_rank`): 先按单一粗尺度给全库打分, 多尺度扫描按分降序进行 ——
         最像的排在前面, 早停更早命中 (粗筛只影响顺序, 不影响谁能被扫到);
      2. `ICON_EARLY_EXIT`: 分数 ≥0.90 立即返回 (真命中实测 0.84+, 库外未知 ≤0.58);
      3. **分批递进** (`batch`>0): 按粗筛顺序每 batch 个精匹一批, 本批已出现 ≥ stop_score
         的命中就停; 没命中就取下一批, **直到全库扫完** —— 与全库扫描结果完全等价,
         只是绝大多数情况第 1 批第 1 个就中 (实测 23/23 真装备如此)。
    旧实现是**过滤**式预筛 (粗尺度 top12 ∪ 色相 top20 + ICON_RESCAN_FLOOR 复扫门槛), 候选集里
    没有真命中就直接返回错答案: 阿尔法印记(32544) 全库扫 0.802, 预筛只给 0.570 且因低于门槛
    放弃复扫 —— 这类用准确率换时间的做法已全部删除。第 3 条不是过滤: 它是"分批推进直到全库扫完",
    每一件图标都还有机会被扫到, 只是命中的那一批之后就停。

    coarse: 调用方已算好的 `_coarse_rank(roi)` 结果 (省一次 27ms 粗筛); 空槽门槛由调用方
        在自己的层上判 (见 `extract_equipment` 的 gated 标记)。

    游戏内图标渲染尺寸比库裁切小且内容在槽内有偏移 (固定同尺寸比对对护卫纹章只有 0.39 分),
    故模板缩小到 scales 后在整槽滑窗对齐; 搜索窗与模板同做高斯模糊以容忍低分辨率样本的插值发虚。
    """
    if roi.size == 0 or roi.shape[0] > 200 or roi.shape[1] > 200:
        # 保险丝: 正常槽 ~66px / 详情槽 90px, 超大 ROI 必是病态帧残留,
        # 全库滑窗在其上要 ~1s 且必然误报, 直接按未命中处理
        return 0.0, None
    if _roi_is_dark(roi):
        return 0.0, None
    if mask_badge:
        # 前 3 格"去数字": target 与模板两侧都把角标区蒙成中性色 (见 _mask_badge)
        roi = _mask_badge(roi)
    r = cv2.GaussianBlur(roi, ICON_BLUR_KSIZE, 0) if ICON_BLUR_KSIZE else roi
    ih, iw = r.shape[:2]
    if mask_badge:
        scaled = _scaled_library_badge(scales, max_px)
        order = list(coarse[0]) if coarse is not None else _coarse_rank_badge(roi)[0]
    else:
        scaled = _scaled_library(scales, max_px)
        order = list(coarse[0]) if coarse is not None else _coarse_rank(roi)[0]
    rest = [eid for eid in scaled if eid not in set(order)]
    scan = order + rest          # rest: 粗尺度装不下 ROI 的图标仍要扫 (保证零淘汰)
    stop = ICON_STOP_SCORE if stop_score is None else float(stop_score)
    step = int(batch) if batch and batch > 0 else len(scan) or 1
    best = (0.0, None)
    for start in range(0, len(scan), step):
        for eid in scan[start:start + step]:
            for t in scaled.get(eid, ()):
                if t.shape[0] > ih or t.shape[1] > iw:
                    continue  # 该尺度模板大于 ROI, 这格用不上
                _, score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(r, t, cv2.TM_CCOEFF_NORMED))
                if score > best[0]:
                    best = (float(score), eid)
                    if score >= ICON_EARLY_EXIT:
                        return best
        # 分批递进: 本批已有命中就停 (门槛取两列判定阈值的较大者, 不会截断到阈值之下);
        # 否则取下一批继续 —— 直到全库扫完, 召回与一次性全扫一致。
        if stop < 1.0 and best[0] >= stop:
            return best
    return best


def _badge_digit_mask(arr: np.ndarray, rect: tuple) -> np.ndarray | None:
    """裁槽左上角数量角标区, 二值化出深色数字笔画, 归一成 COUNT_DIGIT_W×COUNT_DIGIT_H.

    金色角标底(亮) + 深色数字(暗) 的对比极强, 直接按灰度阈值切笔画即可, 不需要 OCR。
    返回 None = 这个槽没有角标 (例如装备重铸器从来不写数量)。

    与 scripts/build_digit_templates.py 共用同一条流水线 —— 改这里必须同步重跑那个脚本,
    否则模板与运行时提取方式不一致, 分数会莫名掉下来。
    """
    x1, y1 = int(rect[0]), int(rect[1])
    crop = arr[y1:y1 + COUNT_BADGE_BOX, x1:x1 + COUNT_BADGE_BOX]
    if crop.size == 0 or min(crop.shape[:2]) < 12:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    mask = (gray < COUNT_DIGIT_DARK).astype(np.uint8)
    if int(mask.sum()) < 12:
        return None
    ys, xs = np.nonzero(mask)
    h, w = int(ys.max() - ys.min()) + 1, int(xs.max() - xs.min()) + 1
    if h < 6 or w < 3 or h > COUNT_BADGE_BOX or w > COUNT_BADGE_BOX:
        return None
    m = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    m = cv2.resize(m.astype(np.float32), (COUNT_DIGIT_W, COUNT_DIGIT_H), interpolation=cv2.INTER_AREA)
    return ((m > 0.35).astype(np.uint8) * 255)


def _load_digit_templates() -> dict[int, np.ndarray]:
    """读 recognize/templates/digits/<数字>.png (由 build_digit_templates 生成), 进程内缓存."""
    global _COUNT_DIGIT_TPL
    if _COUNT_DIGIT_TPL is None:
        tpl: dict[int, np.ndarray] = {}
        try:
            if DIGIT_TEMPLATE_DIR.exists():
                for p in sorted(DIGIT_TEMPLATE_DIR.glob("*.png")):
                    try:
                        d = int(p.stem)
                    except ValueError:
                        continue
                    m = np.asarray(Image.open(p).convert("L"))
                    if m.size == COUNT_DIGIT_W * COUNT_DIGIT_H:
                        tpl[d] = m.astype(np.uint8)
        except Exception:      # noqa: BLE001  读不到就退回 OCR, 不让识别整体挂掉
            tpl = {}
        _COUNT_DIGIT_TPL = tpl
    return _COUNT_DIGIT_TPL


def _read_count_by_template(mask: np.ndarray) -> tuple[bool, int | None]:
    """用数字模板读数量角标. 返回 (是否已定论, 数值).

    (True, n) = 定论: n 是数量
    (False, _) = 没法定论 (无模板 / 不是精确命中) → 调用方回退 OCR 保底

    判据是 **精确命中** 而不是"最高分领先": 实测同一数字跨帧渲染像素完全一致 (对自身模板
    相关 =1.000), 而不同数字之间反而很像 (5↔6 = 0.965, 5↔8 = 0.954) —— 用"领先幅度"判
    (最优 0.035, 阈值 0.12 只能采信 12%) 还不如用"必须几乎逐像素相等"。代价是没见过的
    数字一律走 OCR, 但那正是我们要的保守方向: **宁可慢, 不可读错**。
    """
    tpls = _load_digit_templates()
    if not tpls:
        return False, None
    a = mask.astype(np.float32)
    a = (a - a.mean()) / (a.std() + 1e-6)
    best_s, best_d = -2.0, None
    for d, t in tpls.items():
        b = t.astype(np.float32)
        b = (b - b.mean()) / (b.std() + 1e-6)
        s = float((a * b).mean())
        if s > best_s:
            best_s, best_d = s, d
    if best_d is None or best_s < COUNT_TEMPLATE_MIN_SCORE:
        return False, None
    return True, int(best_d)


def _read_count(arr: np.ndarray, rect: tuple) -> int | None:
    """消耗品左上角数量角标: 数字模板精确命中 (~1ms) → 角标哈希缓存 → OCR 兜底.

    2026-11-01 (P0-18): 原来是纯 OCR (裁 38px 放大 4x), 每次 70~300ms, 而且必须串行 ——
    并发请求会把远程 OCR 连接打爆 (ConnectionResetError → 降级本地 CPU 引擎, 更慢更不稳),
    于是它成了整栏并行的下限 (纯模板匹配已 3.3x, 整栏只有 1.1~1.4x)。现在:
      1) 模板精确命中 → 直接给数字 (本局 5/6/7/8 全覆盖, 120/120 正确);
      2) 没命中但同一角标以前读过 → 哈希缓存直接给 (OCR 每个"新"数字只付一次钱);
      3) 都没命中 → 才串行 OCR, 并把结果写进缓存。
    这样"没见过的数字"最多慢一次, 且永远不会因为模板不覆盖而读错。
    """
    if not USE_COUNT_TEMPLATES:
        return _read_count_ocr(arr, rect)
    try:
        mask = _badge_digit_mask(arr, rect)
    except Exception:      # noqa: BLE001  模板路径出任何问题都退回 OCR
        return _read_count_ocr(arr, rect)
    if mask is None:
        return None        # 没角标就是没数量, 不必再花 OCR 的钱
    key = hashlib.blake2b(mask.tobytes(), digest_size=8).digest()
    with _COUNT_TPL_LOCK:
        if key in _COUNT_TPL_CACHE:
            return _COUNT_TPL_CACHE[key]
    try:
        done, val = _read_count_by_template(mask)
    except Exception:      # noqa: BLE001
        done, val = False, None
    if not done:
        val = _read_count_ocr(arr, rect)
    with _COUNT_TPL_LOCK:
        _COUNT_TPL_CACHE[key] = val
    return val


def _read_count_ocr(arr: np.ndarray, rect: tuple) -> int | None:
    """数量角标 OCR (兜底路径): 裁剪槽左上角 38px 窗口放大 4x 后读数字.

    窗口实测: 34px 会切掉数字右缘误读为字母, 38px 置信度 0.98.
    OCR 必须串行: 并发请求会把远程 OCR 连接打爆 (ConnectionResetError → 降级本地 CPU
    引擎, 反而更慢/更不稳)。数量角标只是辅助字段, 不值得为它冒 OCR 崩溃的风险。
    """
    x1, y1 = rect[0], rect[1]
    corner = (x1, y1, x1 + 38, y1 + 38)
    with _COUNT_OCR_LOCK:
        items = ocr.recognize_region(Image.fromarray(arr), corner, upscale=4)
    digits = [ch for it in items for ch in it["text"] if ch.isdigit()]
    return int("".join(digits)) if 1 <= len(digits) <= 2 else None


def _formula_names(formula, meta: dict) -> list[str] | None:
    """合成公式解析: formula("508,503") -> 对应散件名称列表; 散件/消耗品无公式返回 None."""
    if not formula:
        return None
    ids = formula if isinstance(formula, (list, tuple)) else str(formula).split(",")
    names = []
    for x in ids:
        x = str(x).strip()
        if x.isdigit():
            names.append(meta.get(int(x), {}).get("name") or x)
    return names or None


def _normalize_slots(slots: list[tuple]) -> list[tuple]:
    """检出框明显偏离中位尺寸时按中心归一到中位尺寸.

    小框: 暗色/青蓝色调图标 (如神谕纹章) 在亮度阈值下只检出高亮核心, 框比标准槽
    小 20%+ → ROI 不完整导致图标库匹配失配; 大框: 金色粒子光效与槽体连通把检出框
    撑宽 (重铸器+角标帧实测 73px vs 中位 66px), 混入槽外光效/背景污染匹配.
    两者均按中心缩放修复.
    """
    if len(slots) < 2:
        return slots
    ws = sorted(s[2] - s[0] for s in slots)
    hs = sorted(s[3] - s[1] for s in slots)
    mw, mh = ws[len(ws) // 2], hs[len(hs) // 2]
    out = []
    for x1, y1, x2, y2 in slots:
        if abs(x2 - x1 - mw) > mw * 0.2 or abs(y2 - y1 - mh) > mh * 0.2:
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            out.append((cx - mw // 2, cy - mh // 2, cx - mw // 2 + mw, cy - mh // 2 + mh))
        else:
            out.append((x1, y1, x2, y2))
    return out


def count_equip(img: Image.Image, equip_id: int, *, scales=ICON_SCALES,
                threshold: float | None = None) -> tuple[int, list[dict]]:
    """数装备栏里还剩**几个** `equip_id` 这件 —— 逐槽 × 单模板, 约 20ms。

    为什么需要 (用户 m02532 的思路): 给装/合成的判据是"库存[key] N -> N-1", 只要知道
    **这一件还剩几个**就够, 不必知道另外 5 格各是什么。整栏全库识别 262ms, 数单件 ~20ms
    (detect_slots 14.5ms + 每格 6~8 次滑窗), 快 13 倍 —— 前后校验各一次, 单动作省 ~0.5s。
    数量**没变**时更好: 20ms 就能给出"没动"的结论, 直接省掉后面整个校验。

    与 `extract_equipment` 的关系: 那个回答"每格是什么", 这个回答"某件有几个"。
    同一份 `_scaled_library` 模板缓存, 所以要和 `extract_equipment` 用**同一批 scales**
    才能命中同一份缓存 (默认 ICON_SCALES 一致)。

    Returns:
        (count, hits) —— hits 是命中槽位列表 [{"slot", "score", "rect", "center"}, ...],
        count 为 len(hits)。没命中返回 (0, [])。
    """
    arr = _normalize(img)
    lib, _ = _load_library()
    icon = lib.get(int(equip_id))
    if icon is None:
        return 0, []
    thr = ICON_THRESHOLD if threshold is None else float(threshold)
    scaled = _scaled_library(scales, ICON_MAX_TEMPLATE)
    tpls = scaled.get(int(equip_id), ())
    if not tpls:
        return 0, []
    hits = []
    row, last_col = 0, 0
    for i, rect in enumerate(_normalize_slots(detect_slots(img)), 1):
        x1, y1, x2, y2 = rect
        col = 1 if x1 <= regions.EQUIP_COL1_MAX_X else 2
        row = row + 1 if col == last_col else 1
        last_col = col
        roi = arr[y1:y2, x1:x2]
        if roi.size == 0 or roi.shape[0] > 200 or roi.shape[1] > 200:
            continue
        r = cv2.GaussianBlur(roi, ICON_BLUR_KSIZE, 0) if ICON_BLUR_KSIZE else roi
        ih, iw = r.shape[:2]
        best = 0.0
        for t in tpls:
            if t.shape[0] > ih or t.shape[1] > iw:
                continue
            best = max(best, float(cv2.minMaxLoc(
                cv2.matchTemplate(r, t, cv2.TM_CCOEFF_NORMED))[1]))
            if best >= ICON_EARLY_EXIT:
                break
        if best >= thr:
            hits.append({"slot": i, "row": row, "col": col, "score": round(best, 3),
                         "rect": rect, "center": ((x1 + x2) // 2, (y1 + y2) // 2)})
    return len(hits), hits


def _slot_entry(arr: np.ndarray, rect: tuple, slot: int, row: int, col: int, *,
                stop: float, batch: int, full: bool = False) -> dict:
    """识别**单个槽**并组装 entry (供 `extract_equipment` / `extract_equip_at` 共用)。

    抽成函数是为了让"只认指定几格"的快路 (`extract_equip_at`) 与整栏扫描走**同一份**判定
    逻辑 —— 否则两条路的门槛/消耗品裁决一旦漂移, 就会出现"整栏说锁子甲、单格说是别的"。

    Args:
        stop: 分批早停线 (1.01 = 永不早停)。
        batch: 每批精匹多少个图标 (0 = 一次性全扫)。
        full: 只用于 `gated` 死路分支的开关, 语义同 `extract_equipment`。
    """
    x1, y1, x2, y2 = rect
    roi = arr[y1:y2, x1:x2]
    roi_n = _brightness_norm(roi)   # 匹配用: 逐槽亮度归一化(压暗/极亮下防漏认); 数量角标仍用原图
    _lib, meta = _load_library()          # 带缓存, 二次调用 ~0 开销
    entry = {"slot": slot, "row": row, "col": col,
             "equipId": None, "name": None, "type2": None, "kind": None,
             "effect": None, "image": None, "formula": None, "formula_name": None,
             "count": None, "score": 0.0, "rect": rect,
             "center": ((x1 + x2) // 2, (y1 + y2) // 2)}
    if not full and roi.size and _slot_is_empty(roi):
        # 已撤销的判据 (恒 False), 保留分支只为让推翻它的历史留在这里 ——
        # 详见 `_slot_is_empty` docstring: 深灰色散件的纹理能量比空槽还低, 无阈值可用。
        entry["gated"] = "empty"
        return entry
    # ---- 消耗品通道 (只前 N 格) 与图标库**两个都跑**, 再按像素证据裁决 (用户 m00248, 2026-11-02) ----
    # 旧逻辑的致命处: 消耗品 corr >= 0.85 就"直接采信, 免图标库全库比对" —— 于是两通道**没有比较**,
    # 色相像一点就赢。事故: 成装「海妖之怒」(蓝金) 撞「次级英雄复制器」模板 corr 0.868 / NCC 0.331
    # -> 过 0.85 线 -> 跳过图标库 (iscore 恒 0) -> 判成消耗品 -> `cmd_equip_to` 拒收。
    # 现在三条: ① 双门槛先筛 (corr>=0.85 且 **NCC>=0.42**); ② 图标库**始终**跑;
    # ③ 两边都命中时拿 ncc 跟图标库分比高低, 消耗品要**明显更高**才允许覆盖。
    cscore, cname, cncc = (0.0, "", 0.0)
    if slot <= CONSUMABLE_MAX_SLOT:
        cscore, cname, cncc = _consumable_score(roi_n)
    cons_ok = (bool(cname) and cscore >= CONSUMABLE_HUE_THRESHOLD
               and cncc >= CONSUMABLE_PIXEL_GATE)
    # 前 3 格"去数字"匹配: 只在**确实检出数量角标**的格子上做 (代表是消耗品), 蒙掉角标区再比对。
    # 普通散件/成装没有角标 → 走原全图匹配, 不受影响、也不多花时间 (见 _mask_badge / BADGE_MASK_*)。
    _mask = (BADGE_MASK_ENABLED and slot <= BADGE_MASK_MAX_SLOT and bool(roi.size)
             and _has_badge(roi[ICON_INSET:-ICON_INSET, ICON_INSET:-ICON_INSET]))
    iscore, eid = (0.0, None)
    if roi.size:
        coarse = _coarse_rank_badge(roi_n) if _mask else _coarse_rank(roi_n)
        iscore, eid = _match_library(roi_n, coarse=coarse, stop_score=stop, batch=batch,
                                     mask_badge=bool(_mask))
    # 溢出列的图标库门槛更严: 该 x 带与羁绊图标列重叠, 羁绊图标会被误判成槽位并低分错配
    # (详见 ICON_THRESHOLD_COL2 注释)
    _icon_thr = ICON_THRESHOLD_COL2 if col == 2 else ICON_THRESHOLD
    icon_ok = eid is not None and iscore >= _icon_thr
    if cons_ok and (not icon_ok or cncc >= iscore + CONSUMABLE_NCC_MARGIN):
        entry.update(name=cname, score=round(cscore, 3), kind="消耗品",
                     count=_read_count(arr, rect))
    elif icon_ok:
        info = meta.get(eid, {})
        effect = info.get("effect")
        if effect:  # 爬虫原文含 HTML 换行标记, 清洗后再输出
            effect = effect.replace("<br>", " ")
        entry.update(equipId=eid, name=info.get("name"), score=round(iscore, 3),
                     kind=KIND_NAMES.get(info.get("type2"), info.get("type2")),
                     type2=info.get("type2"), effect=effect,
                     image=info.get("image"), formula=info.get("formula"),
                     formula_name=_formula_names(info.get("formula"), meta))
    return entry


def _slot_tasks(slots: list[tuple], want: set[int] | None = None) -> list[tuple]:
    """把 `detect_slots` 的框列表转换成 (slot, rect, row, col) 任务列表.

    row/col 是"列内自上而下第几行 / 主列还是溢出列", 必须**按顺序串行算** —— 但这一步只遍历
    十几个框, 纳秒级; 真正昂贵的 `_slot_entry`(模板匹配/OCR) 才是并行的对象。
    """
    tasks: list[tuple] = []
    row, last_col = 0, 0
    for i, rect in enumerate(slots, 1):
        x1 = rect[0]
        col = 1 if x1 <= regions.EQUIP_COL1_MAX_X else 2
        row = row + 1 if col == last_col else 1
        last_col = col
        if want is not None and i not in want:
            continue
        tasks.append((i, rect, row, col))
    return tasks


def _warm_caches() -> None:
    """并行前预建三个懒加载缓存 (图标库 / 多尺度模板 / 粗尺度模板).

    不做这步的话, 多线程会同时触发首次构建: 白做一遍(每份 150 图标的缩放) 且互相覆盖
    global, 极端情况下拿到半成品。预建后所有线程只读, 天然线程安全。
    """
    _load_library()
    _load_templates()   # 消耗品模板也要预建: 见 _load_templates 里"首帧并行漏识别"的坑
    if USE_COUNT_TEMPLATES:
        _load_digit_templates()   # 数量角标数字模板同理, 冷启动首帧并行必须预建
    _scaled_library(ICON_SCALES, ICON_MAX_TEMPLATE)
    _coarse_library()
    if BADGE_MASK_ENABLED:
        # 前 3 格的"去数字"模板缓存同理, 必须预建, 否则多线程首次同时构建会白做且互相覆盖
        _scaled_library_badge(ICON_SCALES, ICON_MAX_TEMPLATE)
        _coarse_library_badge()


def _slot_entries(tasks: list[tuple], arr: np.ndarray, *, stop: float, batch: int,
                  full: bool = False, parallel: bool | None = None) -> list[dict]:
    """按任务列表识别每槽; parallel=True 时用线程池并发, 输出顺序与串行**完全一致**.

    结果一致性来自: 每槽只读 `arr`(numpy 只读切片) 与只读缓存, 槽与槽之间没有任何共享可写状态。
    并行只为省墙钟, 不改任何判定; 任何异常都退化成串行, 保证行为与旧版一致。
    """
    def _one(t):
        i, rect, row, col = t
        return _slot_entry(arr, rect, i, row, col, stop=stop, batch=batch, full=full)

    par = ICON_PARALLEL if parallel is None else bool(parallel)
    if not par or len(tasks) <= 1:
        return [_one(t) for t in tasks]
    try:
        _warm_caches()
        workers = max(1, min(len(tasks), ICON_MATCH_WORKERS))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_one, tasks))
    except Exception:
        return [_one(t) for t in tasks]   # 线程池不可用/子任务抛错 -> 逐槽串行兜底


def _grid_cells() -> list[list[tuple]]:
    """按常量网格生成两列候选槽矩形 (每列自顶向下)。

    顺序与 `detect_slots` 的 slot 顺序一致: 先主列(col1) 自上而下, 再溢出列(col2) 自上而下。
    """
    cells: list[list[tuple]] = []
    for gx in (EQUIP_GRID_COL1_X, EQUIP_GRID_COL2_X):
        col: list[tuple] = []
        for k in range(EQUIP_GRID_MAX_PER_COL):
            y = EQUIP_GRID_TOP_Y + k * EQUIP_GRID_PITCH
            if y + EQUIP_GRID_H > EQUIP_STRIP[3]:
                break
            col.append((gx, y, gx + EQUIP_GRID_W, y + EQUIP_GRID_H))
        cells.append(col)
    return cells


def _blank_entry(rect: tuple, slot: int, col: int = 1) -> dict:
    """空格占位 entry (字段与 _slot_entry 同构, name=None)。"""
    x1, y1, x2, y2 = rect
    return {"slot": slot, "row": slot, "col": col,
            "equipId": None, "name": None, "type2": None, "kind": None,
            "effect": None, "image": None, "formula": None, "formula_name": None,
            "count": None, "score": 0.0, "rect": rect,
            "center": ((x1 + x2) // 2, (y1 + y2) // 2), "gated": "blank"}


def _slots_by_grid(arr: np.ndarray, *, want: set[int] | None = None,
                   stop: float, batch: int, full: bool = False) -> list[dict] | None:
    """常量网格定位 + 逐格识别 —— **只扫主列(col1, 最多 10 格)**。

    - 不吃亮度连通域, 所以压暗(海克斯说明面板)/亮背景(选秀/加载/战斗特效)下照样能用。
    - 空格快速略过: 只有整条带足够亮 (V 99.5 分位 > 140) 时"铜框判据"才可信, 此时用 `_has_frame`
      省掉空槽 ~270ms 的全库空扫; 压暗帧铜框被压没(实测真实件 edge_ratio 0~0.18), 不用该判据,
      直接逐格匹配(慢但准, 实测 160 帧里只有 4 帧属这类)。
    - 返回 None = 主列已满 10 件(可能有溢出列/羁绊列混淆), 网格路径处理不了, 调用方回退旧实现。
    """
    col1 = _grid_cells()[0]
    if want is not None and any((s < 1 or s > len(col1)) for s in want):
        return None
    x1, y1, x2, y2 = EQUIP_STRIP
    sv = cv2.cvtColor(arr[y1:y2, x1:x2], cv2.COLOR_RGB2HSV)[:, :, 2]
    bright = float(np.percentile(sv, 99.5)) > 140.0
    # 第一步(串行, 极便宜): 用铜框判据标出空格, 剩下要真匹配的格子组成任务列表
    tasks: list[tuple] = []
    plan: list[tuple[str, int, tuple]] = []      # ("blank"|"match", slot, rect) 保持自上而下顺序
    for row_i, rect in enumerate(col1, start=1):
        if want is not None and row_i not in want:
            continue
        if (not full) and bright and not _has_frame(arr, rect):
            plan.append(("blank", row_i, rect))
        else:
            plan.append(("match", row_i, rect))
            tasks.append((row_i, rect, row_i, 1))
    # 第二步(并行): 所有"要真匹配"的格子一次性丢线程池 —— 网格路径原来这里是**串行**循环,
    # 9 格串行 ~700ms; 并行后墙钟 ≈ 最慢那一格 (~250~300ms), 这是整栏提速的关键一步。
    matched: dict[int, dict] = {}
    if tasks:
        for e in _slot_entries(tasks, arr, stop=stop, batch=batch, full=full):
            matched[int(e.get("slot") or 0)] = e
    # 第三步(串行, 只做收尾): 按原顺序拼结果, 保留"连续空格到底"的早停语义
    entries: list[dict] = []
    empties = 0
    for kind, row_i, rect in plan:
        e = _blank_entry(rect, row_i) if kind == "blank" else matched.get(row_i) or _blank_entry(rect, row_i)
        if e.get("name"):
            entries.append(e)
            empties = 0
        elif want is not None:
            entries.append(e)          # 指定格认不出也返回, 由调用方判断是否回退
        else:
            empties += 1
            if empties > EQUIP_GRID_EMPTY_TOLERANCE:
                break
    if want is None and len([e for e in entries if e.get("name")]) >= len(col1):
        return None                    # 主列满 -> 交给旧实现处理溢出列
    return entries


def extract_equip_at(img: Image.Image, slots, *, full: bool = False) -> list[dict]:
    """只识别**指定槽位**的装备 (槽号 = `extract_equipment` 里的 slot, 1 起)。

    为什么需要 (2026-10-31 提速): combine 的 A/B 通常直接给格号, 却要整栏识别一遍。
    combat 阶段整栏 8 格里有 3~4 个**空槽**, 每个空槽都要把 ~150 件图标全扫完 (~306ms)
    才能确认"这里没东西" —— 实测整栏 1120~1220ms, 3 个空槽就占 900ms 以上。而合成只需要
    知道"格4 和格5 是什么", 认这两格 ~77ms/格, 快 7 倍以上。

    与 `count_equip` 的分工: 那个按**某件装备 ID** 数几个, 这个按**槽号**认是什么。

    返回格式与 `extract_equipment` 完全一致 (同一个 `_slot_entry`), 只包含命中的槽位。
    """
    want = {int(s) for s in slots}
    arr = _normalize(img)
    stop = 1.01 if full else ICON_STOP_SCORE      # 1.01 = 永不早停
    batch = 0 if full else ICON_BATCH
    if EQUIP_GRID_ENABLED:
        entries = _slots_by_grid(arr, want=want, stop=stop, batch=batch, full=full)
        if entries and all(e.get("name") for e in entries):
            return entries
    tasks = _slot_tasks(_normalize_slots(detect_slots(img)), want=want)
    return _slot_entries(tasks, arr, stop=stop, batch=batch, full=full)


def extract_equipment(img: Image.Image, *, full: bool = False) -> list[dict]:
    """提取左侧装备栏全部装备, 返回按凹槽自上而下排序的列表.

    full=False (默认): 每格按粗筛排序**分批递进 + 早停** —— 有货槽第 1 批就命中 (~77ms/槽),
        召回与一次性全扫完全等价 (见 `_match_library` 第 3 条);
    full=True (最慢但最保险): 每格一次性扫完整个图标库, 不早停 —— 等价于旧行为。
        **但两条路的结果本来就该一样**, full 只用于对拍/回归, 不是"捞回漏认"的兜底
        (曾经的"空槽+粗分线提前摘掉 + full 兜底捞回"两道路径已全部撤销, 见文件头)。

    Returns:
        [{"slot": 1起, "equipId": int|None, "name": str|None, "type2": str|None,
          "kind": str|None, "effect": str|None, "image": str|None,
          "formula": str|None, "formula_name": [散件名, ...]|None,
          "count": int|None, "score": float,
          "rect": (x1,y1,x2,y2), "center": (cx,cy)}, ...]
        equipId/type2/image/formula 来自爬虫库 items.json, formula_name 为公式
        各散件 ID 对应的名称; 图标库/模板都未命中时 name 为 None (仅报告槽位,
        供可视化排查).
    """
    arr = _normalize(img)
    stop = 1.01 if full else ICON_STOP_SCORE      # 1.01 = 永不早停 (没有分数能到)
    batch = 0 if full else ICON_BATCH             # 0 = 一次性全扫
    if EQUIP_GRID_ENABLED:
        entries = _slots_by_grid(arr, stop=stop, batch=batch, full=full)
        if entries is not None:
            return entries
    tasks = _slot_tasks(_normalize_slots(detect_slots(img)))
    return _slot_entries(tasks, arr, stop=stop, batch=batch, full=full)


def _consumable_names() -> set[str]:
    """消耗品模板名集合 (拆卸器 / 重铸器 / 幸运装备宝箱 / 次级英雄复制器)。

    内存为主模式 (`extract_equipment_by_mem(mem_first=True)`) 用它给 `kind` 定值 ——
    内存只知道 tableId，不区分"消耗品/装备"，而 `cmd_equip_to` 的消耗品硬拦只看 kind。
    """
    try:
        return {str(x[0]) for x in _load_templates() if x and x[0]}
    except Exception:      # noqa: BLE001 - 模板缺失时退化成"不认识任何消耗品"
        return set()


def extract_equipment_by_mem(img: Image.Image, mem_items: list[dict],
                             conflicts: list | None = None,
                             mem_first: bool = False) -> list[dict] | None:
    """内存定件数 + 只精识别"有货的前 N 格", 结果与 `extract_equipment` **完全同构**.

    为什么这样做 (2026-11-01 内存对拍 + 实机实测):
    1. 内存 `PlayerModel.EquipmentList` 的 **List 顺序就是装备栏自上而下的槽位顺序**
       (111 样本里件数一致 108、顺序完全一致 107; 4 处不一致全是图色认错/漏认, 内存全对).
    2. 装备栏整栏 1.2s 里 **大头是空槽**: 空槽必须扫完整库才能确认"没有"(单槽 ~275ms),
       而有货的格子 22~40ms 就早停命中. 若知道件数 N, 只扫前 N 格即可.
    3. 装备栏**物品总是从第 1 格连续占满前 N 格** (合成/使用后整体平移), 所以前 N 格 == 全部物品.

    因此: 内存只用来提供**件数 N** 与**名字交叉核对**, 名字与像素仍由真识别器逐个出
    (`_slot_entries`), 与今天 `extract_equipment` 的结果等价 —— 只是跳过了空槽扫描.
    这样做**不改变任何识别结果**, 只省时间, 也不会有"按内存序号硬凑"的采错风险.

    参数:
        img: 当前帧 (1920x1080 基准).
        mem_items: 已按 slot 升序的内存装备列表, 每项
            {"slot": 0起列表索引, "tid": int, "count": int, "name": str|None}.

    返回:
        list[dict] (字段同 `extract_equipment`, 额外 `_src="mem_slots"`); 或 **None** ——
        表示失配 (件数>检出槽数 / 前 N 格里混进了空槽 / 内存名字与图色名字对不上),
        调用方**必须回退整栏图色** (铁律: 宁可慢一点, 不可采错).

    `conflicts` (用户 m00248 新增, 2026-11-02): 可选的**出参**。失配时把「第几格 / 内存叫什么 /
    图色叫什么」逐条 append 进去。以前这里只 `return None` —— 上层只知道"对不上", 不知道
    **哪一格对不上、两边各叫什么**, 于是
    `图色=次级英雄复制器 / 内存=海妖之怒` 的冲突被静默降级成"回退图色", 一路错到
    `cmd_equip_to` 的消耗品拦截才炸, 而且炸出来的文案把根因指错了。调用方拿到明细才能告警。

    `mem_first` (用户 m00464 新增, 2026-11-02): **以内存为主**。件数/槽号/名字全按内存,
    图色只负责给每格的像素矩形与"交叉核对"。命中/漏认/认错都**不改变结论**(一律返回条目),
    差异只记进 `conflicts` 供上层提示。为什么: 图色漏认一格(例: 饮血剑 2006 那格没认出)
    会让 `equip_to 3` 直接报「目标 3 不在」, 而内存对"装备栏有哪几件、按什么顺序排"是精确的
    (2026-11-01 对拍: 件数一致 108/111、顺序一致 107/111, 4 处不一致全是图色错)。
    内存查不到名字的件**不猜名字**: 保留 `mem_table_id` 且 `name=None`, 由上层按 ID 兜底.
    """
    if not mem_items:
        return None
    # 常量网格直接给候选槽 (不走 detect_slots 的匹配式占用判定, 免得同一处识别两遍)
    slots = ([rect for col in _grid_cells() for rect in col]
             if EQUIP_GRID_ENABLED else _normalize_slots(detect_slots(img)))
    n = len(mem_items)

    def _note(kind: str, detail: str, **kw) -> None:
        if conflicts is not None:
            conflicts.append({"kind": kind, "detail": detail, **kw})

    # 检出槽数少于内存件数 -> 说明 detect_slots 漏格, 无法可靠取前 N 格, 交回图色.
    if not slots or n > len(slots):
        _note("slot_count", "内存 %d 件 > 图色检出槽位 %d 个" % (n, len(slots or [])),
              mem_n=n, fig_n=len(slots or []))
        return None
    arr = _normalize(img)
    # 只精识别"有货的前 N 格"; 空槽一个都不扫 (这是本函数的全部收益).
    entries = _slot_entries(_slot_tasks(slots[:n]), arr,
                            stop=ICON_STOP_SCORE, batch=ICON_BATCH)
    if mem_first:
        # ---- 内存为主 (用户 m00464): 条目**一律产出**, 名字/件数按内存, 图色只给框与交叉核对 ----
        # 关键收益: 图色漏认某格时, 这一格仍然存在(slot/rect/名字都在), 于是
        # `equip_to 3` 按格号能找到目标, 不会再报「目标 ['3'] 不在」。
        out: list[dict] = []
        cons_names = _consumable_names()
        # 装备栏正在"变化"时**不做逐格名字交叉核对** (用户 m04307, 2026-11-05):
        # 拆卸/合成/拾取之后, 物品要飞回装备栏并整体重排, 动画期间图色只认得出前几格、
        # 且逐格内容整体错位(例: 内存[拆卸器,鬼索,纹章,日炎,枪刃] 图色只看得到
        # [拆卸器,纹章,正义之手] 3 格)。这时拿图色去核对内存必然全"冲突", 但**图色本身
        # 没错**, 只是抓帧抓在动画中间 —— 6-3 实测: 拆卸器成功(05:13:45)后 1s 抓帧报冲突,
        # 3s 后同一装备栏图色与内存 4 件**逐格 100% 一致**。
        # 判据: 图色认得出的有货格数 < 内存件数 (新件还在飞、格子还没落定)。
        _partial = len(entries) < n
        if _partial:
            _note("anim_pending",
                  "图色只认出 %d 格 < 内存 %d 件 (装备栏重排动画中), 跳过逐格名字核对"
                  % (len(entries), n), mem_n=n, fig_n=len(entries))
        for i, it in enumerate(mem_items):
            slot = i + 1
            e = entries[i] if i < len(entries) else _blank_entry(slots[i], slot)
            mem_name = (it.get("name") or "").strip() or None
            fig_name = e.get("name")
            if _partial:
                pass                      # 动画中间帧: 名字一律以内存为准, 不核对不报冲突
            elif mem_name and fig_name and mem_name != fig_name:
                _note("name_mismatch",
                      "格%s 图色=%s / 内存=%s" % (e.get("slot"), fig_name, mem_name),
                      slot=e.get("slot"), mem_name=mem_name, fig_name=fig_name,
                      mem_table_id=it.get("tid"))
            elif mem_name and not fig_name:
                _note("fig_missing", "格%s 图色漏认, 内存=%s" % (e.get("slot"), mem_name),
                      slot=e.get("slot"), mem_name=mem_name, fig_name=None,
                      mem_table_id=it.get("tid"))
            item = dict(e)
            item["name"] = mem_name or fig_name          # 内存优先; 内存查不到才用图色名
            item["_src"] = "mem_slots"
            item["mem_table_id"] = it.get("tid")
            if item["name"] in cons_names:
                item["kind"] = "消耗品"                   # 内存不区分消耗品, 靠模板名补
            try:
                cnt = int(it.get("count") or 1)
            except (TypeError, ValueError):
                cnt = 1
            item["count"] = cnt if cnt > 1 else None
            out.append(item)
        return out
    if len(entries) != n:
        _note("slot_count", "图色只认出 %d 格, 内存 %d 件" % (len(entries), n),
              mem_n=n, fig_n=len(entries))
        return None
    # 前 N 格里只要有一格认不出 (name=None), 就说明这 N 格里混进了空槽 = 对不齐.
    bad = False
    for e, it in zip(entries, mem_items):
        fig_name = e.get("name")
        mem_name = it.get("name")
        if not fig_name:
            bad = True
            _note("fig_missing", "格%s 图色认不出, 内存=%s" % (e.get("slot"), mem_name),
                  slot=e.get("slot"), mem_name=mem_name, fig_name=None,
                  mem_table_id=it.get("tid"))
        elif mem_name and mem_name != fig_name:
            bad = True
            _note("name_mismatch",
                  "格%s 图色=%s / 内存=%s" % (e.get("slot"), fig_name, mem_name),
                  slot=e.get("slot"), mem_name=mem_name, fig_name=fig_name,
                  mem_table_id=it.get("tid"))
    # 内存名字 vs 图色名字交叉核对: 有任何一件对不上就整批交回图色 (宁可慢, 不可错).
    if bad:
        return None
    for e in entries:
        e["_src"] = "mem_slots"
    return entries


def extract_detail_equips(img: Image.Image) -> list[dict]:
    """详情卡下方 3 个装备槽内的装备识别 (regions.DETAIL_EQUIP_SLOTS 固定槽位).

    详情页图标比装备栏大 (图标约 80px vs 60px), 复用图标库但用独立尺度表
    DETAIL_EQUIP_SCALES; 空槽为暗色马甲占位, 低于阈值时各字段保持 None.

    Returns:
        固定 3 项 (slot 1起): {"slot", "equipId", "name", "score", "rect", "center"}.
    """
    arr = _normalize(img)
    _, meta = _load_library()
    result = []
    for i, rect in enumerate(regions.DETAIL_EQUIP_SLOTS, 1):
        x1, y1, x2, y2 = rect
        roi = arr[y1:y2, x1:x2]
        entry = {"slot": i, "equipId": None, "name": None, "score": None,
                 "rect": rect, "center": ((x1 + x2) // 2, (y1 + y2) // 2)}
        score, eid = _match_library(roi, scales=DETAIL_EQUIP_SCALES, max_px=DETAIL_ICON_MAX)
        if eid is not None and score >= DETAIL_EQUIP_THRESHOLD:
            entry.update(equipId=eid, name=meta.get(eid, {}).get("name"), score=round(score, 3))
        result.append(entry)
    return result


def annotate(img: Image.Image, equipments: list[dict]) -> None:
    """在原图上画装备槽位方框+名称标签 (供 show_regions 使用), 原地修改."""
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 14)
    for eq in equipments:
        x1, y1, x2, y2 = eq["rect"]
        label = f"{eq['slot']}:{eq['name'] or '?'}"
        if eq["count"] is not None:
            label += f"x{eq['count']}"
        draw.rectangle((x1 - 3, y1 - 3, x2 + 3, y2 + 3), outline=(0, 220, 120), width=2)
        draw.text((x2 + 6, y1 + 2), label, fill=(0, 220, 120), font=font,
                  stroke_width=2, stroke_fill=(0, 0, 0))


# ---------------------------------------------------------------------------
# 棋盘奕子装备识别 (血条下方 26px 图标 vs 官方图标库)
#
# 2026-10-18 复测: 官方 64px 图标缩到 ~24px 用 TM_CCOEFF_NORMED 匹配棋盘 26px 图标,
# 正确对 0.85~0.92 (巨人腰带/负极斗篷/拳套), 库外误分 ≤0.67, 阈值 0.75~0.80 干净分开。
# 旧报告"模板匹配不可行"是把棋盘腰带错当成拳套去比官方拳套模板造成的假象。
# 详见 dev_docs/02_采集与训练/装备识别_模板匹配探索_交接.md §3.5。
# ---------------------------------------------------------------------------

BOARD_ICON_SIDE = 26                        # 棋盘装备图标边长 px (1920x1080 基准)
BOARD_ICON_OFFSET = (-10, 20)               # 血条左上角 -> 首件装备图标左上角 偏移 (dx, dy)
BOARD_ICON_PITCH = 28                       # 同奕子多件装备图标的中心间距 px (横排/竖排)
BOARD_ICON_MAX = 3                          # 单奕子装备数上限 (最多 3 件)
# 无血条 (只有 rect, bar=None; 纯图色工程无 SAM3/内存补检) 时从身体框 rect 推算装备图标位置:
# 实测 (2026-10-18, 示例图 r4c1 卡密尔 珠光护手+巨人腰带): 图标在 rect 顶边上方 ~32px,
# 横排间距 ~30px; 与 rect=(646,570,688,639) 对得上 (图标 650/680@538 = x1+4 / x1+4+30, y1-32)。
BOARD_ICON_RECT_DX = 4                      # rect 左边缘 -> 首件图标左上角 x 偏移
BOARD_ICON_RECT_DY = -33                    # rect 顶边 -> 图标左上角 y 偏移 (实测 536/537 均 0.851)
BOARD_ICON_RECT_PITCH = 30                  # 无血条时多件装备的横排间距
BOARD_ICON_RECT_DY_TOL = 1                  # rect 版 y 方向 ±1px 微调 (身体框有 1~2px 抖动)
BOARD_ICON_SCALES = tuple(range(14, 28))    # 官方图标缩到 14~27px 去匹配 (棋盘图标内图案约 24px)
BOARD_ICON_COARSE = 22                      # 粗筛尺度 (中位, 只用来给全库排序, 不淘汰)
BOARD_ICON_COARSE_GATE = 0.50               # 粗筛门槛: 22px 粗筛 top1 低于此值必是背景/空位, 直接跳过
                                            # (实测装备粗筛 0.73~0.81, 背景 0.20~0.36, 中间有安全间隔)
BOARD_ICON_EARLY_EXIT = 0.80                # 早停分 (命中档 0.85+, 早停可把单图标从 ~100ms 压到 ~10ms)
BOARD_ICON_THRESHOLD = 0.75                 # 命中阈值 (正确对 0.85+, 误分 ≤0.67)
BOARD_TEMPLATE_FILENAME = "board_item_templates.pkl"

_BOARD_TEMPLATES: dict | None = None   # {equipId: {"coarse": 22px, "multi": [14~27px]}}, 惰性加载/构建


def _board_template_path():
    """模板库持久化路径 (跟爬虫版本走, 图标库更新即自动失效重建)."""
    return default_config.SCRAPE_INFO_DIR / SCRAPE_VERSION / BOARD_TEMPLATE_FILENAME


def ensure_board_template_library(force_rebuild: bool = False) -> dict:
    """确保「棋盘装备匹配模板库」存在, 不存在则从官方图标库构建并持久化, 返回模板库.

    模板库 = {equipId: {"coarse": 22px 粗筛模板, "multi": [14~27px 多尺度模板]}}。
    首次调用约 ~10ms (150 图标 x 14 尺度) 构建一次并落盘到
    ``workspace/scrape_info/<version>/board_item_templates.pkl`` (pickle), 之后跨运行直接加载。
    force_rebuild=True 强制重建 (图标库变了 / 缓存损坏时用)。
    """
    global _BOARD_TEMPLATES
    path = _board_template_path()
    if _BOARD_TEMPLATES is not None and not force_rebuild:
        return _BOARD_TEMPLATES
    if path.exists() and not force_rebuild:
        with open(path, "rb") as f:
            _BOARD_TEMPLATES = pickle.load(f)
        return _BOARD_TEMPLATES
    lib, _ = _load_library()
    tpl: dict = {}
    for eid, icon in lib.items():
        tpl[eid] = {
            "coarse": cv2.resize(icon, (BOARD_ICON_COARSE, BOARD_ICON_COARSE)),
            "multi": [cv2.resize(icon, (s, s)) for s in BOARD_ICON_SCALES],
        }
    if not tpl:
        # 图标库为空 (爬虫数据未下载): 不落盘 (避免空库覆盖好库), 下次调用再试
        _BOARD_TEMPLATES = tpl
        return _BOARD_TEMPLATES
    _BOARD_TEMPLATES = tpl
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(tpl, f, protocol=pickle.HIGHEST_PROTOCOL)
    return _BOARD_TEMPLATES


def _tm_score(roi: np.ndarray, tpl: np.ndarray) -> float:
    """TM_CCOEFF_NORMED 单模板匹配峰值 (tpl 须 ≤ roi)."""
    return float(cv2.minMaxLoc(cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED))[1])


def _match_board_icon(roi: np.ndarray, templates: dict) -> tuple[float, int | None]:
    """26x26 棋盘图标全库匹配: 粗筛排序 + 多尺度滑窗 + 图标级早停. 返回 (最高分, equipId).

    早停是「图标级」而非「尺度级」: 每个候选图标先扫完它的全部尺度取峰值, 峰值 ≥ 早停阈值
    才返回 —— 保证返回的是该图标的真实最高分, 而不是「第一个过阈值的尺度分」(后者会把腰带
    0.922 截成 0.804, 分数被低估, 后续阈值判断不稳)。
    """
    ih, iw = roi.shape[:2]
    order = []
    for eid, t in templates.items():
        c = t["coarse"]
        if c.shape[0] <= ih and c.shape[1] <= iw:
            order.append((_tm_score(roi, c), eid))
    order.sort(reverse=True)
    if not order or order[0][0] < BOARD_ICON_COARSE_GATE:
        return 0.0, None  # 粗筛 top1 都够不着装备, 必是背景/空位, 省掉多尺度全扫
    best = (0.0, None)
    for _, eid in order:
        eid_best = 0.0
        for t in templates[eid]["multi"]:
            if t.shape[0] > ih or t.shape[1] > iw:
                continue
            sc = _tm_score(roi, t)
            if sc > eid_best:
                eid_best = sc
        if eid_best > best[0]:
            best = (float(eid_best), eid)
            if eid_best >= BOARD_ICON_EARLY_EXIT:
                return best
    return best


def extract_board_equips(img: Image.Image, units=None,
                         threshold: float = BOARD_ICON_THRESHOLD,
                         max_equips: int = BOARD_ICON_MAX) -> list[dict]:
    """识别棋盘/备战席奕子身上的装备 (血条下方 26px 图标 vs 官方图标库).

    Args:
        img: 1920x1080 截图 (PIL Image), 内部会归一化到该基准。
        units: ``board.extract_units(img)`` 的返回 (dict, 含 board/bench 两键);
            缺省时内部调用 recognize.board.extract_units (血条路径, 约 0.3s)。
        threshold: 命中阈值 (实测正确对 0.85+, 库外误分 ≤0.67, 建议 0.75~0.80)。
        max_equips: 单奕子装备数上限 (最多 3 件, 图标横排/竖排)。

    Returns:
        每个「检出至少 1 件装备」的 unit 一条:
            {"region": "board"|"bench", "location": {...}|None, "bar": [x1,y1,x2,y2],
             "unit_name": str|None, "equips": [
                 {"equipId", "name", "score", "rect": (x1,y1,x2,y2), "center": (cx,cy)}, ...]}
        空位/未命中的 unit 不产出。有血条时候选位置按横排 + 竖排 (间距 28px) 遍历;
        无血条 (只给 `rect`) 时从身体框 `rect` 推算 (见 BOARD_ICON_RECT_* 常量)。
        背景/空位用 `_roi_is_dark` + 粗筛门槛两道闸快速跳过, 同一装备 id 只计一次 (去重)。
    """
    arr = _normalize(img)
    templates = ensure_board_template_library()
    _, meta = _load_library()
    if units is None:
        from recognize import board as board_mod
        units = board_mod.extract_units(img)
    dx, dy = BOARD_ICON_OFFSET
    results: list[dict] = []
    for region, us in units.items():
        for u in us:
            bar = u.get("bar")
            rect = u.get("rect")
            # ---- 2026-10-31 撤销: 「血条顶 - YOLO框顶」的 gap 闸门 ----
            # 曾想把没装备的棋子在模板匹配前就跳过 (每只省 0.31s), 实测**不成立**, 已删:
            #   卡密尔/卡西奥佩娅 (有装备) gap = -46~-51, 看着能用;
            #   但 峡谷迅捷蟹(巨人腰带) gap = -8、阿卡丽(负极斗篷) gap = -8 —— 同样有装备, 却和
            #   20 只**没装备**的棋子 (-7~-8) 完全一样, 闸门会把它们整只跳过 -> 漏认 2/3 的真装备
            #   (5 张标注图里 3 件只认出 1 件)。
            # 教训: 这个 gap 量的是**棋子体型** (卡密尔框本身就高), 不是"有没有装备" —— 两者是巧合
            # 相关, 不是因果。任何"用一个数把有/无装备分开"的想法都必须**双向取样**(同一只棋子
            # 有装备 vs 没装备) 才能采信。
            # 候选图标左上角: 有血条走血条左下角 (横排+竖排); 无血条 (只给 rect) 从身体框推算.
            if bar:
                x1, y1 = bar[0], bar[1]
                # 候选位置必须是**完整方阵** (i, j 各取 0..max_equips-1), 不能只扫 L 形。
                # 2026-10-25 实机坐实: 官方图标并不总排在 L 形上 —— stage 4-1 帧里 board,1,4 的
                # 「正义之手」落在 (i=2, j=1) (得分 0.827), 而 L 形只覆盖 (i,0)/(0,j), 该格永远认不出
                # -> extract_board_equips 对其返回空 -> equip_verify.confirm 的 before_equips 为空
                # -> trust_inv=False -> 给装成功却判失败 (库存=1->0 仍 ok=False), 白烧 9s 且触发重试。
                # 偶发在 (i,j) 二维组合的代价: 单帧 47 次 -> 85 次模板匹配, 约 +0.25s (0.91s -> 1.16s)。
                origins = [(x1 + dx + i * BOARD_ICON_PITCH, y1 + dy + j * BOARD_ICON_PITCH)
                           for i in range(max_equips) for j in range(max_equips)]
            elif rect:
                x1, y1 = rect[0], rect[1]
                # 每件装备的 y 允许 ±TOL 微调 (身体框有 1~2px 抖动), 取该位置最高分。
                # 逐个试: 低于阈值的偏移不落库, 首个过阈值的落库后同 id 去重 —— 实际拿到的是最高分。
                origins = []
                for i in range(max_equips):
                    rx = x1 + BOARD_ICON_RECT_DX + i * BOARD_ICON_RECT_PITCH
                    for ddy in range(-BOARD_ICON_RECT_DY_TOL, BOARD_ICON_RECT_DY_TOL + 1):
                        origins.append((rx, y1 + BOARD_ICON_RECT_DY + ddy))
            else:
                continue
            equips: list[dict] = []
            seen_ids: set = set()
            for rx, ry in origins:
                roi = arr[ry:ry + BOARD_ICON_SIDE, rx:rx + BOARD_ICON_SIDE]
                if roi.shape[:2] != (BOARD_ICON_SIDE, BOARD_ICON_SIDE):
                    continue
                if _roi_is_dark(roi):
                    continue
                score, eid = _match_board_icon(roi, templates)
                if eid is None or score < threshold or eid in seen_ids:
                    continue
                seen_ids.add(eid)
                equips.append({
                    "equipId": eid,
                    "name": meta.get(eid, {}).get("name"),
                    "score": round(score, 3),
                    "rect": (rx, ry, rx + BOARD_ICON_SIDE, ry + BOARD_ICON_SIDE),
                    "center": (rx + BOARD_ICON_SIDE // 2, ry + BOARD_ICON_SIDE // 2),
                })
            if equips:
                results.append({
                    "region": region,
                    "location": u.get("location"),
                    "bar": list(bar) if bar else None,
                    "unit_name": u.get("name"),
                    "equips": equips,
                })
    return results
