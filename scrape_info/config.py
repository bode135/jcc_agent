"""scrape_info 模块配置: datatft.com 数据抓取的全部可调变量."""
from pathlib import Path

# ---------- 站点 / API / CDN ----------
SITE_BASE = "https://www.datatft.com"
API_BASE = "https://api.datatft.com"
CDN_BASE = "https://static.datatft.com/images"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30  # 单次 HTTP 请求超时(秒)

# ---------- 静态数据集解密 ----------
# assets/data-cn-*.js 为 AES-128-ECB 加密, base64 key 解码为 tftdaiaDexEamVaj
AES_KEY_B64 = "dGZ0ZGFpYURleEVhbVZhag=="

# 静态数据集用到的 key
DATASET_HERO_KEY = "heros18"
DATASET_EQUIP_KEY = "equips18"
DATASET_RACE_KEY = "races18"
DATASET_JOB_KEY = "jobs18"
DATASET_CHARM_KEY = "charms18"  # 仙灵法杖 (database#charm 页签, 字段 title/effect/cost/upgrade)

# ---------- 抓取参数 ----------
DEFAULT_VERSION = None  # None = 自动检测最新版本(bootstrap chunk 版本数组首元素)
SEASON = "18"           # team/list 的 season 参数
TIME_RANGE_DAYS = 5     # team/list 的 time 参数(最近 N 天)
TIER_PRIORITY = ["challenger", "grandmaster", "diamond"]  # 阵容数据源依次尝试
LIST_TIER = "diamond"   # explore/list 的 tier 参数(推荐装备用)

# ---------- 输出 ----------
DEFAULT_OUTPUT_DIR = "workspace/scrape_info"  # 相对当前工作路径
APPEND_VERSION_SUBDIR = True                  # 输出目录下追加 v{版本} 子目录
VERSION_PREFIX = "v"
DIR_CHESS_INFO = "chess_info"
DIR_COMPS = "comps"
DIR_IMAGE_UNITS = "images/units"
DIR_IMAGE_ITEMS = "images/items"

# 装备合成表 (recipes.py): 结构化配方 JSON + 人读文档, 都写进 DIR_CHESS_INFO
FILE_ITEM_RECIPES = "item_recipes.json"
FILE_RECIPE_DOC = "装备合成表.md"

# 图片扩展名: 棋子 heros/default/{chessId}.jpg, 装备 equips/{equipId}.png
IMAGE_EXT_UNIT = ".jpg"
IMAGE_EXT_ITEM = ".png"

# ---------- 特殊棋子: 拉克丝变体(共用基础卡图 chessId 918099) ----------
LUX_CHESS_ID = 918099
# 变体 key -> 中文名(经静态数据 races18/jobs18 的 key-name 对应关系验证)
LUX_VARIANTS = {
    "DA_18_Lux_Inferno": "拉克丝 地狱火",
    "DA_18_Lux_Elderwood": "拉克丝 永恒之森",
    "DA_Lux18_Blackthorn": "拉克丝 黑荆棘",
    "DA_Lux18_Blossom": "拉克丝 灵魂莲华",
    "DA_18_Lux_Coven": "拉克丝 魔女",
    "DA_18_Lux_Fae": "拉克丝 花仙子",
    "DA_18_Lux_Primal": "拉克丝 野兽之灵",
    "DA_18_Lux_Sunbeam": "拉克丝 日蚀骑士",
    "DA_18_Lux_Moonbeam": "拉克丝 月蚀骑士",
    "DA_Lux18_Base": "拉克丝",
}
