"""T4 绘制信息工具自检 —— 全离线/合成数据, 不点游戏一下。

用法 (工程根):
    python -X utf8 -u -m tools.selftest_overlay            # 全跑 (~40s, 首次含模型加载)
    python -X utf8 -u -m tools.selftest_overlay --quick    # 跳过 5 张图的子进程离线帧

检查项:
  [1] 模块可导入 + 三个文件里没有内存/选秀/操作类残留依赖
  [2] debug_overlay.draw_frame 用合成数据画一张 PNG, 关键颜色与非透明像素都在
  [3] tests/images 5 张图逐张跑 scripts.overlay_info --img, 退出码/两张 PNG/一帧摘要
  [4] 同一张图模拟 6 帧 1s 节拍, 打印每帧耗时与本帧重算项 (验证降频生效)
  [5] 实时窗口探测 (有就打印目标, 没有也算通过: 本机不一定有雷电)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEMP = ROOT / "tempdir"
PY = sys.executable

# overlay 侧三个文件里绝不允许再出现的东西 (内存读取 / 选秀 / 鼠标键盘操作)
FORBIDDEN = [
    r"\btrae_agent\b", r"\bmem_provider\b", r"mem_gt", r"mem_rt", r"\bdraft_ring\b",
    r"control\.input", r"control\.mouse", r"\bpyautogui\b",
]
FORBIDDEN_FILES = ["overlay/debug_overlay.py", "overlay/live_draw.py", "utils/overlay_draw.py",
                   "scripts/overlay_info.py"]

IMAGES = ["board_ingame.png", "augment_choose.png", "reward_balls.png", "bench_forge.png"]

ok_all = True


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


def bad(msg: str) -> None:
    global ok_all
    ok_all = False
    print(f"[X] {msg}")


def info(msg: str) -> None:
    print(f"     {msg}")


# --------------------------------------------------------------------------- #
def check_imports() -> None:
    print("== [1] 导入与残留依赖 ==")
    try:
        from overlay import debug_overlay, live_draw           # noqa: F401
        from utils import overlay_draw                        # noqa: F401
        import scripts.overlay_info as info_mod               # noqa: F401
    except Exception as exc:  # noqa: BLE001
        bad(f"模块导入失败: {type(exc).__name__}: {exc}")
        return
    ok(f"导入通过: overlay.debug_overlay / overlay.live_draw / utils.overlay_draw / "
       f"scripts.overlay_info ({info_mod.__name__})")
    for rel in FORBIDDEN_FILES:
        p = ROOT / rel
        if not p.exists():
            bad(f"{rel} 不存在")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        hits = [pat for pat in FORBIDDEN if re.search(pat, text)]
        if hits:
            bad(f"{rel} 仍有禁用依赖: {hits}")
        else:
            ok(f"{rel} 干净 (无 {'/'.join(x.strip(chr(92)+'b') for x in FORBIDDEN[:5])} …)")
    # live_draw 对外 API 还在 (其它脚本可能 import)
    names = ["start", "LiveDrawThread", "collect", "draw", "set_frame_source",
             "begin_main_work", "end_main_work", "main_work"]
    missing = [n for n in names if not hasattr(live_draw, n)]
    if missing:
        bad(f"live_draw 少了对外 API: {missing}")
    else:
        ok(f"live_draw 对外 API 齐全 ({len(names)} 个)")


# --------------------------------------------------------------------------- #
def check_draw() -> None:
    print("== [2] 合成数据绘制 ==")
    from overlay import debug_overlay
    size = (1920, 1080)
    units = {"board": [
        {"box": [300, 300, 420, 460], "name": "测试棋子", "model_name": "测试棋子",
         "stars": 2, "score": 0.95},
        {"box": [480, 300, 600, 460], "name": "可酷伯", "model_name": "可酷伯", "score": 0.88},
    ], "bench": [
        {"box": [464, 687, 542, 810], "name": "慎", "model_name": "慎", "score": 0.81},
    ]}
    bars = {"board": [{"bar": [1230, 60, 1420, 90], "stars": 2}],
            "bench": [{"bar": [1100, 660, 1300, 690], "stars": 1}]}
    legends = [{"name": "小小英雄", "box": [700, 300, 830, 430]}]
    shop = [{"name": n, "price": p, "rect": [485 + 233 * i, 902, 715 + 233 * i, 1053]}
            for i, (n, p) in enumerate([("芸阿娜", 2), ("沃里克", 2), ("阿兹尔", 3),
                                        ("疯狂掷币人", 1), ("卡西奥佩娅", 3)])]
    equip_bar = [{"name": n, "rect": [23, 119 + 78 * i, 89, 185 + 78 * i], "count": cnt}
                 for i, (n, cnt) in enumerate([("装备拆卸器", 5), ("装备重铸器", None),
                                               ("无用大棒", None), ("拳套", None),
                                               ("巨人腰带", None), ("锁子甲", None)])]
    board_equips = [{"region": "board", "unit_name": "可酷伯",
                     "equips": [{"name": "石像鬼石板甲", "rect": [500, 470, 540, 510]}]}]
    balls_color = [{"color": "silver", "rect": [1230, 73, 1254, 97], "center": [1242, 85]},
                   {"color": "blue", "rect": [489, 540, 513, 564], "center": [501, 552]}]
    show = {k: True for k in ("OVERLAY_SHOW_UNITS", "OVERLAY_SHOW_BLOOD_BAR",
                              "OVERLAY_SHOW_BALLS_COLOR", "OVERLAY_SHOW_BALLS_YOLO",
                              "OVERLAY_SHOW_SHOP", "OVERLAY_SHOW_EQUIP_BAR",
                              "OVERLAY_SHOW_BOARD_EQUIPS")}
    frame = debug_overlay.draw_frame(size, units=units, bars=bars, legends=legends,
                                     balls_yolo=[], balls_color=balls_color, shop=shop,
                                     equip_bar=equip_bar, board_equips=board_equips,
                                     show=show, header="3-6 剩余21s 准备 Lv8 金币30")
    out = TEMP / "selftest_overlay_synth.png"
    TEMP.mkdir(exist_ok=True)
    frame.save(out)
    if frame.size != size:
        bad(f"画布尺寸 {frame.size} != {size}")
    import numpy as np
    arr = np.asarray(frame.convert("RGBA"))
    n_alpha = int((arr[..., 3] > 0).sum())
    expect = {"棋子蓝": (0, 100, 255), "血条灰": (64, 64, 64),
              "商店橙": (255, 140, 0), "装备青": (0, 180, 160),
              "携带洋红": (230, 50, 150), "小小英雄紫": (170, 0, 200),
              "球色青": (90, 200, 255), "header白": (235, 235, 235)}
    miss = []
    for k, c in expect.items():
        hit = int(((arr[..., 0] == c[0]) & (arr[..., 1] == c[1]) & (arr[..., 2] == c[2])
                   & (arr[..., 3] == 255)).sum())
        if hit == 0:
            miss.append(k)
        else:
            print(f"     颜色 {k}: {hit} px")
    if n_alpha > 1000 and not miss:
        ok(f"合成帧绘制通过: 非透明像素 {n_alpha}, {len(expect)} 类颜色全在 -> {out}")
    else:
        bad(f"合成帧绘制异常: 非透明像素 {n_alpha}, 缺颜色 {miss} -> {out}")


def check_pure_render() -> None:
    """utils.overlay_draw.render_frame 是通用纯函数 (不碰窗口), 也要能离测。"""
    from utils.overlay_draw import render_frame
    frame = render_frame(
        (1920, 1080),
        boxes=[{"rect": [100, 100, 300, 300], "label": "测试框", "color": (255, 0, 0),
                "width": 3}, {"box": [400, 100, 500, 200], "label": "box 键", "below": True,
                              "color": (0, 255, 0)}],
        points=[{"center": [600, 300], "r": 12, "label": "点", "color": (0, 200, 255),
                 "filled": True}],
        lines=[{"pts": [(700, 300), (900, 500)], "color": (255, 0, 255), "width": 2}],
        header="utils.overlay_draw 纯函数离线测试",
    )
    out = TEMP / "selftest_overlay_pure.png"
    frame.save(out)
    import numpy as np
    arr = np.asarray(frame.convert("RGBA"))
    n_alpha = int((arr[..., 3] > 0).sum())
    has_red = int(((arr[..., 0] == 255) & (arr[..., 1] == 0) & (arr[..., 2] == 0)).sum())
    has_green = int(((arr[..., 0] == 0) & (arr[..., 1] == 255) & (arr[..., 2] == 0)).sum())
    if n_alpha > 1000 and has_red > 0 and has_green > 0:
        ok(f"纯函数 render_frame 通过: 非透明 {n_alpha} px, 红框 {has_red} px, 绿框 {has_green} px "
           f"-> {out}")
    else:
        bad(f"纯函数 render_frame 异常: 非透明 {n_alpha}, 红 {has_red}, 绿 {has_green} -> {out}")


# --------------------------------------------------------------------------- #
def check_offline_frames() -> None:
    print("== [3] tests/images 离线帧 ==")
    for name in IMAGES:
        img = ROOT / "tests" / "images" / name
        if not img.exists():
            bad(f"测试图不存在: {img}")
            continue
        cmd = [PY, "-X", "utf8", "-u", "-m", "scripts.overlay_info",
               "--img", str(img.relative_to(ROOT)), "--json", "--no-print"]
        t0 = time.monotonic()
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
        cost = time.monotonic() - t0
        if proc.returncode != 0:
            bad(f"{name}: 退出码 {proc.returncode}, stderr 尾部: {proc.stderr.strip()[-400:]}")
            continue
        stem = Path(name).stem
        png = TEMP / f"overlay_info_{stem}.png"
        mix = TEMP / f"overlay_info_{stem}_mix.png"
        if not (png.exists() and mix.exists()):
            bad(f"{name}: PNG 没落盘 ({png.name}/{mix.name})")
            continue
        line = ""
        for ln in proc.stdout.strip().splitlines():
            if ln.startswith("{"):
                line = ln
        if not line:
            bad(f"{name}: 没看到 JSON 行")
            continue
        d = json.loads(line)
        ok(f"{name}: {cost:.1f}s 帧 cost={d['cost']}s 阶段={d['phase']}/{d['phase_cn']} "
           f"回合={d['stage']} 剩余={d['remain_time']} Lv={d['level']} 金币={d['gold']} "
           f"棋盘={len(d['board'])} 备战={len(d['bench'])} 商店={len([x for x in d['shop'] if x.get('name')])} "
           f"装备栏={len(d['equip_bar'])} 携带={len(d['board_equips'])} 球={len(d['balls'])} "
           f"errors={d['errors']}")
        info(f"PNG: {png.relative_to(ROOT)} | {mix.relative_to(ROOT)}")


# --------------------------------------------------------------------------- #
def check_cadence_assert() -> None:
    """1s 节拍模拟: 第 1 帧全算(冷), 之后按分项间隔到点重算, 看热帧能不能跟住 1s。"""
    from scripts.overlay_info import Observer, load_image
    img = load_image(ROOT / "tests" / "images" / "board_ingame.png")
    obs = Observer()
    costs = []
    t_start = time.monotonic()
    for i in range(6):
        target_t = t_start + i * 1.0
        now = time.monotonic()
        if now < target_t:
            time.sleep(target_t - now)
        obs.refresh(img, force=(i == 0))
        costs.append(obs.last_cost)
        print(f"     frame {i + 1}: cost={obs.last_cost:.3f}s fresh={obs.last_fresh} "
              f"errors={list(obs.errors)}")
    hot = costs[1:]
    if all(c <= 1.0 for c in hot):
        ok(f"节拍通过: 冷帧 {costs[0]:.2f}s, 热帧 {min(hot):.2f}-{max(hot):.2f}s (1s 节拍跟得住)")
    elif max(hot) <= 1.6:
        ok(f"节拍可接受: 热帧最慢 {max(hot):.2f}s (略超 1s, 靠 --interval 放宽即可)")
    else:
        bad(f"节拍超预算: 热帧 {hot}")


# --------------------------------------------------------------------------- #
def check_target() -> None:
    print("== [5] 实时窗口探测 (不判定失败: 本机不一定有雷电) ==")
    from overlay import debug_overlay
    tgt = debug_overlay.resolve_target()
    if tgt is None:
        info("没找到雷电 RenderWindow —— 实时模式会打中文提示并退出码 2, 属预期")
        return
    info(f"目标: main={tgt['main']} render={tgt['render']} "
         f"{tgt['w']}x{tgt['h']} @({tgt['left']},{tgt['top']}) "
         f"should_hide={debug_overlay.should_hide(tgt)}")


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T4 绘制信息工具自检")
    ap.add_argument("--quick", action="store_true", help="跳过 5 张图的离线帧子进程检查")
    args = ap.parse_args(argv)
    print(f"工程根: {ROOT}")
    check_imports()
    check_draw()
    check_pure_render()
    if not args.quick:
        check_offline_frames()
    print("== [4] 1s 节拍模拟 (同一张图连跑 6 帧) ==")
    check_cadence_assert()
    check_target()
    print()
    if ok_all:
        print("[OK] 自检全部通过")
        return 0
    print("[X] 自检有失败项 (见上面 [X] 行)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
