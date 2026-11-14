"""小小英雄定位 —— 等级数字块 + 右侧一小段红/绿血条（纯图形学, 8~18ms/帧）。

为什么要独立一路（用户 m01362/m01364/m01368 的口径）：

- 小小英雄的「血条」不是一个整条, 而是**等级数字块右边一点点**的绿色(我方) 或
  红色(敌方) 小段。所以按"找血条 -> 看血条上方有没有名牌"的老办法找它, 既容易
  被聊天条/好友头像满足（假阳性），战斗帧名牌又常被遮住（假阴性）。
- 反过来锚定就稳: 先找**白色等级数字块**（尺寸 + 暗底双筛）→ 再看它右侧那一小段
  是红是绿 → 命中即 hero, 顺便把"几级 / 敌我"一起读出来。
- 判据实现全在 `recognize/draft_hero.py`（移植自父项目, 阈值都是实测标定过的,
  别乱调）。本模块只做两件事: 把整帧检测裁到**对局区域**(避开顶栏/底栏/右侧玩家
  列表的 UI 数字与头像血条), 以及给 overlay/抽取层一个统一的返回结构。

用法::

    from recognize.hero import detect
    heroes = detect(img)          # PIL Image 或 ndarray 均可
    # -> [{"x","y","kind","level","level_score","numblk","rgb","score"}]

`kind`: `"mine"` 我方 / `"enemy"` 敌方; `level`: 等级数字(读不到模板时 None);
`(x, y)`: 英雄落点(数字块中心 + config 里的 `DRAFT_HERO_LEVEL_DX/DY` 偏移)。
"""

from __future__ import annotations

import numpy as np

from recognize import draft_hero

#: 对局区域 (x1, y1, x2, y2) —— 只在棋盘/备战席范围内认英雄。
#: 顶栏(y<250: 回合/装备/金币 HUD)、底栏(y>820: 商店/经验条)、右侧玩家列表
#: (x>1550: 8 行头像+血条) 都会产生"白数字 + 红条"的假候选, 必须排除。
GAME_REGION = (150, 250, 1550, 820)

#: 数字块边长闸 (像素) —— 英雄等级数字实测 11×16 ~ 12×17, 留一点余量。
BLK_MIN_SIDE = 6
BLK_MAX_SIDE = 30


def _to_rgb(img) -> np.ndarray:
    """PIL Image / ndarray -> RGB ndarray (本项目内部帧统一是 RGB)。"""
    if isinstance(img, np.ndarray):
        arr = img
        if arr.ndim == 2:
            return np.stack([arr] * 3, axis=-1)
        if arr.shape[2] == 4:
            return arr[:, :, :3]
        return arr
    return np.asarray(img.convert("RGB"))


def _center(blk) -> tuple[float, float]:
    x, y, w, h = blk
    return x + w / 2.0, y + h / 2.0


def detect(img, *, region: tuple[int, int, int, int] | None = None,
           max_x: int | None = None) -> list[dict]:
    """一帧 -> 小小英雄列表 (已裁到对局区域 + 按距离去重)。

    Args:
        img: PIL Image(1920×1080 口径) 或 RGB ndarray。
        region: 识别区域, 默认 `GAME_REGION`; 传 `None` 时不裁。
        max_x: 只认 x 小于它的候选; `None` 用 config 的
            `DRAFT_HERO_SEG_MAX_X`(默认 1600)。

    Returns:
        `[{x, y, kind, level, level_score, numblk, rgb, score}]`, 按 (y, x) 排序。
    """
    rgb = _to_rgb(img)
    reg = GAME_REGION if region is None else region
    x1, y1, x2, y2 = reg
    H, W = rgb.shape[:2]
    x1, y1 = max(0, int(x1)), max(0, int(y1))
    x2, y2 = min(W, int(x2)), min(H, int(y2))
    if x2 <= x1 or y2 <= y1:        # 区域退化 -> 整帧
        sub = rgb
        ox, oy = 0, 0
    else:
        sub = rgb[y1:y2, x1:x2]
        ox, oy = x1, y1
    out: list[dict] = []
    for it in draft_hero.hero_blocks(sub, max_x=max_x):
        blk = it["numblk"]
        w, h = blk[2], blk[3]
        if not (BLK_MIN_SIDE <= w <= BLK_MAX_SIDE and BLK_MIN_SIDE <= h <= BLK_MAX_SIDE):
            continue
        cx, cy = _center(blk)
        hx, hy = draft_hero.hero_xy(blk)
        item = dict(it)
        item["numblk"] = (int(blk[0] + ox), int(blk[1] + oy), int(w), int(h))
        item["x"] = round(float(hx + ox), 1)
        item["y"] = round(float(hy + oy), 1)
        item["cx"] = round(float(cx + ox), 1)
        item["cy"] = round(float(cy + oy), 1)
        out.append(item)
    out.sort(key=lambda u: (u["y"], u["x"]))
    return out


def mine(heroes: list[dict]) -> list[dict]:
    """只留我方英雄 (落点最靠下的那一个通常是自己的; 这里只做过滤, 不做选择)。"""
    return [u for u in heroes if u.get("kind") == "mine"]
