"""scrape_info: datatft.com 棋子/装备/羁绊/热门阵容数据抓取模块."""
from .decrypt import get_latest_version, get_version_list
from .recipes import build_recipes, latest_items_json, render_markdown, write_recipes
from .scraper import scrape

__all__ = [
    "scrape", "get_version_list", "get_latest_version",
    "build_recipes", "render_markdown", "write_recipes", "latest_items_json",
]
