"""scrape_comp: datatft.com 单个阵容(strategy)页的数据抓取 + 棋盘解密 + 分析文档生成.

与 scrape_info 的分工:
    scrape_info  抓全站基础库 (棋子/装备/羁绊/热门阵容榜单)
    scrape_comp  抓某个具体阵容页, 解出棋盘站位/装备, 并整理成"能照着打"的文档
"""
from .analyze import analyze as analyze_comp
from .api import DatatftClient
from .codec import Codec, ascii_board, board_positions
from .gamedata import GameData
from .report import render
from .scraper import parse_comp_ref, scrape

__all__ = [
    "scrape", "parse_comp_ref", "analyze_comp", "render",
    "DatatftClient", "GameData", "Codec", "board_positions", "ascii_board",
]
