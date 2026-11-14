"""scrape_comp 模块配置: datatft.com 阵容(strategy)页抓取与解密相关的全部可调变量.

数据来源链 (2026-10 实测, 见 dev_docs/architecture.md "阵容数据" 一节):

    用户给的网页  https://jcc.datatft.com/comp/rank#16278-33039-凯特琳-主宰
      │ 外层是 iframe 壳: /comp/rank -> /service/tool/intent-wheel.html#/comps/a/<slug>
      ▼
    API  https://api.datatft.com
      ├─ POST /team/strategy      {"id": 16278}   -> 运营思路/出装/阵容码/teamUrl
      ├─ POST /team/strategy/extra{"id": 16278}   -> 实战装备统计(每个弈子每件装备的 top4/名次)
      ├─ POST /team/rank          {"season":"18"} -> 该阵容的评级/主C(heroId)/三星与成装
      └─ POST /team/list          {"version":...} -> 阵容榜(需要版本号, 仅列出榜单时用)
      ▼
    棋盘 teamUrl  https://jcc.datatft.com/team-builder/<密文>
      └─ 自研码表解码 (见 codec.py), 得 56 格 mapHeroItems + mapHexs(强化符文)
      ▼
    静态数据  https://static.datatft.com/tft-base/languages/cn.<hash>.json
      └─ base64 + AES-128-ECB 解密, 得 heros18/equips18/hexs18/races18/jobs18
         (码表的英雄码/装备码/海克斯码就直接取自这份数据, 必须与站点同源)
"""
from pathlib import Path

# ---------- 站点 / API / 静态数据 ----------
SITE_BASE = "https://jcc.datatft.com"
TOOL_PAGE = SITE_BASE + "/service/tool/intent-wheel.html"  # 阵容页真正的 iframe 页面
API_BASE = "https://api.datatft.com"
# 静态数据 URL 带构建哈希, 会随站点发版变化: 先按 TOOL_PAGE 动态发现, 失败才用下面的兜底值
LANG_DATA_URL_FALLBACK = "https://static.datatft.com/tft-base/languages/cn.5a44dbc087214f60.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30      # 单次 HTTP 请求超时(秒), 静态数据 2MB 左右用长超时
LANG_REQUEST_TIMEOUT = 120

# AES 密钥与 scrape_info 完全一致 (站点 data-cn / tft-base 数据集共用), 因此直接复用其解密函数
AES_KEY_B64 = "dGZ0ZGFpYURleEVhbVZhag=="

# ---------- 赛季 ----------
SEASON = "18"
SEASONS = ("18", "17", "11")  # jcc 站可选赛季 (intent-wheel 的 dt 列表, 11 仅 www 站)
DATA_LANG = "cn"              # 静态数据语言 (cn 才有中文名)

# ---------- 输出 ----------
DEFAULT_OUTPUT_DIR = "workspace/scrape_comp"  # 相对当前工作路径
CACHE_DIR_NAME = "_cache"                     # 静态数据缓存(2MB, 避免每次重下)
CACHE_FILE_NAME = "game_data_cn.json"
FILE_RAW = "raw.json"           # 原始 API 响应 (strategy/extra/rank)
FILE_ANALYSIS = "analysis.json"  # 结构化分析结果
FILE_REPORT = "阵容分析.md"      # 人读文档

# ---------- 码表 (复刻 intent-wheel 的 encryptInit) ----------
# 位置码: 0~55 共 56 格 = 8 行 × 7 列, 前 28 格(4 行)是我方棋盘
MAP_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!<>."   # 56
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"         # 52, 生成装备码/兜底英雄码用
MAP_SLOTS = 56
# 装备码表在生成前会做一次重排: 先剔掉这些, 再把 31004~31007 追加到末尾
EQUIP_CODE_SKIP = (612, 545, 573, 31004, 31005, 31006, 31007, 2252)
EQUIP_CODE_TAIL = (31004, 31005, 31006, 31007)
# 各赛季追加的"野怪/召唤物"假棋子(静态数据里没有, 需按站位顺序拼兜底码)
SPECIAL_HEROES = {
    "10": [9977, 9978, 9979],
    "12": [9977, 9978, 912999],
    "13": [9977, 9978, 913999, 913998],
    "14": [9977, 9978, 914999],
    "15": [9977, 9978, 915999, 1000003],
    "16": [9977, 9978, 9999, 916998],
    "17": [9977, 9978, 917997, 917998, 917999, 917996, 917995],
    "18": [9977, 9978, 999001, 999002, 999003, 999004],
}
SPECIAL_HERO_NAMES = {  # 兜底棋子中文名 (取自 intent-wheel 的 co() 映射)
    9995: "雕像", 9988: "黑默丁格", 9989: "小炮", 9990: "璐璐", 9987: "人马", 9986: "扎克",
    9985: "霸龙", 9984: "炮台", 9983: "男爵", 9982: "先锋", 9981: "虚空鱼", 9980: "触手",
    9977: "目标假人", 9978: "魔像", 9991: "机械甲虫", 9992: "机械巨熊", 9993: "机械巨龙",
    9979: "迪斯科灯", 911010: "凯尔", 912999: "悠米", 913999: "锻炉", 913998: "赛恩",
    914999: "T-43X", 915999: "超级机甲", 1000003: "婕拉植物", 916997: "厄塔汗",
    916998: "冰封之塔", 917995: "海魔至尊", 917996: "黑洞", 917997: "羊咩咩",
    917998: "羊咩咩 & 咩咩羊", 917999: "慎-圣物",
    # 注意: 18 赛季的 999001~999004 在站点自己的名称表里也没有名字, 这里留空,
    # 由 gamedata 统一兜底成 "未命名(99900X)", 文档里会说明这一点, 不要凭空编名字.
}

# ---------- 棋盘几何 ----------
BOARD_ROWS = 4   # 我方棋盘行数 (mapHeroItems 只取前 28 格)
BOARD_COLS = 7
FRONT_ROWS = (1, 2)   # 前排(靠敌方)
BACK_ROWS = (3, 4)    # 后排(我方底线)

# ---------- 装备功能分类关键词 (匹配 equips18 的 effect 字段) ----------
SHRED_ARMOR_KEYWORDS = ("护甲击碎",)      # 破甲
SHRED_MAGIC_KEYWORDS = ("魔抗击碎",)      # 破魔抗
WOUND_KEYWORDS = ("重伤",)                # 重伤(减疗)
# 有用的装备大类: common=成装, guangming=光明版, aoen=奥恩神器, zhuanzhi*=纹章
USABLE_EQUIP_TYPES = ("common", "guangming", "aoen", "zhuanzhi", "zhuanzhiJgg", "zhuanzhi2")

# ---------- 难度文案 (站点 i18n: label.difficultyN) ----------
DIFFICULTY_TEXT = {1: "简单", 2: "适中", 3: "困难"}

# ---------- 报告用 ----------
SITE_NAME = "datatft.com (金铲铲大数据)"
DEFAULT_SLUG_TITLE = "comp"


def output_root(output_dir: str | None = None) -> Path:
    """输出根目录: 传参优先, 否则 当前工作路径/config.DEFAULT_OUTPUT_DIR."""
    return Path(output_dir) if output_dir else Path.cwd() / DEFAULT_OUTPUT_DIR
