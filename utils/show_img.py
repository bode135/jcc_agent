"""图片展示与中文标注 (cv2 窗口 + PIL 中文字体): 一行看图, 中文不乱码.

为什么要单独一个模块 (全是实机踩过的坑):
    1. `cv2.putText` 只认 Hershey ASCII 字体 → 中文全部渲染成 '?' (装备名/动作 JSON 里全是中文);
       所以文字统一用 PIL + 中文字体画到图上, 再交给 cv2 显示 (与 tests/manual/match_template.py、
       utils/show_rect_in_img.py 同一套路)。
    2. `cv2.imshow(winname)` 的**窗口标题**走 ANSI, 中文会乱码 → Windows 上用 user32.SetWindowTextW
       把标题重设为 Unicode (窗口名内部固定用 ASCII)。
    3. `cv2.imread/imwrite` 不支持中文路径 (Windows) → 读用 PIL, 写用 imencode + tofile。
    4. 阻塞式 `waitKey(0)` 会卡住自动化脚本 → `wait=False` 只刷一帧不阻塞 (todolist 口径:
       阻塞式入口都要能关掉)。

用法:
    from utils.show_img import show_img

    show_img("workspace/agent_debug/run_1007__133610/poll_0061_g1__134303.png")
    show_img("x.png", title="备战席 3 只", texts=[(470, 700, "格1/格2/格3 都是棋子")])
    show_img(arr_bgr, wait=False)                       # 自动化: 只 imshow 一帧, 不阻塞
    show_img(img, save_path="tempdir/标注.png")          # 顺带存图 (中文路径安全)

命令行:
    python -m utils.show_img <图片路径> [--title 标题] [--texts "x,y,文字" ...] [--no-wait] [--save out.png]
"""

import argparse
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from config.config import default_config

# 项目根走 config (2026-10-18 exe 打包: 打包后 `__file__` 在解包目录, 不能用它推项目根)。
PROJECT_ROOT = default_config.PROJECT_ROOT

# 中文字体候选: 普通体 / 粗体 (项目其它标注脚本同款: recognize/equipment.py、scripts/show_regions.py 等)
_CJK_FONT_PATHS = ("C:/Windows/Fonts/msyh.ttc",     # 微软雅黑
                   "C:/Windows/Fonts/simhei.ttf",   # 黑体
                   "C:/Windows/Fonts/simsun.ttc")   # 宋体
_CJK_BOLD_FONT_PATHS = ("C:/Windows/Fonts/msyhbd.ttc",   # 微软雅黑 粗体
                        "C:/Windows/Fonts/simhei.ttf")

_WINDOW_NAME = "utils.show_img"   # cv2 内部窗口名 (固定 ASCII; 可见标题由 _set_window_title 设)
_FONT_WARNED = False


def find_cjk_font_path(bold: bool = False) -> str | None:
    """第一个可用的中文字体文件路径; 全都没有返回 None.

    字体候选表只在这里维护: `load_cjk_font` (本模块) 与 `utils/show_rect_in_img._load_font`
    (找不到字体就回退 cv2.putText) 都复用它, 免得两处候选表各写一遍。
    """
    from PIL import ImageFont
    for path in (_CJK_BOLD_FONT_PATHS if bold else _CJK_FONT_PATHS):
        try:
            ImageFont.truetype(path, 12)   # 真正试打开一次: 文件存在但没权限/损坏也算不可用
            return path
        except OSError:
            continue
    return None


def load_cjk_font(size: int = 20, bold: bool = False):
    """加载支持中文的字体; 全部候选都不可用时退回 PIL 默认字体 (中文会变方块, 但不会崩).

    返回 PIL ImageFont, 永不返回 None —— 调用方不必再判空 (与 show_rect_in_img._load_font 的
    "None 表示回退 cv2.putText" 语义不同: 本模块统一走 PIL, 缺字体只降级字形)。
    """
    global _FONT_WARNED
    from PIL import ImageFont
    path = find_cjk_font_path(bold)
    if path is not None:
        return ImageFont.truetype(path, size)
    if not _FONT_WARNED:
        _FONT_WARNED = True
        print("[show_img] 警告: 找不到中文字体, 中文会显示成方块 (装上 msyh.ttc/simhei.ttf 即可)")
    return ImageFont.load_default()


def to_bgr(img) -> np.ndarray:
    """图片 → BGR ndarray (cv2 约定), 不改动入参.

    支持三种来源 (与 utils.show_rect_in_img 一致):
        - 路径 str/Path: 相对路径以项目根为基准; 用 PIL 读 (cv2.imread 不支持中文路径)
        - PIL Image: 按 RGB 读入后转 BGR
        - ndarray: **按 BGR 原样拷贝** (已是 cv2 约定; RGB 数组请先自己 cvtColor 或传 PIL 图)
    """
    if isinstance(img, (str, Path)):
        path = Path(img)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        try:
            pil = Image.open(path).convert("RGB")
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"图片读取失败: {path}") from exc
        return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    if isinstance(img, Image.Image):
        return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    return np.array(img, copy=True)


def imwrite_cjk(path, arr: np.ndarray) -> Path:
    """保存图片 (中文路径安全): cv2.imwrite 在 Windows 上不支持非 ASCII 路径, 用 imencode + tofile."""
    out = Path(path)
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    ext = out.suffix or ".png"
    ok, buf = cv2.imencode(ext, arr)
    if not ok:
        raise ValueError(f"图片编码失败: {out}")
    buf.tofile(str(out))
    return out


def draw_texts(arr: np.ndarray, texts, *, size: int = 20, color=(0, 215, 255),
               bold: bool = False, bg: bool = True, padding: int = 3) -> np.ndarray:
    """在 BGR 图上画**中文可读**的文字标签, 返回新数组 (不改动入参).

    texts: [(x, y, 文字), ...] 或 [(x, y, 文字, 颜色 BGR), ...]; (x, y) 为文字**左上角**。
    文字下方带黑底衬 (bg=True) 防花背景看不清; 画不了中文的 cv2.putText 只在字体不可用时兜底。
    """
    items = list(texts or [])
    if not items:
        return arr
    out = np.array(arr, copy=True)
    font = load_cjk_font(size, bold=bold)
    pil = Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    for item in items:
        x, y, text = int(item[0]), int(item[1]), str(item[2])
        rgb = tuple(reversed(item[3])) if len(item) > 3 else tuple(reversed(color))
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        w, h = right - left, bottom - top
        if bg:
            draw.rectangle((x - padding, y - padding, x + w + padding * 2, y + h + padding * 2),
                           fill=(0, 0, 0))
        draw.text((x - left, y - top), text, font=font, fill=rgb)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def _set_window_title(window: str, title: str) -> None:
    """Windows: 用 user32.SetWindowTextW 把窗口标题设成 Unicode (cv2 设的标题走 ANSI, 中文会乱码).

    按"进程 id + 当前 ASCII 标题"找到刚创建的窗口 (标题本身可能已是乱码, 不能拿中文去匹配);
    非 Windows 或失败时静默跳过 —— 标题乱码不影响看图。
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pid = os.getpid()
        matches: list = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value != pid or not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value == window or buf.value.startswith(window):
                matches.append(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
        for hwnd in matches:
            user32.SetWindowTextW(hwnd, title)
    except Exception:  # noqa: BLE001 - 标题设置失败不影响看图
        return


def show_img(img, title: str = "show_img", *, texts=None, text_size: int = 20,
             wait: bool = True, save_path=None, scale: float | None = None,
             destroy_window: bool | None = None) -> np.ndarray:
    """cv2 窗口展示图片 (**支持中文**: 图内文字走 PIL 中文字体, 窗口标题走 Unicode).

    Args:
        img: 路径 (str/Path, 相对路径按项目根) / PIL Image / ndarray (BGR)。
        title: 窗口标题 (可中文)。
        texts: 可选图内标注 [(x, y, 文字[, 颜色 BGR]), ...], 中文照常显示。
        wait: True 阻塞到按键关闭 (默认); **False 只刷一帧不阻塞** (自动化脚本用)。
        save_path: 可选落盘路径 (中文路径安全)。
        scale: 可选缩放倍率 (看放大裁剪图时用, 如 1.5)。
        destroy_window: wait 结束后是否关窗; 默认 wait=True 关、wait=False 留 (便于连续刷新同一窗口)。

    Returns:
        实际展示的 BGR ndarray (已含标注/缩放), 便于继续处理或复用。
    """
    arr = to_bgr(img)
    if scale and scale != 1:
        arr = cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    if texts:
        arr = draw_texts(arr, texts, size=text_size)
    if save_path:
        print(f"[show_img] 已保存: {imwrite_cjk(save_path, arr)}")

    cv2.namedWindow(_WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
    cv2.imshow(_WINDOW_NAME, arr)
    _set_window_title(_WINDOW_NAME, title)   # 中文标题 (Windows)
    if wait:
        print(f"[show_img] {title} —— 按任意键关闭窗口...")
        cv2.waitKey(0)
        if destroy_window is None or destroy_window:
            cv2.destroyWindow(_WINDOW_NAME)
    else:
        cv2.waitKey(1)   # 必须 pump 一次事件循环, 否则窗口不刷新
        if destroy_window:
            cv2.destroyWindow(_WINDOW_NAME)
    return arr


def _parse_text_arg(text: str):
    """CLI 用: "x,y,文字[,RRGGBB]" → (x, y, 文字[, (B,G,R)])."""
    parts = text.split(",", 3)
    if len(parts) < 3:
        raise ValueError(f"--texts 需要 x,y,文字[,RRGGBB]: {text!r}")
    x, y, msg = int(parts[0]), int(parts[1]), parts[2]
    if len(parts) == 4:
        rgb = parts[3].strip().lstrip("#")
        r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
        return (x, y, msg, (b, g, r))
    return (x, y, msg)


def main() -> None:
    parser = argparse.ArgumentParser(description="cv2 看图 (支持中文标注/标题)")
    parser.add_argument("img", help="图片路径 (相对路径按项目根)")
    parser.add_argument("--title", default="show_img", help="窗口标题 (可中文)")
    parser.add_argument("--texts", action="append", default=None, metavar="X,Y,文字[,RRGGBB]",
                        help="图内标注, 可重复: --texts 470,700,备战席格1")
    parser.add_argument("--text-size", type=int, default=20)
    parser.add_argument("--scale", type=float, default=None, help="缩放倍率, 如 1.5")
    parser.add_argument("--save", default=None, help="另存标注图路径")
    parser.add_argument("--no-wait", action="store_true", help="只刷一帧不阻塞 (自动化)")
    args = parser.parse_args()

    texts = [_parse_text_arg(t) for t in (args.texts or [])]
    # 一次性 CLI: 不阻塞时也把窗口关掉, 免得在桌面上留一个没人管的窗口
    show_img(args.img, args.title, texts=texts, text_size=args.text_size,
             wait=not args.no_wait, save_path=args.save, scale=args.scale,
             destroy_window=True)


if __name__ == "__main__":
    main()
