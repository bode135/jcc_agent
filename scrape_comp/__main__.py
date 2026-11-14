"""CLI 入口: python -m scrape_comp <网址|阵容id> [--out DIR] [--season 18] [--refresh-data] [--json]"""
import argparse
import json

from . import config
from .scraper import scrape


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="scrape_comp",
        description="抓取 datatft.com 某个阵容页的数据, 解密棋盘并生成中文分析文档",
    )
    parser.add_argument("ref", help="阵容网址(可带 #id-纹章id-名字)或纯阵容 id, 如 16278")
    parser.add_argument("--out", dest="output", help=f"输出根目录 (默认 ./{config.DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--season", default=config.SEASON, help=f"赛季 (默认 {config.SEASON})")
    parser.add_argument("--refresh-data", action="store_true", help="忽略静态数据缓存, 重新下载")
    parser.add_argument("--json", action="store_true", help="只打印分析结果的 JSON")
    args = parser.parse_args(argv)

    result = scrape(args.ref, output_dir=args.output, season=args.season,
                    refresh_data=args.refresh_data, verbose=not args.json)

    if args.json:
        print(json.dumps(result["analysis"], ensure_ascii=False, indent=2))
    else:
        print()
        print(f"阵容: {result['title']} (id={result['id']}, 赛季 {result['season']})")
        print(f"输出目录: {result['output_dir']}")
        for key, path in result["files"].items():
            print(f"  - {key}: {path}")


if __name__ == "__main__":
    main()
