"""抓取编排: 解析用户给的网址/ID -> 抓接口 -> 解密棋盘 -> 分析 -> 落盘 JSON + Markdown.

用法:
    python -m scrape_comp "https://jcc.datatft.com/comp/rank#16278-33039-凯特琳-主宰"
    python -m scrape_comp 16278
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import config
from .analyze import analyze as analyze_comp
from .api import DatatftClient
from .codec import Codec
from .gamedata import GameData

_SLUG_RE = re.compile(r"(\d+)(?:-(\d+))?(?:-([^/#?]*))?")
_BAD_CHARS_RE = re.compile(r'[\\/:*?"<>|\s]+')


def parse_comp_ref(ref: str) -> dict:
    """从网址/井号串/纯 ID 里取出阵容 id 和纹章 id.

    实测网址形态: https://jcc.datatft.com/comp/rank#16278-33039-凯特琳-主宰
    井号里是 `<阵容id>-<纹章id>-<主C名>-<羁绊名>`, 站点前端只用前两段 (见 codec/README).
    """
    text = str(ref or "").strip()
    if not text:
        raise ValueError("阵容网址/ID 不能为空")
    body = text.split("#", 1)[1] if "#" in text else text
    body = body.strip("/")
    m = _SLUG_RE.match(body)
    if not m:
        raise ValueError(f"无法从 {ref!r} 解析阵容 id")
    return {
        "id": int(m.group(1)),
        "equipId": int(m.group(2)) if m.group(2) else None,
        "slugTitle": (m.group(3) or "").strip("-"),
        "input": text,
    }


def comp_dir_name(comp: dict) -> str:
    """输出目录名: `<id>-<纹章id>-<标题>` (标题取站点 title, 去掉文件名非法字符)."""
    parts = [str(comp["id"])]
    if comp.get("emblemId"):
        parts.append(str(comp["emblemId"]))
    title = _BAD_CHARS_RE.sub("", str(comp.get("title") or config.DEFAULT_SLUG_TITLE))
    parts.append(title)
    return "-".join(parts)


def scrape(ref: str, output_dir: str | None = None, season: str = config.SEASON,
           refresh_data: bool = False, write: bool = True, verbose: bool = True) -> dict:
    """主入口: 返回 {analysis, files, output_dir, ...}."""
    def log(msg):
        if verbose:
            print(f"[scrape_comp] {msg}", flush=True)

    parsed = parse_comp_ref(ref)
    root = config.output_root(output_dir)
    cache_dir = root / config.CACHE_DIR_NAME

    log(f"阵容 id={parsed['id']} (赛季 {season})")
    client = DatatftClient()
    raw = client.fetch_all(parsed["id"], season)
    strategy = raw["strategy"]
    log(f"拿到攻略: {strategy.get('title')} "
        f"(前四率 {strategy.get('top4')}%, 平均名次 {strategy.get('place')})")

    log("加载静态数据(英雄/装备/羁绊/海克斯码表)...")
    game = GameData.load(season=season, cache_dir=cache_dir, refresh=refresh_data)
    codec = Codec(game)

    analysis = analyze_comp(strategy, raw.get("extra"), raw.get("rank"),
                            game, codec, source_url=parsed["input"])
    analysis["fetchedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # 标题以站点数据为准, 目录名可能需要重算
    target = root / comp_dir_name(analysis["comp"])
    files = {"raw": target / config.FILE_RAW,
             "analysis": target / config.FILE_ANALYSIS,
             "report": target / config.FILE_REPORT}

    if write:
        target.mkdir(parents=True, exist_ok=True)
        files["raw"].write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        files["analysis"].write_text(json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8")
        from .report import render
        files["report"].write_text(render(analysis), encoding="utf-8")
        log(f"已写出: {files['report']}")

    return {
        "id": parsed["id"],
        "title": analysis["comp"]["title"],
        "season": season,
        "output_dir": str(target),
        "files": {k: str(v) for k, v in files.items()} if write else {},
        "analysis": analysis,
    }
