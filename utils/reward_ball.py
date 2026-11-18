"""奖励球自动拾取: **纯图色** (识别 -> 按右下角排序 -> 逐个点击 -> 复查消失)。

用法:
    from utils.reward_ball import collect_balls, detect_balls
    picked = collect_balls()          # 拾取当前画面全部奖励球, 返回拾取清单
    balls = detect_balls(img)         # 只识别, 不点 (module 单测/离线图用)

改造要点 (纯图色):
    只保留图色一条路:
        颜色法 `recognize.reward_ball.find_balls` (YOLO 球模型作可选兜底)
        + 逐球点击 + **点完复查球是否消失** (判据不是"点了没报错", 而是画面里那颗球没了)。
    排序/区域过滤/护栏这些跟"怎么读球"无关的口径全部原样保留 (理由见各函数 docstring)。

点击一律走 `control.fallback.click_with_fallback` (默认局部像素差复核): 后台 PostMessage
丢消息时自动换 fg/adb 通道; 单颗球点不掉只记一条 [!] 继续下一颗, 不中断整轮。
"""

from __future__ import annotations

import time

from capture.screen import capture_game, resize
from config.config import default_config
from control.mouse import click_point
from recognize.reward_ball import find_balls


def _sort_by_corner(balls: list[dict], width: int, height: int,
                    corner: str = "bottom_right") -> list[dict]:
    """按"离指定角由近到远"排序 (用户口径: 越靠近右下角越先点)。

    为什么要排: 点击会**指挥小小英雄跑过去**。若先点右上角再点右下角, 它会跑到半路又折返,
    折返途中可能来不及碰到球 → **漏捡**。按离右下角由近到远排成一条不回头的路径, 走位最省。
    """
    def key(b: dict) -> float:
        cx, cy = b["center"][0], b["center"][1]
        if corner == "bottom_left":
            return (cx) ** 2 + (height - cy) ** 2
        if corner == "top_right":
            return (width - cx) ** 2 + (cy) ** 2
        if corner == "top_left":
            return cx ** 2 + cy ** 2
        return (width - cx) ** 2 + (height - cy) ** 2   # 默认: 右下角

    return sorted(balls, key=key)   # sorted 稳定: 同距离保持原顺序


def _ball_region() -> tuple[int, int, int, int]:
    """奖励球检测区域 (读 `config.BALL_REGION`; 没配就退回"不限")。

    2026-11-04: 颜色法原来扫整块棋盘, 战斗期会把**对方备战席**上的棋子/旗子
    当成奖励球点过去 —— 对方备战席根本没有球。这里按配置收窄到己方棋盘可玩区。
    """
    reg = getattr(default_config, "BALL_REGION", None)
    try:
        if reg is not None and len(tuple(reg)) == 4:
            return tuple(int(v) for v in reg)   # type: ignore[return-value]
    except Exception:      # noqa: BLE001 - 配置写错不影响捡球
        pass
    return (0, 0, 10 ** 4, 10 ** 4)


def _clip_to_region(balls: list[dict]) -> list[dict]:
    """只保留球心落在 `BALL_REGION` 内的候选 (对方备战席/石墙/我方备战席一律不算球)."""
    x0, y0, x1, y1 = _ball_region()
    kept = [b for b in balls
            if x0 <= int(b["center"][0]) <= x1 and y0 <= int(b["center"][1]) <= y1]
    if len(kept) != len(balls):
        print(f"[捡球] 区域过滤: 剔除 {len(balls) - len(kept)} 个区域外候选 "
              f"(对方备战席/石墙等, 区域={x0},{y0},{x1},{y1})", flush=True)
    return kept


def detect_balls(img, mode: str = "color") -> list[dict]:
    """按 mode 识别奖励球, 统一返回 `[{"center": (x, y), "color": ...}, ...]`.

    - `"color"`: 颜色法 `recognize.reward_ball.find_balls` (默认, 快, 但战斗特效可能误检)。
    - `"yolo"`: 先跑一次 YOLO 推理, 取被丢弃的 ball 类框 (`recognize.yolo_units.last_balls`);
      模型检出的球框**不参与奕子/格位映射**, 只在这里用于捡球。
      **注意走 `YOLO_BALL_ONNX`(v2.1) 而不是认奕子的 11s 模型** —— 11s 只有 67 类,
      类别表尾部的 `ball` 类不在其中, 用它检球永远是 0 个。
    """
    if mode == "yolo":
        from recognize import yolo_units
        yolo_units.predict(img, model=yolo_units.model_for("ball"))   # 球框落在 _LAST_BALLS
        out = []
        for b in yolo_units.last_balls():
            x1, y1, x2, y2 = b["box"]
            out.append({"color": "yolo",
                        "center": (int((x1 + x2) // 2), int((y1 + y2) // 2)),
                        "score": b.get("score")})
        return _clip_to_region(out)
    return _clip_to_region(find_balls(img))


def _shot():
    """实时截图并按 1920×1080 基准归一 (交给 resize, 不在这里改尺寸)."""
    return resize(capture_game())


def _same_ball(a: dict, b: dict, radius: int = 70) -> bool:
    """两个球候选是否是"同一颗" (球会随小小英雄移动/轻微抖动, 不能按精确坐标去重)."""
    ax, ay = a["center"]
    bx, by = b["center"]
    return (ax - bx) ** 2 + (ay - by) ** 2 <= radius ** 2


class _BallGoneVerifier:
    """点击后的判据: 刚才那颗球**在画面里消失**(允许附近 70px 内不再有同色球).

    比通用像素差更准: 点球场地上同时有棋子光效/血条变化, 全屏或大区域像素差会被污染;
    "那颗球没了"才是捡球成功的唯一物证。复核失败时不会抛错, 只返回 ok=False。
    """

    __doc__ = "局部重识别: 点击位置附近的奖励球是否消失(捡球成功的唯一物证)"

    def __init__(self, ball: dict, *, mode: str = "color", radius: int = 70,
                 shot=None) -> None:
        self.ball = ball
        self.mode = mode
        self.radius = radius
        self._shot = shot or _shot

    def reset(self) -> None:      # 与 control.fallback 的默认复核器接口对齐 (无需基准帧)
        return None

    def __call__(self, info: dict | None = None) -> dict:
        try:
            balls = detect_balls(self._shot(), self.mode)
        except Exception as exc:      # noqa: BLE001 - 截图/识别失败视为"没验成"
            return {"ok": False, "verify": "ball_gone", "error": f"{type(exc).__name__}: {exc}"}
        left = [b for b in balls if _same_ball(self.ball, b, self.radius)]
        return {"ok": not left, "verify": "ball_gone",
                "same_center": left[0]["center"] if left else None,
                "balls_now": len(balls)}


def collect_balls(max_rounds: int = 1, click_cooldown: float = 0.6,
                  settle_seconds: float = 2, cancel=None,
                  max_clicks: int = 0, min_center_y: int = 0,
                  mode: str = "color", max_seconds: float = 0.0, *,
                  dry_run: bool = False, use_fallback: bool = True,
                  shot_fn=None, wait_fn=None) -> list[dict]:
    """拾取画面中的奖励球, 返回已点击(或 dry-run 下计划点击)的球列表.

    Args:
        max_rounds: 最多识别-点击轮数。**默认 1** (dsv4f §5.1): 每轮 `settle_seconds=2`
            是纯等待, 而 prep 窗口只有 ~30s, 捡球不该吃掉 1/5。多轮兜底由调用方显式传。
        max_seconds: **挂钟硬上限** (>0 生效, 0=不限)。每颗球**之前**和**之后**都查表,
            到点立刻返回, 不再等到"本轮结束"才检查 (一个球的点击本身要 0.2~1.5s)。
        click_cooldown: 相邻两次点击的间隔秒数。
        settle_seconds: 每轮点击后等待画面稳定的秒数。
        cancel: 可选回调 (`() -> bool`); 返回 True 时**立刻停手** (不再点下一颗球)。
            采集器用它做时间护栏 (战斗剩余 <= 15s 就收手, 不吃下一轮的 prep)。
        max_clicks: 本次最多点几颗 (0 = 不限); 用于"检出数量异常多"时防乱点。
        min_center_y: 只点 `center.y >= 此值` 的球 (0 = 不限)。剔除**顶部 HUD 的固定误检** ——
            实测 `find_balls` 在战斗阶段把顶栏图标当球 (点下去是点 UI 不是捡球)。
        mode: 识别方式 `"color"` / `"yolo"` (见 detect_balls)。
        dry_run: 只识别 + 打印计划坐标/判定依据, 一次都不点 (返回计划清单, 每项带 plan=True)。
        use_fallback: True 走 `click_with_fallback` (点后复查球消失, 失败换通道);
            False 走老的 `click_point` 裸点 (排障/the fallback 本身有问题时的回退开关)。
        shot_fn / wait_fn: 注入式取帧/等待 (`() -> Image` / `(float) -> None`),
            给自测/离线图复现用; 缺省走实时截图与 time.sleep。

    Returns:
        `[{"center": (x, y), "color": ..., "round": n, "ok": bool, "channel": str|None}, ...]`
        —— 内存优先方案里返回的也是这个形状 (调用方 scripts/game.py 只看 center/color)。
    """
    picked: list[dict] = []
    t0 = time.perf_counter()
    stop_requested = False
    _take_shot = shot_fn or _shot
    _wait = wait_fn or time.sleep

    def _out_of_time() -> bool:
        return max_seconds > 0 and (time.perf_counter() - t0) >= max_seconds

    def _cancelled() -> bool:
        try:
            return bool(cancel and cancel())
        except Exception:      # noqa: BLE001 - 护栏回调故障不该打断捡球
            return False

    for rnd in range(1, max(1, int(max_rounds)) + 1):
        if stop_requested or _out_of_time() or _cancelled():
            break
        if dry_run:
            print(f"[捡球] dry-run 第{rnd}轮: 只识别不点击 (需要模拟器窗口才能实时截图)", flush=True)
        try:
            img = _take_shot()
        except Exception as exc:      # noqa: BLE001 - 无窗口/截图失败要给人看的中文报错
            if dry_run:               # dry-run 没窗口不算错误: 给出判据即可, 不假装识别成功
                print(f"[!] 无窗口/取不到画面, dry-run 只输出判定依据: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                print("[i] dry-run 判定依据: 点击后该球从画面消失 (局部重识别 find_balls)",
                      flush=True)
                break
            raise RuntimeError(
                f"捡球需要实时截图, 但当前取不到游戏画面: {type(exc).__name__}: {exc}。"
                "请先启动雷电模拟器并进入对局画面; 只想看坐标请用 --dry-run。") from exc

        balls = detect_balls(img, mode)
        # 剔除顶部 HUD 误检 (点下去是点 UI 不是捡球)
        if min_center_y > 0:
            keep = [b for b in balls if int(b["center"][1]) >= min_center_y]
            if len(keep) != len(balls):
                print(f"[捡球] 剔除顶部误检 {len(balls) - len(keep)} 个 "
                      f"(y < {min_center_y})", flush=True)
            balls = keep
        # 已经点过的球不再重复点 (小小英雄没走到/球还在画面里时会出现)
        balls = [b for b in balls
                 if not any(_same_ball(b, p) for p in picked)]
        if not balls:
            print(f"[捡球] 第{rnd}轮: 当前画面没有(新的)奖励球", flush=True)
            break

        w, h = img.size
        balls = _sort_by_corner(balls, w, h)
        print(f"[捡球] 第{rnd}轮: 检出 {len(balls)} 个球 -> "
              + ", ".join(f"{b['color']}@{tuple(b['center'])}" for b in balls), flush=True)

        for ball in balls:
            if stop_requested or _out_of_time() or _cancelled():
                stop_requested = True
                print("[捡球] 触发护栏/超时, 立刻停手 (不再点下一颗)", flush=True)
                break
            if max_clicks and len(picked) >= max_clicks:
                print(f"[捡球] 已达 max_clicks={max_clicks}, 停止", flush=True)
                stop_requested = True
                break

            x, y = int(ball["center"][0]), int(ball["center"][1])
            rec = {"center": (x, y), "color": ball.get("color"), "round": rnd,
                   "ok": False, "channel": None}
            if dry_run:
                rec.update(ok=True, channel="dry-run", plan=True)
                print(f"[捡球] dry-run 计划点击 {tuple(ball['center'])} "
                      f"({ball.get('color')}), 判定依据: 该球从画面消失", flush=True)
                picked.append(rec)
                continue

            if use_fallback:
                from control.fallback import ClickFailed, click_with_fallback
                try:
                    res = click_with_fallback(
                        x, y, verify=_BallGoneVerifier(ball, mode=mode), settle=0.25)
                    rec.update(ok=True, channel=res.channel, attempts=res.attempts)
                    print(f"[OK] 捡球 {tuple(ball['center'])} 生效 (通道 {res.channel}, "
                          f"{res.attempts} 次)", flush=True)
                except ClickFailed as exc:
                    rec.update(ok=False, error=str(exc))
                    print(f"[!] 捡球 {tuple(ball['center'])} 未生效: {exc}", flush=True)
            else:
                try:
                    click_point(x, y)
                    rec.update(ok=True, channel="click_point")
                except Exception as exc:      # noqa: BLE001
                    rec.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                    print(f"[!] 捡球 {tuple(ball['center'])} 点击异常: {exc}", flush=True)
            picked.append(rec)
            if click_cooldown > 0:
                _wait(click_cooldown)
            if _out_of_time():
                print(f"[捡球] 超时 ({max_seconds}s) 停止", flush=True)
                stop_requested = True
                break

        if rnd < max_rounds and not stop_requested and settle_seconds > 0:
            _wait(settle_seconds)

    ok_n = sum(1 for p in picked if p.get("ok"))
    print(f"[捡球] 结束: 计划/已点 {len(picked)} 个, 确认生效 {ok_n} 个, "
          f"耗时 {time.perf_counter() - t0:.2f}s", flush=True)
    return picked


def print_balls(picked: list[dict]) -> None:
    """把 collect_balls 的结果按人读格式打印 (scripts/game.py balls 子命令用)."""
    from recognize.reward_ball import COLOR_NOTES
    print(f"[捡球] 共 {len(picked)} 个:", flush=True)
    for b in picked:
        note = COLOR_NOTES.get(str(b.get("color")), str(b.get("color")))
        status = "生效" if b.get("ok") else "未生效"
        ch = b.get("channel") or "-"
        print(f"  {note} center={tuple(b['center'])} {status} 通道={ch}", flush=True)


__all__ = ["detect_balls", "collect_balls", "print_balls"]
