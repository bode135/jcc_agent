"""星级徽记探针: 打印每根血条左侧徽记的色相/几何/白像素, 用来标定 1★/2★/3★ 区间。

何时用
------
* 换分辨率/换渲染清晰度后, 想确认 `recognize/board.py::BADGE_COLORS` 还准不准;
* 拿到 **3★ 金色徽记**的真实帧 (本工程测试集里没有), 用本工具读出金徽记的 H/S/V 分布,
  再回来微调 `BADGE_COLORS` 里 3 星那一档;
* 排查"某只棋子没检出": 看它的血条左侧到底有没有徽记 (没有就说明血条本身是误检)。

用法
----
    python -X utf8 -u tools\\probe_star_badge.py --img tests/images/board_ingame.png
    python -X utf8 -u tools\\probe_star_badge.py --img a.png --img b.png --crop

`--crop` 会把每根血条左侧的窗口放大 8 倍拼成一张 PNG 落到 tempdir/, 便于肉眼看徽记。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np                                       # noqa: E402
import cv2                                               # noqa: E402
from PIL import Image                                    # noqa: E402

from recognize import board, phase                       # noqa: E402


def _stats(arr, bbox) -> str:
    x, y, w, h = bbox
    sub = cv2.cvtColor(arr[y:y + h, x:x + w], cv2.COLOR_RGB2HSV)
    H, S, V = sub[..., 0].astype(int), sub[..., 1].astype(int), sub[..., 2].astype(int)
    sat = S >= 90
    hmed = int(np.median(H[sat])) if sat.any() else -1
    return (f"box=({x},{y},{w},{h}) 白={board.badge_white_count(arr, bbox)} "
            f"Hmed={hmed} Smed={int(np.median(S))} Vmed={int(np.median(V))}")


def probe(path: Path, crop: bool) -> int:
    img = phase.normalize_1080(Image.open(path).convert("RGB"))
    arr = np.array(img)
    res = board.extract_units(img, include_legend=True)
    print(f"== {path.name}  血条={len(res['board']) + len(res['bench'])} 根")
    windows = []
    for area in ("board", "bench"):
        for u in res[area]:
            stars, bbox = board.star_badge(arr, u["bar"])
            legend = bool(u.get("legend"))
            print(f"   {area:<5} bar={str(u['bar']):<22} 徽记星级={stars} legend={legend} "
                  f"{_stats(arr, bbox) if bbox else '无徽记(会被 BAR_REQUIRE_BADGE 丢弃)'}")
            x1, y1, x2, y2 = u["bar"]
            windows.append(((x1 - 40, y1 - 20, x2 + 8, y2 + 20), f"{area} {u['bar']}"))
    if crop and windows:
        sheet = Image.new("RGB", (len(windows) * 300, 320), (16, 16, 16))
        from PIL import ImageDraw
        d = ImageDraw.Draw(sheet)
        for i, (box, label) in enumerate(windows):
            c = img.crop(box)
            c = c.resize((c.width * 6, c.height * 6), Image.LANCZOS)
            sheet.paste(c, (i * 300, 0))
            d.text((i * 300 + 2, 300), label, fill=(255, 255, 255))
    if crop and windows:
        sheet = Image.new("RGB", (len(windows) * 300, 320), (16, 16, 16))
        from PIL import ImageDraw
        d = ImageDraw.Draw(sheet)
        for i, (box, label) in enumerate(windows):
            c = img.crop(box)
            c = c.resize((c.width * 6, c.height * 6), Image.LANCZOS)
            sheet.paste(c, (i * 300, 0))
            d.text((i * 300 + 2, 300), label, fill=(255, 255, 255))
        out = ROOT / "tempdir" / f"badge_probe_{path.stem}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out)
        print(f"   裁图 -> {out}")

    st = board.last_stats()
    print(f"   [diagnostic] 无徽记丢弃={len(st.get('dropped_no_badge') or [])} 根 "
          f"(require_badge={st.get('require_badge')})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="星级徽记探针 (标定 1/2/3 星颜色区间)")
    ap.add_argument("--img", action="append", required=True, help="图片路径, 可重复")
    ap.add_argument("--crop", action="store_true", help="把血条左侧窗口放大拼图落到 tempdir/")
    args = ap.parse_args(argv)
    for p in args.img:
        probe(Path(p), args.crop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
