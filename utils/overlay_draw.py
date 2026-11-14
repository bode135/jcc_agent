# -*- coding: utf-8 -*-
"""通用悬浮框绘制工具 —— 在雷电模拟器游戏画面上实时叠加"框 / 点 / 线 / 文字"。

## 这是什么
项目里 `overlay/debug_overlay.py` 已经实现了一套**分层透明置顶窗口**
（`WS_EX_LAYERED|TRANSPARENT|TOPMOST|TOOLWINDOW|NOACTIVATE` + `UpdateLayeredWindow`
预乘 alpha 原子贴图，不注入、不 hook、不抢键鼠）。但它只提供按固定分类
（棋子/商店/装备栏/球/GT…）取数绘制的 `draw_frame`。

本模块把"窗口 + 贴图"复用过来（直接调 `overlay.debug_overlay.start/set_frame/stop`），
再抽出一个**与业务无关的通用绘制入口**：给一组 rect / 点 / 线 / 标签，就画在游戏画面上。
矩形框+描边文字的画法与 `debug_overlay._stroke` / `utils.draw_regions` 的 `d.rectangle` 同款。

## 典型用法
    from utils.overlay_draw import OverlayDraw
    ov = OverlayDraw(ld_index=0)
    if ov.start():
        ov.draw(
            boxes=[{"rect": (100, 200, 180, 260), "label": "球-蓝", "color": (90, 200, 255)}],
            points=[{"center": (960, 540), "r": 18, "label": "点击点", "color": (255, 80, 80)}],
            header="奖励球 3 个",
        )
        ...
        ov.stop()

## 只想要一张图（离线看效果 / 单测，不起窗口）
    from utils.overlay_draw import render_frame
    img = render_frame((1920, 1080), boxes=[...], points=[...], header="示例")
    img.save("tempdir/overlay_demo.png")

## 数据结构
    boxes:  [{"rect":(x1,y1,x2,y2) 或 "box", "label":str, "color":(r,g,b) 或 (r,g,b,a),
              "width":int, "below":bool}]
    points: [{"center":(x,y), "r":int, "label":str, "color":..., "filled":bool}]
    lines:  [{"pts":[(x,y),...], "color":..., "width":int}]
    坐标一律 **1920x1080 基准**（与项目其它绘制/识别一致）；贴图时若目标客户区尺寸不同会自动缩放。

## 边界（与 debug_overlay 一致）
    - 纯外部 Win32 分层窗口，**不注入、不 hook、不抢前台键鼠**；找不到雷电 RenderWindow 时 start() 返回 False。
    - 任何绘制/提交失败都**吞掉不抛**（装饰功能，绝不影响调用方主流程）。
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from overlay import debug_overlay


def _font():
    """加载可显示中文的字体 (与 debug_overlay 同源)。"""
    try:
        from utils.show_img import load_cjk_font
        return load_cjk_font(14, bold=True)
    except Exception:  # noqa: BLE001 - 兜底: 没字体就默认字体, 绝不因字体崩
        try:
            from PIL import ImageFont
            return ImageFont.load_default()
        except Exception:  # noqa: BLE001
            return None


def _rgba(c):
    """颜色补齐 alpha=255 (RGBA 画布上 3 元组某些 Pillow 版本会报错)。"""
    c = tuple(c)
    return c if len(c) == 4 else (c[0], c[1], c[2], 255)


def _as_box(b):
    """把 (x1,y1,x2,y2) / [x,y,w,h] 统一成 (x1,y1,x2,y2) 整数元组; 坏数据返回 None。"""
    if not b or len(b) < 4:
        return None
    try:
        vals = [float(v) for v in b[:4]]
        if b is not None and len(b) >= 4 and vals[2] > vals[0] and vals[3] > vals[1]:
            return tuple(int(round(v)) for v in vals)
    except Exception:  # noqa: BLE001
        return None
    return None


def render_frame(size, *, boxes=None, points=None, lines=None, header=None) -> Image.Image:
    """纯函数: 在**全透明**的 size 画布上画 boxes/points/lines/header, 返回 PIL RGBA 图。

    不依赖窗口, 可离线单测 / 存图看效果。坐标为 1920x1080 基准。
    """
    w, h = int(size[0]), int(size[1])
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pen = ImageDraw.Draw(canvas)
    font = _font()

    def stroke(rect, color, label: str = "", below: bool = False, width: int = 3) -> int:
        """画一个矩形框 (+可选描边文字标签)。这是全项目通用的 rect 画法。"""
        box = _as_box(rect)
        if box is None:
            return 0
        x1, y1, x2, y2 = box
        pen.rectangle([x1, y1, x2, y2], outline=_rgba(color), width=max(1, int(width)))
        if label:
            ty = y2 + 1 if below else (y1 - 17 if y1 >= 17 else y1 + 1)
            ty = max(0, min(ty, h - 4))
            pen.text((max(0, x1), ty), label, fill=_rgba(color), font=font,
                     stroke_width=2, stroke_fill=(0, 0, 0, 255))
        return 1

    for b in boxes or []:
        rect = b.get("rect") if isinstance(b, dict) else b
        if isinstance(b, dict):
            stroke(rect if rect is not None else b.get("box"), b.get("color", (90, 200, 255)),
                   b.get("label") or "", b.get("below", False), b.get("width", 3))
        else:
            stroke(rect, (90, 200, 255))

    for p in points or []:
        if not isinstance(p, dict):
            continue
        c = p.get("center")
        if not c or len(c) < 2:
            continue
        x, y = float(c[0]), float(c[1])
        r = int(p.get("r", 12))
        col = _rgba(p.get("color", (255, 120, 60)))
        if p.get("filled", True):
            pen.ellipse([x - r, y - r, x + r, y + r], fill=col, outline=(10, 10, 10, 255), width=2)
        else:
            pen.ellipse([x - r, y - r, x + r, y + r], outline=col, width=2)
        if p.get("label"):
            pen.text((int(x - r), int(y + r) + 2), p["label"], fill=col, font=font,
                     stroke_width=2, stroke_fill=(0, 0, 0, 255))

    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        pts = ln.get("pts") or []
        if len(pts) < 2:
            continue
        pen.line([(float(a[0]), float(a[1])) for a in pts],
                 fill=_rgba(ln.get("color", (255, 255, 0))), width=int(ln.get("width", 2)))

    if header:
        pen.text((10, 8), header, fill=_rgba((170, 170, 170)), font=font,
                 stroke_width=2, stroke_fill=(0, 0, 0, 255))
    return canvas


class OverlayDraw:
    """悬浮框绘制器: 封装分层窗口生命周期 + 每次把 boxes/points/lines 画上去。

    线程语义: start() 起后台窗口线程; draw() 可在任意线程调用 (内部 PostMessage 交给窗口线程贴图);
    stop() 收窗口。找不到目标窗口时 start() 返回 False, 之后所有 draw() 静默失败。
    """

    def __init__(self, ld_index=None):
        self.ld_index = ld_index
        self._target = None

    def start(self) -> bool:
        ok = False
        try:
            ok = debug_overlay.start(self.ld_index)
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            try:
                self._target = debug_overlay.resolve_target(self.ld_index)
            except Exception:  # noqa: BLE001
                self._target = None
        return ok

    def size(self) -> tuple:
        t = self._target
        if not t:
            try:
                t = debug_overlay.resolve_target(self.ld_index)
                self._target = t
            except Exception:  # noqa: BLE001
                t = None
        return (int(t["w"]), int(t["h"])) if t else (1920, 1080)

    def draw(self, boxes=None, points=None, lines=None, header=None) -> bool:
        try:
            frame = render_frame(self.size(), boxes=boxes, points=points, lines=lines, header=header)
            return bool(debug_overlay.set_frame(frame))
        except Exception:  # noqa: BLE001 - 装饰功能, 吞掉
            return False

    def clear(self) -> bool:
        """清空 (提交一张全透明画布)。"""
        return self.draw()

    def is_active(self) -> bool:
        return debug_overlay.is_active()

    def stop(self, wait: float = 2.0) -> None:
        try:
            debug_overlay.stop(wait)
        except Exception:  # noqa: BLE001
            pass


def _demo(out_path=None):
    """离线 demo: 生成一张示例画布 (不起窗口), 便于肉眼核对绘制是否正确。"""
    img = render_frame(
        (1920, 1080),
        boxes=[{"rect": (900, 380, 1000, 480), "label": "球-蓝 0.91", "color": (90, 200, 255)},
               {"rect": (1100, 300, 1180, 380), "label": "球-银", "color": (220, 220, 220), "below": True}],
        points=[{"center": (960, 540), "r": 18, "label": "点击点", "color": (255, 80, 80)}],
        lines=[{"pts": [(900, 430), (960, 540), (1140, 340)], "color": (0, 240, 255), "width": 2}],
        header="[overlay_draw demo] 框 + 点 + 线 + 中文标签",
    )
    path = out_path or "tempdir/overlay_draw_demo.png"
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)
    print("[overlay_draw] demo 已存", path)
    return path


if __name__ == "__main__":
    _demo()
