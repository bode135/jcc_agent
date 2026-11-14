#!/usr/bin/env python
"""被动观察器 (T4 交付): 只截图 / 识别 / 打印 / 绘制, **绝不点击、绝不按键**.

用途: 调试绘制与决策消息用 —— 把这一帧"看到的东西"打成一行行中文决策消息, 同时把识别结果
实时画到 overlay 透明窗口上 (棋子框+名字+星级 / 血条 / 小小英雄 / 装备栏 / 棋子携带装备 /
奖励球 / 顶栏一行状态)。

纯图色方案: 只依赖截图像素 + 识别模块, 不读游戏进程内存, 不含任何 input/mouse 调用
(本文件全文没有 control.* 的导入, 也从不下发点击)。

两种模式:
    离线  --img tests/images/board_ingame.png   → 识别一帧 + 打印 + 落 PNG 到 tempdir/ 验收
    实时  (默认)                               → 循环截图 + 打印 + 画到 overlay 窗口

分项降频 (1s 一帧的预算下, 实测热态耗时): YOLO 棋子 7ms / 血条+星数 51ms / 奖励球 18ms /
小小英雄 7ms / 阶段 25-48ms 每帧; 装备栏 ~500ms 2s 一次; 棋子携带装备 ~700ms 3s 一次;
整图 OCR(回合/等级/经验/金币/商店) ~2.0s 5s 一次 (其间按本地走秒推算剩余时间)。

用法:
    python -X utf8 -u -m scripts.overlay_info --img tests/images/board_ingame.png
    python -X utf8 -u -m scripts.overlay_info --interval 1.0 --seconds 10
    python -X utf8 -u -m scripts.overlay_info --json --no-overlay --seconds 5
"""

from __future__ import annotations

# 直接运行支持: `python scripts/overlay_info.py ...` 与 `python -m scripts.overlay_info ...` 等价。
if __package__ in (None, ""):
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image

from config.config import default_config
from overlay import debug_overlay, live_draw

TARGET_SIZE = (1920, 1080)

# 阶段 -> 中文 (识别侧 phase.detect 的 phase 值)
PHASE_CN = {"prep": "准备", "combat": "战斗", "augment": "海克斯", "draft": "选秀",
            "unknown": "非对局"}

# 各识别项的重算间隔 (秒): 按实测热态耗时定, 详见模块 docstring
# 只在"对局画面"里有意义的识别项。大厅/登录/选英雄等非对局画面上跑这些,
# 会把界面美术当目标 (实测大厅 K/DA 补给站帧刷出 23 个假球、满地假球框)。
# 判据用 phase.detect: 只有 prep / combat / augment / draft 才算对局。
IN_GAME_ONLY = {"units", "bars", "balls", "yolo_balls", "legend", "heroes",
                "equip_bar", "board_equips", "game", "augment"}
IN_GAME_PHASES = {"prep", "combat", "augment", "draft"}


def _in_game(phase_state) -> bool:
    """phase 状态 -> 是否对局画面; 还没算过 phase 时保守返回 True (先算一轮再说)。"""
    if not phase_state:
        return True
    ph = (phase_state or {}).get("phase")
    if ph is None:
        return True
    return str(ph) in IN_GAME_PHASES


ITEM_INTERVAL = {
    "units": 0.5,        # YOLO 棋子 (7ms)
    "bars": 0.5,         # 血条/星数/小小英雄名牌 (51ms)
    "balls": 0.5,        # 奖励球(颜色法) (18ms)
    "yolo_balls": 0.5,   # 奖励球(YOLO 球权重, 常为空) (9ms)
    "legend": 0.5,       # 小小英雄(YOLO legend 权重) (7ms)
    "heroes": 0.5,       # 小小英雄(等级数字块+红绿小血段) (8~18ms, 用户 m01362)
    "phase": 0.5,        # 阶段 (25-48ms 热态)
    "equip_bar": 2.0,    # 装备栏 (~500ms)
    "board_equips": 3.0, # 棋子携带装备 (~700ms)
    "game": 5.0,         # 整图 OCR: 回合/剩余/等级/经验/金币/商店 (~2.0s)
    "augment": 2.5,      # 海克斯卡 (~1.2s, 只在海克斯阶段算)
}

# 绘制类别 -> config 的 OVERLAY_SHOW_* 键 (debug_overlay.draw_frame 的 show dict)
DRAW_KEYS = {
    "units": "OVERLAY_SHOW_UNITS",
    "bars": "OVERLAY_SHOW_BLOOD_BAR",
    "balls": "OVERLAY_SHOW_BALLS_COLOR",
    "yolo_balls": "OVERLAY_SHOW_BALLS_YOLO",   # 同时画 legends(小小英雄)
    "shop": "OVERLAY_SHOW_SHOP",
    "equip": "OVERLAY_SHOW_EQUIP_BAR",
    "bequips": "OVERLAY_SHOW_BOARD_EQUIPS",
    "heroes": "OVERLAY_SHOW_HERO",
}


def _ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000.0, 1)


def _cell_of(unit: dict) -> str:
    """棋子位置的人读标签 (用户口径 m01029): 我方 `r1~r4c1~c7`, 敌方 `-r1~-r4`, 备战席 `slot{N}`。

    绝对行 (`location["row"]`, 1..8 从远到近) 只在几何运算里用, 这里交给
    `recognize.grid.cell_label` 翻译——**和源项目 `utils/unit_cell.py` 的口径一致**:
    己方第 1 行 = 自己最底那排 (绝对行 8), 敌方 `-r1` = 对面最靠中线那排 (绝对行 4)。
    """
    from recognize.grid import cell_label
    loc = unit.get("location") or {}
    if loc.get("slot"):
        return f"slot{int(loc['slot'])}"
    row, col = loc.get("row"), loc.get("column")
    if row and col:
        return cell_label(int(row), int(col))
    return "-"


def _pos_of_center(center) -> str:
    """任意点 -> 人读格位标签 (棋盘 `r1c1`, 备战席 `slot5`); 不在棋盘/备战席内返回 ""。

    走 `recognize.grid` 的正式几何 (棋盘先试, 再试备战席), 供"只有 center、
    没有 location 字段"的场景使用 (如 `extract_board_equips` 的分组)。
    """
    from recognize.grid import board_position, bench_position, cell_label
    if not center:
        return ""
    try:
        cell = board_position(tuple(center))
        return cell_label(int(cell["row"]), int(cell["column"]))
    except Exception:
        pass
    try:
        slot = bench_position(tuple(center))
        return f"slot{int(slot)}"
    except Exception:
        return ""


def _stars_label(stars) -> str:
    return ("★" * int(stars)) if stars else "-"


class Observer:
    """一帧帧跑识别并汇总成"决策消息 + 画框数据" (token: 只观察, 不操作)."""

    def __init__(self, draw_show: dict | None = None) -> None:
        self.cache: dict = {}       # 各识别项的最近结果
        self.ts: dict = {}          # 各识别项上次重算时刻 (monotonic)
        self.errors: dict = {}      # 各识别项最近一次的错误文本
        self.frame = 0
        self.last_cost = 0.0
        self.last_fresh: list[str] = []
        self._remain_at: tuple[int, float] | None = None   # (剩余秒, 采信时刻)
        self.draw_show = draw_show

    # ------------------------------------------------------------------ #
    # 分项调度
    # ------------------------------------------------------------------ #
    def _enabled(self, key: str) -> bool:
        """augment 只在海克斯阶段算 (其它阶段跑一次 OCR 白烧 1.2s)."""
        if key == "augment":
            ph = (self.cache.get("phase") or {}).get("phase")
            return ph == "augment"
        return True

    def due(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        out = []
        for key, iv in ITEM_INTERVAL.items():
            if not self._enabled(key):
                continue
            if now - float(self.ts.get(key, -1e9)) >= iv:
                out.append(key)
        # 阶段先算: augment 是否启用/画什么取决于阶段 (不让海克斯 OCR 浪费在非海克斯帧)
        out.sort(key=lambda k: 0 if k == "phase" else 1)
        return out

    def _run(self, key: str, img: Image.Image) -> None:
        """跑一项识别; 失败只记 errors, 不抛."""
        t0 = time.monotonic()
        try:
            value = self._compute(key, img)
        except Exception as exc:  # noqa: BLE001 - 单项失败不许打断整帧
            self.errors[key] = f"{type(exc).__name__}: {exc}"
            self.ts[key] = time.monotonic()
            return
        self.errors.pop(key, None)
        if value is not None:
            self.cache[key] = value
        if key == "game":
            g = (value or {}).get("game") or {}
            rt = g.get("round_time")
            if isinstance(rt, int) and rt >= 0:
                self._remain_at = (rt, time.monotonic())
        if key == "augment" and value is not None:
            self.cache[key] = value
        self.ts[key] = time.monotonic()

    def _compute(self, key: str, img: Image.Image):
        if key == "units":
            from recognize import yolo_units
            return yolo_units.extract_units(img)
        if key == "bars":
            # match_cells=set(): 跳过"全棋子库多尺度头像匹配"(单只 145ms, 9 只 1.3s),
            # 名字留给 YOLO (7ms 就给全名), 这里只要 血条框/星数/格位/小小英雄名牌。
            from recognize import board
            return board.extract_units(img, include_legend=True, match_cells=set())
        if key == "balls":
            from recognize import reward_ball
            return reward_ball.find_balls(img)
        if key == "yolo_balls":
            from recognize import yolo_units
            yolo_units.predict(img, model=yolo_units.ball_onnx_path())
            return yolo_units.last_balls()
        if key == "legend":
            from recognize import yolo_units
            yolo_units.predict(img, model=yolo_units.legend_onnx_path())
            return yolo_units.last_legends()
        if key == "heroes":
            from recognize import hero
            return hero.detect(img)
        if key == "phase":
            from recognize import phase
            return phase.detect(img)
        if key == "equip_bar":
            from recognize import equipment
            return equipment.extract_equipment(img)
        if key == "board_equips":
            from recognize import equipment
            return equipment.extract_board_equips(img, units=self.cache.get("bars"))
        if key == "game":
            from extract_info import extract as extract_all
            timings: dict = {}
            errs: dict = {}
            res = extract_all(img, only={"game", "shop"}, timings=timings, errors=errs)
            out = {"game": {r["key"]: r["value"] for r in res.get("game") or []},
                   "shop": res.get("shop") or [],
                   "timings": timings}
            if errs:
                out["errors"] = errs
            return out
        if key == "augment":
            from recognize import augment
            return augment.extract(img)
        raise KeyError(key)

    def refresh(self, img: Image.Image, force: bool = False) -> dict:
        """跑这一帧到点的识别项, 返回汇总状态 (见 `state`)."""
        keys = list(ITEM_INTERVAL) if force else self.due()
        if force:
            self.errors.clear()
            keys.sort(key=lambda k: 0 if k == "phase" else 1)
        # 非对局画面 (大厅/登录/选英雄/结算) 只跑 phase, 其余项跳过并把缓存清空,
        # 免得把界面美术画成球/棋子 (实测大厅帧 23 个假球)。
        if not _in_game(self.cache.get("phase")):
            skipped = [k for k in keys if k in IN_GAME_ONLY]
            keys = [k for k in keys if k not in IN_GAME_ONLY]
            for k in skipped:
                self.cache[k] = None if k in ("game", "augment") else []
            if skipped:
                print(f"[!] 非对局画面: 跳过 {len(skipped)} 项 ({', '.join(skipped)})",
                      flush=True)
        t0 = time.monotonic()
        for key in keys:
            self._run(key, img)
        self.last_cost = round(time.monotonic() - t0, 3)
        self.last_fresh = keys
        self.frame += 1
        return self.state()

    # ------------------------------------------------------------------ #
    # 汇总
    # ------------------------------------------------------------------ #
    def _remain(self) -> int | None:
        """回合剩余秒数: 最近一次 OCR 值 + 本地走秒 (OCR 5s 一次, 其间不许冻住)."""
        if self._remain_at is None:
            return None
        base, t_at = self._remain_at
        return max(0, int(base - (time.monotonic() - t_at)))

    def state(self) -> dict:
        ph = self.cache.get("phase") or {}
        game_snap = self.cache.get("game") or {}
        game = game_snap.get("game") or {}
        shop = game_snap.get("shop") or []
        raw_units = self.cache.get("units") or {"board": [], "bench": []}
        bars = self.cache.get("bars") or {"board": [], "bench": []}
        # 星数来自血条条纹 -> 按最近中心贴到 YOLO 棋子 (YOLO 不给星, 血条不给名字)
        units = live_draw.merge_stars({"units": raw_units, "bars": bars})
        legend_units = [{"name": u.get("name") or "小小英雄", "box": u.get("bar"),
                         "center": u.get("center"), "stars": None}
                        for area in ("board", "bench") for u in (bars.get(area) or [])
                        if u.get("legend")]
        return {
            "frame": self.frame,
            "cost": self.last_cost,
            "fresh": list(self.last_fresh),
            "errors": dict(self.errors),
            "phase": ph,
            "phase_cn": PHASE_CN.get(ph.get("phase"), ph.get("phase") or "?"),
            "stage": game.get("stage"),
            "remain": self._remain(),
            "level": game.get("level"),
            "xp": game.get("xp"),
            "gold": game.get("gold"),
            "shop": shop,
            "xp_buy_cost": game.get("xp_buy_cost"),
            "refresh_cost": game.get("refresh_cost"),
            "free_refresh": game.get("free_refresh"),
            "board": units.get("board") or [],
            "bench": units.get("bench") or [],
            "legends": [x for x in (self.cache.get("legend") or [])] + legend_units,
            "legend_yolo": [x for x in (self.cache.get("legend") or [])],
            "legend_bars": legend_units,
            "balls": [b for b in (self.cache.get("balls") or [])],
            "balls_yolo": [b for b in (self.cache.get("yolo_balls") or [])],
            "equip_bar": [e for e in (self.cache.get("equip_bar") or [])],
            "board_equips": [g for g in (self.cache.get("board_equips") or [])],
            "augment": self.cache.get("augment") or {},
            "heroes": [h for h in (self.cache.get("heroes") or [])],
        }

    # ------------------------------------------------------------------ #
    # 打印
    # ------------------------------------------------------------------ #
    def header_text(self, s: dict) -> str:
        """顶栏一行: 回合 剩余秒 阶段 Lv 金币 (如「3-6 剩余20s 准备 Lv5 金币103」)."""
        stage = s.get("stage") or "?-?"
        remain = s.get("remain")
        remain_txt = f"{remain}s" if remain is not None else "?s"
        lv = s.get("level")
        lv_txt = f"Lv{lv}" if lv is not None else "Lv?"
        gold = s.get("gold")
        gold_txt = f"金币{gold}" if gold is not None else "金币?"
        return f"{stage} 剩余{remain_txt} {s.get('phase_cn') or '?'} {lv_txt} {gold_txt}"

    def message(self, s: dict) -> str:
        """一帧决策消息 (多行中文; 不含 emoji)."""
        lines = []
        fresh = ",".join(s.get("fresh") or []) or "-"
        lines.append(f"[frame {s['frame']:03d} | 本帧耗时 {s['cost']:.2f}s | 本帧重算: {fresh}]")
        xp = s.get("xp")
        xp_txt = f"{xp[0]}/{xp[1]}" if isinstance(xp, (list, tuple)) and len(xp) >= 2 else "-"
        g = s.get("game_hint") or ""
        lines.append(
            f" 阶段 {s.get('phase_cn') or '?'}  回合 {s.get('stage') or '?'}  "
            f"剩余 {s.get('remain') if s.get('remain') is not None else '?'}s  "
            f"Lv{s.get('level') if s.get('level') is not None else '?'} (经验 {xp_txt})  "
            f"金币 {s.get('gold') if s.get('gold') is not None else '?'}  "
            f"买经验 {s.get('xp_buy_cost') if s.get('xp_buy_cost') is not None else '?'}  "
            f"刷新 {s.get('refresh_cost') if s.get('refresh_cost') is not None else '?'}  "
            f"免费刷新 {s.get('free_refresh') if s.get('free_refresh') is not None else '?'}"
        )
        shop = s.get("shop") or []
        if shop and all((x.get("name") is None and x.get("price") is None) for x in shop):
            # 海克斯/战斗遮罩期底部商店栏被盖住, 窄带+整图 OCR 都读不到 -> 如实说"未识别到"
            shop = []
        if shop:
            items = []
            for i, sl in enumerate(shop, 1):
                nm = sl.get("name") or "?"
                pr = sl.get("price")
                items.append(f"[{i}]{nm}{(' ' + str(pr) + '金') if pr is not None else ''}")
            lines.append(" 商店 " + " | ".join(items))
        else:
            lines.append(" 商店 未识别到 (被遮罩/空商店)")

        def _units_line(title: str, us: list, with_cell: bool) -> str:
            if not us:
                return f" {title} 0 只"
            parts = []
            for u in us:
                nm = u.get("name") or "?"
                cell = _cell_of(u) if with_cell else ""
                tail = f" {cell}" if cell else ""
                sc = u.get("score")
                sc_txt = f" ({float(sc):.2f})" if sc is not None else ""
                parts.append(f"{nm}{tail} {_stars_label(u.get('stars'))}{sc_txt}")
            return f" {title} {len(us)} 只: " + " | ".join(parts)

        lines.append(_units_line("棋盘", s.get("board") or [], True))
        lines.append(_units_line("备战席", s.get("bench") or [], True))

        hs = s.get("heroes") or []
        if hs:
            parts = []
            for h in hs:
                who = "我方" if h.get("kind") == "mine" else "敌方"
                lv = h.get("level")
                pos = "(%.0f,%.0f)" % (h.get("x") or 0, h.get("y") or 0)
                parts.append(f"{who}Lv{lv if lv is not None else '?'} {pos}")
            lines.append(f" 小小英雄 {len(hs)} 处 (等级块法, 右侧小血段判敌我): " + " | ".join(parts))

        legs = s.get("legends") or []
        # 英雄路 (等级块法) 已经报过的同一只英雄, 老路 (YOLO/名牌) 不再重复报:
        # 两路的框中心相距 <40px 就认为是同一只 (用户 m01362 要求以等级块法为准)。
        _hboxes = [h.get("numblk") for h in hs if h.get("numblk")]
        if _hboxes:
            _keep = []
            for lg in legs:
                b = lg.get("box") or lg.get("rect")
                if not b:
                    _keep.append(lg)
                    continue
                cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
                dup = False
                for nb in _hboxes:
                    nx, ny, nw, nh = nb[:4]
                    if ((cx - (nx + nw / 2.0)) ** 2 + (cy - (ny + nh / 2.0)) ** 2) ** 0.5 < 40:
                        dup = True
                        break
                if not dup:
                    _keep.append(lg)
            legs = _keep
        if legs:
            boxes = []
            for lg in legs:
                b = lg.get("box") or lg.get("rect")
                boxes.append(str([int(v) for v in b]) if b else "-")
            yolo_n = len(s.get("legend_yolo") or [])
            bar_n = len(s.get("legend_bars") or [])
            tag = "旧路备查" if hs else "小小英雄"
            lines.append(f" {tag} {len(legs)} 处 (YOLO {yolo_n} / 血条名牌 {bar_n}): "
                         + " | ".join(boxes))
        elif not hs:
            lines.append(" 小小英雄 未识别到")

        eq = s.get("equip_bar") or []
        if eq:
            parts = []
            for e in eq:
                nm = e.get("name") or "?"
                cnt = e.get("count")
                parts.append(f"{e.get('slot') or '?'} {nm + ('x' + str(cnt) if cnt and cnt > 1 else '')}")
            lines.append(f" 装备栏 {len(eq)} 槽: " + " | ".join(parts))
        else:
            lines.append(" 装备栏 未识别到")

        beq = s.get("board_equips") or []
        if beq:
            parts = []
            for grp in beq:
                loc = grp.get("location") or {}
                nm = grp.get("unit_name")
                if not nm:
                    from recognize.grid import cell_label
                    if loc.get("slot"):
                        where = f"slot{int(loc['slot'])}"
                    elif loc.get("row") and loc.get("column"):
                        # 绝对行 (1..8) -> 人读标签: 我方 r1~r4 / 敌方 -r1~-r4
                        where = cell_label(int(loc["row"]), int(loc["column"]))
                    else:
                        bar = grp.get("bar") or ()
                        ctr = None
                        if len(bar) == 4:
                            ctr = ((bar[0] + bar[2]) / 2.0, (bar[1] + bar[3]) / 2.0 + 105)
                        where = _pos_of_center(ctr) or "未命名单位"
                    nm = f"{grp.get('region') or '?'} {where}"
                eqs = "、".join(e.get("name") or "?" for e in grp.get("equips") or [])
                parts.append(f"{nm}: {eqs}")
            lines.append(f" 棋子携带装备 {len(beq)} 组: " + " | ".join(parts))
        else:
            lines.append(" 棋子携带装备 无 (本帧未识别到)")

        bl = s.get("balls") or []
        if bl:
            cnt = {}
            for b in bl:
                c = b.get("color") or "?"
                cnt[c] = cnt.get(c, 0) + 1
            cn = {"silver": "银", "blue": "蓝", "gold": "金"}
            parts = [f"{cn.get(k, k)} {v}" for k, v in cnt.items()]
            lines.append(f" 奖励球 {len(bl)} 个: " + " | ".join(parts))
        else:
            lines.append(" 奖励球 0 个")

        aug = s.get("augment") or {}
        if aug and aug.get("is_augment"):
            cards = aug.get("cards") or []
            lines.append(f" 海克斯 {len(cards)} 张: "
                         + " | ".join(f"[{(c.get('name') or '?')}]" for c in cards))
        if s.get("errors"):
            errs = "; ".join(f"{k}={v}" for k, v in s["errors"].items())
            lines.append(f"[!] 识别项出错 (该项本轮沿用缓存): {errs}")
        return "\n".join(lines)

    def json_line(self, s: dict) -> str:
        """一帧 JSON (一行)."""
        out = {
            "frame": s["frame"],
            "cost": s["cost"],
            "fresh": s["fresh"],
            "errors": s["errors"],
            "stage": s["stage"],
            "phase": (s.get("phase") or {}).get("phase"),
            "phase_cn": s["phase_cn"],
            "remain_time": s["remain"],
            "level": s["level"],
            "xp": s["xp"],
            "gold": s["gold"],
            "shop": [{"name": x.get("name"), "price": x.get("price")} for x in s["shop"]],
            "board": [{"name": u.get("name"), "stars": u.get("stars"),
                       "cell": _cell_of(u), "score": u.get("score"),
                       "box": u.get("box")} for u in s["board"]],
            "bench": [{"name": u.get("name"), "stars": u.get("stars"),
                       "slot": _cell_of(u), "score": u.get("score"),
                       "box": u.get("box")} for u in s["bench"]],
            "legends": [x.get("box") for x in s["legends"]],
            "heroes": [{"kind": h.get("kind"), "level": h.get("level"),
                         "level_score": h.get("level_score"),
                         "center": [round(h.get("x") or 0), round(h.get("y") or 0)],
                         "numblk": [int(v) for v in h.get("numblk") or []]}
                        for h in (s.get("heroes") or [])],
            "equip_bar": [{"slot": e.get("slot"), "name": e.get("name"),
                           "count": e.get("count"), "rect": e.get("rect")}
                          for e in s["equip_bar"]],
            "board_equips": [{"region": g.get("region"), "unit_name": g.get("unit_name"),
                              "equips": [e.get("name") for e in g.get("equips") or []]}
                             for g in s["board_equips"]],
            "balls": [{"color": b.get("color"), "center": b.get("center")} for b in s["balls"]],
            "augment": {"is_augment": (s.get("augment") or {}).get("is_augment"),
                        "cards": [c.get("name") for c in
                                  ((s.get("augment") or {}).get("cards") or [])]},
        }
        return json.dumps(out, ensure_ascii=False)

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def show_dict(self) -> dict:
        """默认全画; 传了 draw_show (--show 列表/--show-xxx 开关) 就以它为唯一依据."""
        if not self.draw_show:
            return {v: True for v in DRAW_KEYS.values()}
        show = {v: False for v in DRAW_KEYS.values()}
        show.update(self.draw_show)
        return show

    def draw(self, s: dict, size=TARGET_SIZE) -> Image.Image:
        """把汇总状态画成透明画布 (棋子框+名字+星级 / 血条 / 小小英雄 / 装备 / header)."""
        return debug_overlay.draw_frame(
            size,
            units={"board": s["board"], "bench": s["bench"]},
            bars={"board": self._bars_area("board"), "bench": self._bars_area("bench")},
            legends=[{"name": x.get("name") or "小小英雄", "box": x.get("box")}
                     for x in s.get("legends") or []],
            balls_yolo=s.get("balls_yolo") or [],
            balls_color=s.get("balls") or [],
            shop=s.get("shop") or [],
            equip_bar=s.get("equip_bar") or [],
            board_equips=s.get("board_equips") or [],
            heroes=s.get("heroes") or [],
            show=self.show_dict(),
            header=self.header_text(s),
        )

    def _bars_area(self, area: str) -> list:
        return (self.cache.get("bars") or {}).get(area) or []


# --------------------------------------------------------------------------- #
# 图片 / 窗口工具
# --------------------------------------------------------------------------- #
def load_image(path: str | Path) -> Image.Image:
    """读图并归一到 1920×1080 (识别口径固定 1080p)."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(default_config.PROJECT_ROOT) / p
    if not p.exists():
        raise FileNotFoundError(f"图片不存在: {p}")
    img = Image.open(p).convert("RGB")
    if img.size != TARGET_SIZE:
        img = img.resize(TARGET_SIZE, Image.LANCZOS)
    return img


def save_pngs(overlay_img: Image.Image, src: Image.Image | None, out_dir: Path,
              stem: str) -> list[Path]:
    """落 PNG: 透明 overlay 图 + (有原图时)叠原图的对照图, 方便验收."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    p1 = out_dir / f"overlay_info_{stem}.png"
    overlay_img.save(p1)
    paths.append(p1)
    if src is not None:
        base = src.convert("RGBA")
        mix = Image.alpha_composite(base, overlay_img)
        p2 = out_dir / f"overlay_info_{stem}_mix.png"
        mix.convert("RGB").save(p2)
        paths.append(p2)
    return paths


def _no_window_hint(ld_index) -> None:
    print("[!] 没有找到雷电模拟器窗口"
          f"(ld_index={ld_index!r})。本机可能没装雷电模拟器或没开游戏窗口。")
    print("[!] 实时模式需要窗口; 离线验收请用: "
          "python -X utf8 -u -m scripts.overlay_info --img tests/images/board_ingame.png")


# --------------------------------------------------------------------------- #
# 离线 / 实时
# --------------------------------------------------------------------------- #
def run_offline(args) -> int:
    ov = Observer(draw_show=_resolve_show(args))
    img = load_image(args.img)
    s = ov.refresh(img, force=True)
    if not args.no_print:
        print(ov.message(s))
    if args.json:
        print(ov.json_line(s))
    stem = Path(args.img).stem
    if not args.no_overlay:
        frame = ov.draw(s, size=img.size)
        paths = save_pngs(frame, img, default_config.TEMP_ROOT, stem)
        print("[OK] 绘制 PNG: " + ", ".join(str(p) for p in paths))
    return 0


def run_live(args) -> int:
    from capture import screen as cs

    target = debug_overlay.resolve_target(args.ld_index)
    if target is None:
        _no_window_hint(args.ld_index)
        return 2
    print(f"[OK] 目标窗口: hwnd={target.get('render') or target.get('main')} "
          f"{target.get('w')}x{target.get('h')} @({target.get('left')},{target.get('top')})")

    overlay_ok = False
    if not args.no_overlay:
        overlay_ok = debug_overlay.start(args.ld_index)
        if not overlay_ok:
            print("[!] overlay 窗口启动失败 (贴附/建窗没成功), 本模式只打印决策消息")
    ov = Observer(draw_show=_resolve_show(args))
    t_start = time.monotonic()
    frames = 0
    try:
        while True:
            if args.seconds and (time.monotonic() - t_start) >= args.seconds:
                break
            t0 = time.monotonic()
            try:
                img = cs.capture_game()
            except Exception as exc:  # noqa: BLE001
                print(f"[X] 截图失败: {type(exc).__name__}: {exc}")
                print("[!] 请确认游戏窗口在前台/未最小化; 本工具只截图不做任何操作")
                return 2
            s = ov.refresh(img)
            if not args.no_print:
                print(ov.message(s))
            if overlay_ok:
                try:
                    debug_overlay.set_frame(ov.draw(s, size=img.size))
                except Exception as exc:  # noqa: BLE001
                    print(f"[X] 贴图失败: {type(exc).__name__}: {exc}")
                    overlay_ok = debug_overlay.is_active()
            frames += 1
            dt = time.monotonic() - t0
            if args.interval > dt:
                time.sleep(args.interval - dt)
    except KeyboardInterrupt:
        print(f"\n[OK] 已停止 (共 {frames} 帧)")
    finally:
        debug_overlay.stop()
    print(f"[OK] 实时模式结束: {frames} 帧")
    return 0


def _parse_show(text: str) -> dict:
    """--show units,bars,shop → show dict (未列出的不画)."""
    want = {x.strip() for x in (text or "").split(",") if x.strip()}
    bad = want - set(DRAW_KEYS)
    if bad:
        raise SystemExit(f"--show 有未知类别 {sorted(bad)}; 可选: {','.join(DRAW_KEYS)}")
    show = {v: (k in want) for k, v in DRAW_KEYS.items()}
    return show


def _resolve_show(args) -> dict:
    """--show 列表 + 各 --show-xxx 单开关 -> 一份 show dict (给 draw_frame)."""
    flags = {k: getattr(args, "show_" + k, None) for k in DRAW_KEYS}
    if getattr(args, "show", None):
        show = _parse_show(args.show)
    elif any(flags.values()):
        # 用户只点了某些 --show-xxx: 底子全 False, 点谁开谁 (不是"默认全画再叠加")
        show = {v: False for v in DRAW_KEYS.values()}
    else:
        show = {v: True for v in DRAW_KEYS.values()}
    for key, on in flags.items():
        if on:
            show[DRAW_KEYS[key]] = True
    return show


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts.overlay_info",
        description="被动观察器: 只截图/识别/打印/绘制, 不点击不按键 (T4)")
    p.add_argument("--ld-index", type=int,
                   default=getattr(default_config, "OVERLAY_LD_INDEX", None),
                   help="雷电模拟器序号 (默认取 config.OVERLAY_LD_INDEX)")
    p.add_argument("--img", default=None,
                   help="离线模式: 对这张图识别一帧 (落 PNG 到 tempdir/ 便于验收)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="实时模式每帧间隔秒 (默认 1.0)")
    p.add_argument("--seconds", type=float, default=0.0,
                   help="实时模式运行时长秒 (默认 0 = 一直跑, Ctrl+C 结束)")
    p.add_argument("--json", action="store_true", help="每帧额外打一行 JSON")
    p.add_argument("--no-overlay", action="store_true",
                   help="不建 overlay 窗口/不落 PNG (只打印决策消息)")
    p.add_argument("--no-print", action="store_true", help="不打印决策消息 (只绘制)")
    p.add_argument("--show", default=None,
                   help=f"只画这些类别 (逗号分隔, 可选: {','.join(DRAW_KEYS)}; 默认全画)")
    # 与任务书一致的逐项开关: 指定了哪个就把哪个打开 (配合 --show 只画某几类)
    for key in DRAW_KEYS:
        p.add_argument("--show-" + key.replace("_", "-"), dest="show_" + key,
                       action="store_const", const=True, default=None,
                       help=f"画 {key} (等价 --show 里含 {key})")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    if args.img:
        return run_offline(args)
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
