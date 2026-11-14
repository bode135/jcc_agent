"""CLI 入口: python -m scrape_info [--version V] [--output DIR] [--list-versions] [--no-images] [--recipes-only]

--recipes-only: 不联网, 直接用已爬到的 items.json 重新生成装备合成表 (改了模板/配方口径时用)。
"""
import argparse
import json

from .decrypt import get_version_list
from .recipes import latest_items_json, write_recipes
from .scraper import scrape


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="scrape_info",
        description="抓取 datatft.com 的棋子/装备/羁绊/热门阵容数据",
    )
    parser.add_argument("--version", help="游戏版本, 如 18.3 (默认自动检测最新)")
    parser.add_argument("--output", help="输出目录 (默认 ./workspace/scrape_info, 自动追加 v{版本} 子目录)")
    parser.add_argument("--list-versions", action="store_true", help="仅列出可用版本")
    parser.add_argument("--no-images", action="store_true", help="跳过图片下载")
    parser.add_argument("--recipes-only", action="store_true",
                        help="不联网: 用已爬取的最新 items.json 重新生成装备合成表")
    args = parser.parse_args(argv)

    if args.list_versions:
        versions = get_version_list()
        print(f"可用版本: {' '.join(versions)}")
        print(f"最新版本: {versions[0]}")
        return

    if args.recipes_only:
        items_path = latest_items_json(args.output)
        result = write_recipes(items_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    result = scrape(version=args.version, output_dir=args.output,
                    with_images=not args.no_images)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
