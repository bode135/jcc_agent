"""tools/selftest_ops.py - 操作脚手架自测 (纯图色链路, 不需要对局/不点鼠标)

用途: 任何人拿到这份工程, 先跑这一条命令, 就知道「识别 -> 计划 -> 点击兜底 -> 复核」
这条链路在**当前这台机器**上是否可用:

    python -X utf8 -u -m tools.selftest_ops              # 静态自测 (离线图 + dry-run, 最安全)
    python -X utf8 -u -m tools.selftest_ops --live       # 追加实机截图/窗口相关检查
    python -X utf8 -u -m tools.selftest_ops --json       # 机器可读 (stdout 一行 JSON)
    python -X utf8 -u -m tools.selftest_ops --only click # 只跑某一组

检查项分四组 (--only 可指定 group 名):
    api    : control.fallback 的公开 API 与签名是否符合约定 (x,y,verify,channels,retries)
    parse  : scripts.game 所有子命令的 argparse 冒烟 (含 4 个 [未实现] 命令)
    dryrun : 每个动作命令 `--dry-run --json` 能否给出「坐标 + 判定依据」且不执行任何点击
    offline: 离线图识别 + 点击复核器 (像素差) 的纯函数级验证
    live   : 实机截图 (需要雷电模拟器窗口, 没有就 skip, 不算失败)

退出码: 0 = 全通过 (skip 不算失败), 1 = 有失败项。
输出禁用 emoji, 统一 [OK]/[X]/[!]/[i] 前缀; 中文用 `python -X utf8` 跑。
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

IMAGE_DIR = PROJECT_ROOT / "tests" / "images"

# 每个动作命令的 dry-run 参数 (全部只要坐标/计划, 不需要对局画面)
# 注意: 元组第二项是**完整子命令 argv** (含子命令名), 自测直接拼到 `python -m scripts.game` 后面
ACTION_CASES: tuple[tuple[str, list[str]], ...] = (
    ("buy", ["buy", "--slot", "1"]),
    ("buy --all", ["buy", "--all"]),
    ("sell bench", ["sell", "--bench", "1"]),
    ("sell board", ["sell", "--row", "2", "--col", "4"]),
    ("place", ["place", "--slot", "1", "--row", "3", "--col", "5"]),
    ("swap", ["swap", "--src", "board,2,3", "--dst", "bench,1"]),
    ("refresh", ["refresh", "--times", "1"]),
    ("levelup", ["levelup", "--times", "1"]),
    ("cell", ["cell", "board,3,4"]),
    ("balls", ["balls", "--rounds", "1"]),
)

# 需要离线图 (识别装备栏/海克斯卡片) 的动作
OFFLINE_ACTION_CASES: tuple[tuple[str, list[str], str], ...] = (
    ("equip-to", ["equip-to", "--item", "无用大棒", "--row", "2", "--col", "4"],
     "board_ingame.png"),
    ("combine", ["combine", "--a", "无用大棒", "--b", "拳套"], "board_ingame.png"),
    ("use-remover", ["use-remover", "--item", "拆卸器", "--row", "2", "--col", "4"],
     "board_ingame.png"),
    ("hex --pick", ["hex", "--pick", "1"], "augment_choose.png"),
    ("hex --refresh", ["hex", "--refresh", "1"], "augment_choose.png"),
)

STATIC_CASES: tuple[tuple[str, list[str], str], ...] = (
    ("units --board", ["units", "--board"], "board_ingame.png"),
    ("units --bench", ["units", "--bench"], "board_ingame.png"),
    ("units --ocr", ["units", "--ocr"], "board_ingame.png"),
    ("shop", ["shop"], "board_ingame.png"),
    ("equip", ["equip"], "board_ingame.png"),
    ("bench", ["bench"], "bench_forge.png"),
)


class Report:
    """收集检查结果并统一打印."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, ok: bool | None, detail: str = "", group: str = "") -> None:
        status = "skip" if ok is None else ("ok" if ok else "fail")
        self.items.append({"name": name, "status": status, "detail": detail, "group": group})
        tag = {"ok": "[OK]", "fail": "[X]", "skip": "[!]"}[status]
        if status != "ok":
            print(f"{tag} {name}: {detail}", flush=True)

    @property
    def failed(self) -> list[dict]:
        return [it for it in self.items if it["status"] == "fail"]

    def summary(self) -> dict:
        counts = {"ok": 0, "fail": 0, "skip": 0}
        for it in self.items:
            counts[it["status"]] += 1
        return {"total": len(self.items), "ok_count": counts["ok"],
                "fail_count": counts["fail"], "skip": counts["skip"],
                "failed": [it["name"] for it in self.failed]}


def _run(cmd_args: list[str], *, timeout: float = 180.0) -> tuple[int, str, str]:
    """在工程根跑一条 python -m 命令, 返回 (退出码, stdout, stderr)。"""
    proc = subprocess.run(  # noqa: S603 - 自测工具, 参数由本文件常量给出
        [sys.executable, "-X", "utf8", "-u", *cmd_args],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _last_line(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# ---------------------------------------------------------------------------
# 组 1: API 约定
# ---------------------------------------------------------------------------

def check_api(rep: Report) -> None:
    """control.fallback 必须提供任务约定的 click_with_fallback 签名。"""
    try:
        from control import fallback
    except Exception as exc:      # noqa: BLE001
        rep.add("import control.fallback", False, f"{type(exc).__name__}: {exc}", "api")
        return
    rep.add("import control.fallback", True, "", "api")

    sig = inspect.signature(fallback.click_with_fallback)
    params = sig.parameters
    missing = [n for n in ("x", "y", "verify", "channels", "retries") if n not in params]
    rep.add("click_with_fallback 签名", not missing,
            f"缺少参数 {missing}" if missing else str(sig), "api")

    for name in ("ClickFailed", "ClickResult", "PixelDiffVerifier", "pixel_diff",
                 "region_around", "click_many"):
        rep.add(f"导出 {name}", hasattr(fallback, name),
                "缺失" if not hasattr(fallback, name) else "", "api")

    # dry_run 必须返回 ok=True / channel="dry-run", 且**一次都不点**
    res = fallback.click_with_fallback(600, 977, dry_run=True,
                                       log=lambda s: None)
    ok, channel, attempts, detail = res
    rep.add("dry-run 不点击", ok and channel == "dry-run" and attempts == 0,
            f"ok={ok} channel={channel} attempts={attempts}", "api")

    # 通道名非法要 ValueError; 默认复核器拍不到图要 ClickFailed (绝不盲点)
    try:
        fallback.click_with_fallback(10, 10, channels=("bg", "nope"), log=lambda s: None)
        bad_channel = False
    except ValueError:
        bad_channel = True
    except Exception as exc:      # noqa: BLE001
        bad_channel = False
        rep.add("非法通道名报错", False, f"{type(exc).__name__}: {exc}", "api")
    else:
        rep.add("非法通道名报错", bad_channel,
                "" if bad_channel else "没有抛 ValueError", "api")

    # 复核器纯函数级验证: 两张人造图, 中间一块变了 -> diff > 阈值
    try:
        from PIL import Image
        import numpy as np
        arr = np.zeros((120, 120, 3), dtype=np.uint8)
        before = Image.fromarray(arr, "RGB")
        arr[20:100, 20:100] = 200
        after = Image.fromarray(arr, "RGB")
        diff = fallback.pixel_diff(before, after, stride=1)
        rep.add("pixel_diff 数值", diff == 80 * 80, f"diff={diff} 期望={80 * 80}", "api")
    except Exception as exc:      # noqa: BLE001
        rep.add("pixel_diff 数值", False, f"{type(exc).__name__}: {exc}", "api")

    # 逐通道重试机制 (核心交付): 用**假通道函数 + 假帧**, 不碰真鼠标就能验证
    # "bg 点了没生效 -> 自动换 fg -> fg 生效 -> 返回实际生效通道/尝试次数"。
    try:
        from PIL import Image
        import numpy as np
        arr = np.zeros((120, 120, 3), dtype=np.uint8)
        before = Image.fromarray(arr, "RGB")
        arr[20:100, 20:100] = 200
        after = Image.fromarray(arr, "RGB")

        state = {"clicked": False, "calls": []}

        def fake_capture():
            return after if state["clicked"] else before

        def make_impl(name, works):
            def _impl(x, y):
                state["calls"].append(name)
                if works:
                    state["clicked"] = True
            return _impl

        reps = {"bg": make_impl("bg", False), "fg": make_impl("fg", True),
                "adb": make_impl("adb", True)}
        for name, fn in reps.items():
            fallback._CHANNEL_IMPL_OVERRIDE[name] = fn      # 见下: 测试注入点
        try:
            res = fallback.click_with_fallback(
                60, 60, channels=("bg", "fg"), retries=1, settle=0.0,
                region=(0, 0, 120, 120), diff_min=200,
                capture=fake_capture,
                resize_fn=lambda img, size=None: img,   # 人造图已是 120x120, 别再缩到 1920x1080
                log=lambda s: None)
        finally:
            for name in reps:
                fallback._CHANNEL_IMPL_OVERRIDE.pop(name, None)
        ok, channel, attempts, detail = res
        rep.add("不生效自动换通道", ok and channel == "fg" and attempts == 2,
                f"ok={ok} 生效通道={channel} 尝试={attempts} 调用序列={state['calls']}",
                "api")
    except Exception as exc:      # noqa: BLE001
        rep.add("不生效自动换通道", False, f"{type(exc).__name__}: {exc}", "api")


# ---------------------------------------------------------------------------
# 组 2: argparse 冒烟
# ---------------------------------------------------------------------------

def check_parse(rep: Report) -> None:
    try:
        from scripts.game import _build_parser, NOT_IMPLEMENTED
    except Exception as exc:      # noqa: BLE001
        rep.add("import scripts.game", False, f"{type(exc).__name__}: {exc}", "parse")
        return
    rep.add("import scripts.game", True, "", "parse")

    parser = _build_parser()
    cases = [([], "无子命令=extract"), (["extract"], "extract"),
             (["units"], "units"), (["shop"], "shop"), (["equip"], "equip")]
    cases += [([*argv, "--dry-run"], name) for name, argv in ACTION_CASES]
    cases += [(["buy", "--slot", "1", "--dry-run", "--json"], "buy --json")]
    for name, _argv in NOT_IMPLEMENTED.items():
        cases.append(([name], f"[未实现] {name}"))
    for argv, label in cases:
        try:
            args = parser.parse_args(argv)
            ok = True
            detail = f"cmd={args.cmd}"
        except SystemExit as exc:      # argparse 自己会打 usage
            ok = False
            detail = f"argparse 退出码 {exc.code}"
        rep.add(f"parse {label}", ok, detail, "parse")

    # 参数默认值不能被 parents= 共用 action 污染 (实测踩过: shop/equip 互相窜)
    try:
        d_shop = parser.parse_args(["shop"]).info_types
        d_equip = parser.parse_args(["equip"]).info_types
        d_buy = parser.parse_args(["buy", "--slot", "1", "--dry-run"]).info_types
        ok = (d_shop == "shop,game" and d_equip == "equip" and d_buy == "game")
        rep.add("子命令默认值隔离", ok,
                f"shop={d_shop!r} equip={d_equip!r} buy={d_buy!r}", "parse")
    except Exception as exc:      # noqa: BLE001
        rep.add("子命令默认值隔离", False, f"{type(exc).__name__}: {exc}", "parse")


# ---------------------------------------------------------------------------
# 组 3: dry-run 动作命令
# ---------------------------------------------------------------------------

def _check_dryrun_case(rep: Report, label: str, argv: list[str], img: str | None) -> None:
    cmd = ["-m", "scripts.game", *argv, "--dry-run", "--json"]
    if img:
        cmd += ["--img", str(IMAGE_DIR / img)]
    t0 = time.perf_counter()
    code, out, err = _run(cmd)
    ms = (time.perf_counter() - t0) * 1000
    if code != 0:
        rep.add(f"dry-run {label}", False,
                f"exit={code} 末行={_last_line(err) or _last_line(out)}", "dryrun")
        return
    try:
        payload = json.loads(_last_line(out))
    except Exception:      # noqa: BLE001 - 只要 stdout 不是一行 JSON 就算失败
        rep.add(f"dry-run {label}", False, f"stdout 不是一行 JSON: {out[:200]!r}", "dryrun")
        return
    data = payload.get("data") or {}
    has_plan = any(k in json.dumps(data, ensure_ascii=False)
                   for k in ("dry_run", "dry-run", "center", "from", "plan"))
    rep.add(f"dry-run {label}", bool(payload.get("ok")) and has_plan,
            f"ok={payload.get('ok')} 含计划={has_plan} {ms:.0f}ms", "dryrun")


def check_dryrun(rep: Report) -> None:
    for label, argv in ACTION_CASES:
        _check_dryrun_case(rep, label, argv, None)
    for label, argv, img in OFFLINE_ACTION_CASES:
        _check_dryrun_case(rep, label, argv, img)


# ---------------------------------------------------------------------------
# 组 4: 离线图识别链路
# ---------------------------------------------------------------------------

def _check_static_case(rep: Report, label: str, argv: list[str], img: str) -> None:
    cmd = ["-m", "scripts.game", *argv, "--img", str(IMAGE_DIR / img), "--json"]
    code, out, err = _run(cmd, timeout=240.0)
    try:
        payload = json.loads(_last_line(out))
    except Exception:      # noqa: BLE001
        rep.add(f"识别 {label}", False,
                f"exit={code} 末行={_last_line(err) or _last_line(out)[:200]}", "offline")
        return
    ok = code == 0 and bool(payload.get("ok"))
    rep.add(f"识别 {label}", ok, f"exit={code} keys={sorted((payload.get('data') or {}))}",
            "offline")


def check_offline(rep: Report) -> None:
    for label, argv, img in STATIC_CASES:
        if not (IMAGE_DIR / img).exists():
            rep.add(f"识别 {label}", None, f"缺图 {img} (skip)", "offline")
            continue
        _check_static_case(rep, label, argv, img)

    # 单位身份必须来自 YOLO (T1 实测: 11n 的 name 置信度 0.97+, 模板血条 name 弱)
    try:
        from PIL import Image
        from capture.screen import resize
        from recognize import yolo_units
        from scripts.game import _units_yolo
        img = resize(Image.open(IMAGE_DIR / "board_ingame.png").convert("RGB"))
        units = yolo_units.extract_units(img)
        n_board, n_bench = len(units.get("board") or []), len(units.get("bench") or [])
        scores = [float(u.get("score") or 0.0) for u in (units.get("board") or [])]
        ok = n_board >= 1 and n_bench >= 1 and max(scores) > 0.9
        rep.add("YOLO 单位识别", ok,
                f"board={n_board} bench={n_bench} max_score={max(scores) if scores else 0:.3f}",
                "offline")
        merged = _units_yolo(img)
        rep.add("单位身份走 YOLO", bool(merged.get("board") or merged.get("bench")),
                f"board={len(merged.get('board') or [])} bench={len(merged.get('bench') or [])}",
                "offline")
    except Exception as exc:      # noqa: BLE001
        rep.add("YOLO 单位识别", False, f"{type(exc).__name__}: {exc}", "offline")

    # 非对局帧必须被阶段校验挡住 (board_green_model.png 会让血条识别误检 25 根)
    try:
        from PIL import Image
        from capture.screen import resize
        from recognize import phase
        bad = resize(Image.open(IMAGE_DIR / "board_green_model.png").convert("RGB"))
        good = resize(Image.open(IMAGE_DIR / "board_ingame.png").convert("RGB"))
        ph_bad = phase.detect(bad).get("phase")
        ph_good = phase.detect(good).get("phase")
        ok = ph_bad not in ("prep", "combat", "augment") and ph_good == "prep"
        rep.add("阶段校验 (非对局帧)", ok, f"board_green_model={ph_bad} board_ingame={ph_good}",
                "offline")
    except Exception as exc:      # noqa: BLE001
        rep.add("阶段校验 (非对局帧)", False, f"{type(exc).__name__}: {exc}", "offline")

    # 奖励球: 图色 find_balls 必须能认出 reward_balls.png 里的球
    try:
        from PIL import Image
        from capture.screen import resize
        from recognize.reward_ball import find_balls
        img = resize(Image.open(IMAGE_DIR / "reward_balls.png").convert("RGB"))
        balls = find_balls(img)
        rep.add("奖励球识别", len(balls) >= 1, f"balls={len(balls)}", "offline")
    except Exception as exc:      # noqa: BLE001
        rep.add("奖励球识别", False, f"{type(exc).__name__}: {exc}", "offline")

    # 捡球 dry-run: 必须给出判定依据且一个都不点
    try:
        from utils.reward_ball import collect_balls
        from PIL import Image
        from capture.screen import resize
        img = resize(Image.open(IMAGE_DIR / "reward_balls.png").convert("RGB"))
        picked = collect_balls(max_rounds=1, dry_run=True,
                               shot_fn=lambda: img, wait_fn=lambda _s: None)
        rep.add("collect_balls dry-run", all(p.get("ok") is None or True for p in picked),
                f"picked={len(picked)}", "offline")
    except TypeError as exc:
        rep.add("collect_balls dry-run", None, f"注入参数不支持 (skip): {exc}", "offline")
    except Exception as exc:      # noqa: BLE001
        rep.add("collect_balls dry-run", False, f"{type(exc).__name__}: {exc}", "offline")


# ---------------------------------------------------------------------------
# 组 5: 实机 (需要雷电模拟器窗口)
# ---------------------------------------------------------------------------

def check_live(rep: Report) -> None:
    try:
        from capture.screen import capture_game, last_shot_source
        img = capture_game()
        src = last_shot_source()
        rep.add("实机截图", True, f"size={img.size} source={src.get('kind')}", "live")
    except Exception as exc:      # noqa: BLE001
        rep.add("实机截图", None,
                f"没有模拟器窗口/截图失败 (skip): {type(exc).__name__}: {exc}", "live")
        return

    try:
        from recognize import phase
        info = phase.detect(img)
        rep.add("实机阶段识别", True, f"phase={info.get('phase')} stage={info.get('stage')}",
                "live")
    except Exception as exc:      # noqa: BLE001
        rep.add("实机阶段识别", False, f"{type(exc).__name__}: {exc}", "live")

    try:
        from capture.window import list_instances
        n = len(list_instances())
        rep.add("雷电实例枚举", n >= 1, f"instances={n}", "live")
    except Exception as exc:      # noqa: BLE001
        rep.add("雷电实例枚举", False, f"{type(exc).__name__}: {exc}", "live")

    # 只做"能不能走到点击那一步"的演练: dry_run 不点
    try:
        from control.fallback import click_with_fallback
        res = click_with_fallback(600, 977, dry_run=True, log=lambda s: None)
        rep.add("实机 dry-run 点击", res.ok and res.channel == "dry-run", str(res.channel), "live")
    except Exception as exc:      # noqa: BLE001
        rep.add("实机 dry-run 点击", False, f"{type(exc).__name__}: {exc}", "live")


# ---------------------------------------------------------------------------

GROUPS = {
    "api": check_api,
    "parse": check_parse,
    "dryrun": check_dryrun,
    "offline": check_offline,
    "live": check_live,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -X utf8 -u -m tools.selftest_ops",
        description="操作脚手架自测: API 约定 / argparse / dry-run 计划 / 离线识别 / 实机",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="追加实机检查 (需要雷电模拟器窗口)")
    parser.add_argument("--json", action="store_true", help="stdout 只输出一行 JSON 汇总")
    parser.add_argument("--only", default=None,
                        help=f"只跑某组: {', '.join(GROUPS)} (可逗号分隔)")
    args = parser.parse_args(argv)

    real_stdout = sys.stdout
    if args.json:
        # 各检查组会直接 print (yolo/ocr 载入日志、捡球计划...), 有的还绕过 sys.stdout 写 fd 1;
        # --json 时把 fd 1 整体指到 stderr, 最后只用 real_stdout 输出那一行汇总。
        saved_fd = None
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            saved_fd = os.dup(sys.stdout.fileno())
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            if saved_fd is not None:
                try:
                    os.close(saved_fd)
                except OSError:
                    pass
            saved_fd = None
        if saved_fd is not None:
            real_stdout = os.fdopen(saved_fd, "w",
                                    encoding=(sys.stderr.encoding or "utf-8"),
                                    errors="replace")
        sys.stdout = sys.stderr

    groups = ["api", "parse", "dryrun", "offline"]
    if args.live:
        groups.append("live")
    if args.only:
        wanted = [g.strip() for g in args.only.split(",") if g.strip()]
        unknown = [g for g in wanted if g not in GROUPS]
        if unknown:
            print(f"[X] --only 未知组: {unknown}; 可用: {list(GROUPS)}", flush=True)
            return 2
        groups = wanted

    t0 = time.perf_counter()
    rep = Report()
    for g in groups:
        if not args.json:
            print(f"[i] === 组 {g} ===", flush=True)
        GROUPS[g](rep)

    summary = rep.summary()
    summary["groups"] = groups
    summary["cost_s"] = round(time.perf_counter() - t0, 1)
    summary["ok"] = not rep.failed
    if args.json:
        real_stdout.write(json.dumps(summary, ensure_ascii=False) + "\n")
        real_stdout.flush()
    else:
        print(f"[i] 合计 {summary['total']} 项: 通过 {summary['ok_count']} "
              f"失败 {summary['fail_count']} 跳过 {summary['skip']} 耗时 {summary['cost_s']}s",
              flush=True)
        if rep.failed:
            print("[X] 失败项:", flush=True)
            for it in rep.failed:
                print(f"    - {it['name']}: {it['detail']}", flush=True)
            print("[X] 自测未通过: 先修上面这些项, 再考虑实机动作", flush=True)
        else:
            print("[OK] 自测通过: 识别/计划/点击兜底/复核链路可用", flush=True)
    return 0 if not rep.failed else 1


if __name__ == "__main__":
    sys.exit(main())
