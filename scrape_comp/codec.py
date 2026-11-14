"""teamUrl 密文编解码: 复刻 jcc.datatft.com 前端 intent-wheel 的 `encryptInit` / `decrypt`.

密文长这样 (站点 team-builder 分享链接的尾部):
    DBI3BwBtCCJAqKAiLCIOAuVBEXAKZAF3BcBjBYbAR3BeBVBV|KFLvKtLoJNGkHoHvFUs18
    └────────────── 棋子段 ──────────────┘ └── 海克斯段 ──┘ └ 赛季后缀

棋子段每个棋子的字段顺序 (都是定长/看首字符决定):
    1 字符  位置码   map0~map55 -> 0..55  (56 格 = 8 行 × 7 列; 站点只取前 28 格当我方棋盘)
    2 字符  英雄码   hero<chessId>        (静态数据里英雄自带的 code, 没有则 "z"+字母 兜底)
    1 字符  装备件数 (数字 0-9), 紧跟 N×2 字符的装备码 equip<equipId>   [可省略]
    "~"     强化果实: 1 字符件数 + 每件 1 字符方位(L/R/T/B) + 2 字符 powerup 码   [可省略]
    "*"     星级: 1 字符数字                                                  [可省略]
    "_"     变形棋子: 2 字符英雄码 -> replaceId                                 [可省略]
    "^"     额外羁绊: 1 字符 36 进制下标 -> 表内羁绊名, 可重复                      [可省略]
    "-"     主C标记(占 2 字符)                                                [可省略]

海克斯段在 "|" 之后, 是若干 2 字符 hex 码, 全段以 2 字符为一组直到字符串结束
(赛季后缀 s18 / s17 已在入口处剥掉).
"""
from __future__ import annotations

import re
import urllib.parse

from . import config
from .gamedata import GameData

_URL_PREFIX_RE = re.compile(r"https://(?:www|jcc)\.datatft\.com/(?:team-builder|simulator)/", re.I)
_SEASON_SUFFIX_RE = re.compile(r"s(\d{1,2})$")

# 额外羁绊表 (intent-wheel 里的 tl, 下标是 36 进制)
EXTRA_TRAITS = [
    "TFT15_Destroyer", "TFT15_Juggernaut", "TFT15_Duelist", "TFT17_ManaTrait",
    "TFT17_ASTrait", "TFT17_APTrait", "DA_18_Blossom", "DA_18_Inferno",
    "DA_18_Elderwood", "DA_18_Blackthorn", "DA_18_Coven", "DA_18_Fae",
    "DA_Primal18", "DA_18_Solar", "DA_18_Lunar", "DA_18_Eclipse",
    "DA_18_Slayer", "DA_18_Rapidfire", "DA_18_Spellweaver", "DA_18_Executioner",
]

_POWERUP_SIDES = {"L": "left", "R": "right", "T": "top", "B": "bottom"}


class Codec:
    """按静态数据构建"码 -> 值"表, 并提供 teamUrl 解密."""

    def __init__(self, game: GameData):
        self.game = game
        self.decode_map: dict[str, object] = {}
        self._build()

    # ---------- 建表 ----------
    def _build(self):
        ce = self.decode_map
        season = self.game.season
        letters = config.LETTERS

        for i in range(config.MAP_SLOTS):
            ce["map" + config.MAP_CHARS[i]] = i

        counter = 0
        for hero in self.game.heroes:
            code = hero.get("code")
            if not code:
                code = "z" + letters[counter]
                counter += 1
            ce["hero" + code] = hero.get("chessId")

        equips = list(self.game.equips)
        if str(season) == "11":
            ordered = equips
        else:
            skip = set(config.EQUIP_CODE_SKIP)
            tail = set(config.EQUIP_CODE_TAIL)
            ordered = [e for e in equips if e.get("equipId") not in skip]
            ordered += [e for e in equips if e.get("equipId") in tail]
        for i, equip in enumerate(ordered):
            row = int(str(i / 52).split(".")[0])          # 复刻 parseInt((c/52).toString())
            code = letters[row] + letters[i - row * 52]
            if equip.get("code"):
                code = equip["code"]
            ce["equip" + code] = equip.get("equipId")

        for group in self.game.hex_groups:
            for h in group:
                ce["hex" + str(h.get("code"))] = h.get("hexId")

        for a in self.game.anomalies:
            ce["powerup" + str(a.get("code"))] = a.get("key")

    # ---------- 解密 ----------
    def decode(self, team_url: str) -> dict:
        """把 teamUrl 解成 {slots[56], hexs[], errors[]}; slots 元素为 None 或 dict."""
        text = _URL_PREFIX_RE.sub("", team_url or "")
        text = text.replace("%7C", "|").replace(" ", "")
        if text.endswith("."):
            text = text[:-1]
        m = _SEASON_SUFFIX_RE.search(text)
        if m:
            text = text[: -(len(m.group(1)) + 1)]
        text = urllib.parse.unquote(text)

        slots: list[dict | None] = [None] * config.MAP_SLOTS
        hexs: list[str] = []
        errors: list[str] = []
        i, n = 0, len(text)

        while i < n:
            if text[i] == "|":
                i += 1
                while i < n:
                    code = text[i:i + 2]
                    hex_id = self.decode_map.get("hex" + code)
                    if hex_id is None:
                        errors.append(f"海克斯解码失败: {code}")
                    else:
                        hexs.append(hex_id)
                    i += 2
                break

            pos_code = text[i]
            i += 1
            pos = self.decode_map.get("map" + pos_code)
            if pos is None:
                errors.append(f"位置码解码失败: {pos_code} (下标 {i - 1})")
                break

            hero_code = text[i:i + 2]
            i += 2
            chess_id = self.decode_map.get("hero" + hero_code)
            if chess_id is None:
                errors.append(f"英雄码解码失败: {hero_code}")
                break

            item = {"id": chess_id, "index": pos}

            if i < n and text[i].isdigit():
                count = int(text[i])
                i += 1
                equips = []
                for _ in range(count):
                    code = text[i:i + 2]
                    i += 2
                    equip_id = self.decode_map.get("equip" + code)
                    if equip_id is None:
                        errors.append(f"装备码解码失败: {code}")
                        continue
                    equip = self.game.equip(equip_id)
                    equips.append(equip or {"equipId": equip_id})
                item["equips"] = equips

            if i < n and text[i] == "~":
                i += 1
                count = int(text[i])
                i += 1
                sides = {"left": [], "right": [], "top": [], "bottom": []}
                for _ in range(count):
                    side = _POWERUP_SIDES.get(text[i])
                    i += 1
                    code = text[i:i + 2]
                    i += 2
                    key = self.decode_map.get("powerup" + code)
                    if side is None or key is None:
                        errors.append(f"强化果实解码失败: {code}")
                    else:
                        sides[side].append(key)
                item["powerups"] = sides

            if i < n and text[i] == "*":
                i += 1
                item["stars"] = int(text[i])
                i += 1

            if i < n and text[i] == "_":
                i += 1
                code = text[i:i + 2]
                i += 2
                item["replaceId"] = self.decode_map.get("hero" + code)

            while i < n and text[i] == "^":
                i += 1
                ch = text[i]
                i += 1
                try:
                    idx = int(ch, 36)
                except ValueError:
                    continue
                if 0 <= idx < len(EXTRA_TRAITS):
                    item.setdefault("extraTraits", []).append(EXTRA_TRAITS[idx])

            if text[i:i + 1] == "-":
                i += 2                      # 站点是 i++, i++ (标记占 2 字符)
                item["isMain"] = True

            item["hero"] = self.game.hero(chess_id)
            slots[pos] = item

        return {"slots": slots, "hexs": hexs, "errors": errors}


def board_positions(slots: list, rows: int = config.BOARD_ROWS,
                    cols: int = config.BOARD_COLS) -> list[dict]:
    """把 56 格数组的前 rows*cols 格转成棋盘坐标 (行 1 在最前, 列 1 在最左)."""
    out = []
    for index, item in enumerate(slots[: rows * cols]):
        if not item:
            continue
        row, col = divmod(index, cols)
        out.append({**item, "row": row + 1, "col": col + 1,
                    "zone": "前排" if row + 1 in config.FRONT_ROWS else "后排",
                    "lcr": ("左" if col < cols / 3 else "右" if col >= cols * 2 / 3 else "中")})
    return out


def ascii_board(slots: list, rows: int = config.BOARD_ROWS, cols: int = config.BOARD_COLS) -> str:
    """生成便于核对/贴文档的 ASCII 棋盘 (第 1 行是最前排)."""
    cells = {}
    for index, item in enumerate(slots[: rows * cols]):
        if not item:
            continue
        name = (item.get("hero") or {}).get("displayName") or str(item.get("id"))
        cells[divmod(index, cols)] = name
    width = max((len(v) for v in cells.values()), default=2) * 2 + 2
    lines = []
    for r in range(rows):
        lines.append("|".join(cells.get((r, c), "").center(width) for c in range(cols)))
    return "\n".join(lines)
