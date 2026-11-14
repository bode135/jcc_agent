"""点击兜底: 逐通道点击 + **效果复核**, 不生效自动换通道重试.

为什么需要 (纯图色工程的前提):
    后台 PostMessage (`bg`) 在部分帧/部分 UI 上会被模拟器丢消息 (点了等于没点),
    而 adb 注入 (`adb`) 虽然稳但每次要走 ldconsole 子进程 + Android 内部注入,
    开销量级完全不同。谁生效不可预知, 所以**每次点击都必须有"点了到底有没有反应"的
    判定**: 复核不过就换下一个通道重试, 全通道都不生效就明确报错, 绝不静默成功。

通道语义 (`channels`):
    - "bg" : `control.input.click_left_background` —— 后台 PostMessage 到 RenderWindow
             (WM_MOUSEMOVE -> WM_LBUTTONDOWN -> WM_LBUTTONUP), 不抢用户前台键鼠。
    - "fg" : `control.input.click_left_foreground` —— 前台真实鼠标通道。注意: 当
             `default_config.ALLOW_FOREGROUND_FALLBACK=False` (本项目默认) 时它就是
             `adb_tap`, 与 "adb" 通道等价 (会打印一条 [!] 说明, 便于排障时不被日志误导)。
    - "adb": `control.input.adb_tap` —— 雷电 adb 在 Android 内部注入 tap,
             宿主键鼠不动, 对内嵌 WebView 有效。

复核 (`verify`):
    默认 = `PixelDiffVerifier(x, y)` —— 点击前后对**点击点周围的小方框**做像素差
    (局部差, 不看全屏: 全屏会被战斗动画/光效/飞字污染成假阳性)。也可以自己传:
        - 无参可调用 (`() -> bool | dict`), 或单参可调用 (`(info: dict) -> bool | dict`);
        - 返回 dict 时取 `ok` (或 `hit`) 键, 整个 dict 会写进结果详情。
    传 verify 是**推荐做法**: 每个动作"生效"的判据本来就不一样 (买卡=槽位卡牌消失,
    买经验=金币减少, 捡球=球消失, 海克斯=chosen 变化), 用像素差只是它的通用兜底。

用法:
    from control.fallback import click_with_fallback, ClickFailed
    ok, channel, attempts, detail = click_with_fallback(960, 900)
    res = click_with_fallback(1200, 980, verify=lambda: gold_decreased(), channels=("adb", "bg"))
    res.ok / res.channel / res.attempts / res.detail   # 也可按属性取

    dry-run (只打印计划, 一次都不点):
    res = click_with_fallback(960, 900, dry_run=True)

失败一律抛 `ClickFailed` (RuntimeError 子类), 消息是给人看的中文排查清单,
异常对象上带 `.result` 属性 (完整尝试记录), 调用方 `except ClickFailed as exc` 后
把 `exc.result.as_dict()` 塞进 --json 结果即可。
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import numpy as np
from PIL import Image

from capture.screen import capture_game, resize
from config.config import default_config

#: 默认通道顺序: 先最便宜的后台通道, 再前台, 最后 adb (adb 每次要起子进程, 最慢)。
DEFAULT_CHANNELS: tuple[str, ...] = ("bg", "fg", "adb")

#: 通道 -> 一句话说明 (日志/JSON 里都用它, 避免同一个通道在不同命令里叫法不一致)。
CHANNEL_NOTES: dict[str, str] = {
    "bg": "后台 PostMessage (不抢前台键鼠)",
    "fg": "前台真实鼠标 (ALLOW_FOREGROUND_FALLBACK=True 才真的走前台)",
    "adb": "雷电 adb 内部注入 tap",
}

#: 像素差判"变了"的单通道灰度阈值 (与源工程 scripts/game.py::_region_diff 同口径)。
PIXEL_CHANNEL_THRESHOLD: int = 30


class ClickFailed(RuntimeError):
    """所有通道点击后复核都不生效 (或复核手段本身不可用)."""

    def __init__(self, message: str, *, result: "ClickResult | None" = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass
class ClickResult:
    """一次点击兜底的结果.

    Attributes:
        ok: 是否确认生效 (dry_run 时恒 True, 但 channel="dry-run"、attempts=0)。
        channel: 实际生效的通道名; 全失败为 None; dry_run 为 "dry-run"。
        attempts: 真实点击次数 (不含 dry-run)。
        detail: 结构化详情 (每次尝试的通道/复核值/异常, region, 校验方式等)。
    """

    ok: bool
    channel: str | None
    attempts: int
    detail: dict = field(default_factory=dict)

    def __iter__(self):
        """支持 `ok, channel, attempts, detail = click_with_fallback(...)` 解包."""
        return iter((self.ok, self.channel, self.attempts, self.detail))

    def __len__(self) -> int:
        return 4

    def __getitem__(self, index: int):
        return (self.ok, self.channel, self.attempts, self.detail)[index]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "channel": self.channel,
                "attempts": self.attempts, "detail": self.detail}


# ---------------------------------------------------------------------------
# 像素差复核 (默认 verify)
# ---------------------------------------------------------------------------

def region_around(x: float, y: float, *, half: int = 180,
                  size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """点击点周围的方框 (夹到画面内), 作为默认复核区域."""
    w, h = size or default_config.DEFAULT_RESOLUTION
    x1 = max(0, int(x) - half)
    y1 = max(0, int(y) - half)
    x2 = min(int(w), int(x) + half)
    y2 = min(int(h), int(y) + half)
    if x2 <= x1 or y2 <= y1:          # 极端越界 (坐标写错) -> 退回左上角一小块
        return (0, 0, min(64, w), min(64, h))
    return (x1, y1, x2, y2)


def pixel_diff(before: Image.Image, after: Image.Image,
               region: tuple[int, int, int, int] | None = None,
               *, stride: int = 2, channel_threshold: int = PIXEL_CHANNEL_THRESHOLD) -> int:
    """两帧在某区域内的"明显变化"像素数 (|diff| > channel_threshold 的计数).

    区域缺省 = 整帧; `stride` 隔点采样 (默认 2 -> 1/4 计算量), 只用于"变没变"的
    定性判定, 不需要精确像素数。
    """
    a = np.asarray(before.convert("RGB"), dtype=np.int16)
    b = np.asarray(after.convert("RGB"), dtype=np.int16)
    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    if region is None:
        x1, y1, x2, y2 = 0, 0, w, h
    else:
        x1, y1, x2, y2 = (int(v) for v in region)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return 0
    d = np.abs(a[y1:y2:stride, x1:x2:stride] - b[y1:y2:stride, x1:x2:stride])
    return int((d.max(axis=2) > channel_threshold).sum())


class PixelDiffVerifier:
    """默认复核器: 点击点局部区域的像素差是否超过 `diff_min`.

    用法 (内部已处理"点前拍一张"):
        v = PixelDiffVerifier(1200, 980, region=(1120, 940, 1400, 1060))
        v.reset()          # 拍 before (可省略, 首次 __call__ 会自动拍)
        v()                # 拍 after + 算差 -> {"ok": bool, "diff": int, ...}
    """

    def __init__(self, x: float, y: float, *, region: tuple[int, int, int, int] | None = None,
                 half: int = 180, diff_min: int = 200, stride: int = 2,
                 capture: Callable[[], Image.Image] | None = None,
                 resize_fn: Callable[..., Image.Image] | None = None) -> None:
        self.x, self.y = float(x), float(y)
        self.region = region or region_around(self.x, self.y, half=half)
        self.diff_min = int(diff_min)
        self.stride = int(stride)
        self._capture = capture or capture_game
        self._resize = resize_fn or resize
        self._before: Image.Image | None = None
        self.calls = 0
        self.last_diff = 0

    def _shot(self) -> Image.Image:
        return self._resize(self._capture())

    def reset(self) -> None:
        """重拍 before (每次换通道重试前调用一次, 否则拿"上一次的 after"当基准会误判)."""
        self._before = self._shot()
        self.calls = 0
        self.last_diff = 0

    def __call__(self, info: dict | None = None) -> dict:
        if self._before is None:
            self.reset()
        after = self._shot()
        diff = pixel_diff(self._before, after, self.region, stride=self.stride)
        self.calls += 1
        self.last_diff = diff
        return {"ok": diff >= self.diff_min, "diff": diff, "diff_min": self.diff_min,
                "region": list(self.region), "verify": "pixel_diff"}


def describe_verify(verify: Callable | None, x: float = 0.0, y: float = 0.0) -> str:
    """给人看的一行"判定依据" (dry-run 打印用)."""
    if verify is None:
        return f"局部像素差(点击点 {int(x)},{int(y)} 周围方框 变化像素 > 阈值)"
    doc = (getattr(verify, "__doc__", None) or "").strip().splitlines()
    if doc:
        return doc[0]
    name = getattr(verify, "__name__", None)
    return f"自定义 verify({name})" if name else "自定义 verify"


# ---------------------------------------------------------------------------
# 通道实现 / 主入口
# ---------------------------------------------------------------------------

# 自测注入点: {通道名: 点函数}; 只给 tools/selftest_ops.py 用假通道验证"不生效换通道"逻辑,
# 生产代码不会往里写东西 (留口子而不是给 click_with_fallback 加 channel_impl 参数,
# 是为了不让测试用的钩子出现在正式 API 签名里)。
_CHANNEL_IMPL_OVERRIDE: dict[str, Callable[[float, float], None]] = {}


def _channel_impl(channel: str) -> Callable[[float, float], None]:
    """通道名 -> 具体点击实现 (延迟导入 control.input, 避免模块级循环导入)."""
    injected = _CHANNEL_IMPL_OVERRIDE.get(channel)
    if injected is not None:
        return injected
    from control import input as ci

    if channel == "bg":
        # 强制后台通道: 不复用 click_left, 否则 BACKGROUND_INPUT=False 时会偷换成前台鼠标
        return getattr(ci, "click_left_background", ci._bg_click)
    if channel == "fg":
        return ci.click_left_foreground
    if channel == "adb":
        return ci.adb_tap
    raise ValueError(f"未知点击通道 {channel!r} (可选: {', '.join(DEFAULT_CHANNELS)})")


def _normalize_verify(value) -> tuple[bool, dict]:
    """把 verify 的各种返回值统一成 (ok, detail_dict)."""
    if isinstance(value, dict):
        ok = bool(value.get("ok", value.get("hit", True)))
        return ok, dict(value)
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], bool):
        return value[0], {"ok": value[0], "extra": value[1]}
    return bool(value), {"ok": bool(value)}


def _call_verify(verify: Callable, info: dict):
    """调 verify: 0 参或 1 参(接收本次尝试信息) 都支持."""
    try:
        sig = inspect.signature(verify)
        required = [p for p in sig.parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                    and p.default is p.empty]
        n = len(required)
    except (TypeError, ValueError):
        n = 0
    return verify(info) if n >= 1 else verify()


def _normalize_channels(channels: Iterable[str]) -> tuple[str, ...]:
    if isinstance(channels, str):
        channels = (channels,)
    out: list[str] = []
    for ch in channels:
        name = str(ch).strip().lower()
        if name not in DEFAULT_CHANNELS:
            raise ValueError(f"未知点击通道 {ch!r} (可选: {', '.join(DEFAULT_CHANNELS)})")
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError("channels 不能为空")
    return tuple(out)


def _failure_message(x: float, y: float, records: list[dict], channels: Sequence[str],
                     attempts: int) -> str:
    tried = ", ".join(f"{c}×{sum(1 for r in records if r['channel'] == c)}" for c in channels)
    last = records[-1] if records else {}
    reason = last.get("error") or (
        f"复核未通过 (diff={last.get('diff')} < 阈值 {last.get('diff_min')})"
        if last else "没有任何尝试记录")
    return (
        f"点击 ({int(x)}, {int(y)}) 全部通道均未生效: 共 {attempts} 次尝试 ({tried}); "
        f"最后一次: {reason}。"
        "排查建议: 1) 先 --dry-run 核对坐标是否落在可交互 UI 上; "
        "2) 模拟器是否卡死/窗口被遮挡 (capture.window.wake_window 唤醒); "
        "3) 该目标可能本来就没有画面变化 (点空地) -> 用 verify= 换成动作专属判据; "
        "4) adb 通道失败多为 ldconsole.exe 未找到 (config.LDPLAYER_CONSOLE)."
    )


def click_with_fallback(x: float, y: float, *,
                        verify: Callable | None = None,
                        channels: Sequence[str] = DEFAULT_CHANNELS,
                        retries: int = 2,
                        region: tuple[int, int, int, int] | None = None,
                        half: int = 180,
                        diff_min: int = 200,
                        settle: float = 0.2,
                        capture: Callable[[], Image.Image] | None = None,
                        resize_fn: Callable[..., Image.Image] | None = None,
                        dry_run: bool = False,
                        note: str | None = None,
                        log: Callable[[str], None] | None = None) -> ClickResult:
    """逐通道点击 (x, y), 每次点完调 `verify()` 判定; 不生效换下一通道, 全失败抛错.

    Args:
        x, y: 1920×1080 基准坐标 (control.input 内部换算到屏幕/客户区/Android 像素)。
        verify: 复核回调; None = 用 `PixelDiffVerifier` (点击点周围局部像素差)。
        channels: 通道顺序, 缺省 ("bg", "fg", "adb"); 只试给定的那些。
        retries: 轮数 (每轮把 channels 全试一遍), 缺省 2; 总尝试上限 = retries*len(channels)。
        region / half / diff_min: 默认复核器的区域/半径/阈值 (只对 verify=None 生效)。
        settle: 每次点击后等待复核的秒数 (游戏 UI 有 ~0.1s 的响应延迟)。
        capture, resize_fn: 默认复核器的取帧方式 (测试注入用)。
        dry_run: 只打印"将会怎么点 + 判定依据", 一次都不点 (返回 ok=True, channel="dry-run")。
        note: 覆盖"判定依据"那一行的人话说明 (给动作专属语义用, 如 "买卡: 槽位变空")。
        log: 输出函数 (缺省 print); 传 None 之外的 callable 可把日志改道到 stderr。

    Returns:
        `ClickResult` —— 支持 `ok, channel, attempts, detail = click_with_fallback(...)` 解包,
        也支持 `res.ok / res.channel / res.attempts / res.detail / res.as_dict()`。

    Raises:
        ClickFailed: 所有通道所有轮次都没通过复核 (消息含中文排查清单, `.result` 带详情)。
        ValueError: 通道名非法。
    """
    say = log or print
    chans = _normalize_channels(channels)
    retries = max(1, int(retries))
    ver_desc = note or describe_verify(verify, x, y)
    detail: dict = {"x": int(x), "y": int(y), "channels": list(chans), "retries": retries,
                    "records": [], "verify": None}

    if dry_run:
        say(f"[点击] dry-run: 计划点击 ({int(x)}, {int(y)}); 通道 {'->'.join(chans)}; "
            f"每通道最多 {retries} 轮; 判定依据: {ver_desc}")
        detail.update(verify=ver_desc, dry_run=True)
        detail["plan"] = {"x": int(x), "y": int(y), "channels": list(chans),
                          "retries": retries}
        return ClickResult(ok=True, channel="dry-run", attempts=0, detail=detail)

    # ---- 复核器: 默认像素差 (点前先拍一张, 拍不到就直接报错, 不允许盲点) ----
    verifier: Callable = verify if verify is not None else PixelDiffVerifier(
        x, y, region=region, half=half, diff_min=diff_min,
        capture=capture, resize_fn=resize_fn)
    if verify is None:
        detail["verify"] = "pixel_diff"
        try:
            verifier.reset()          # type: ignore[attr-defined]
        except Exception as exc:      # noqa: BLE001 - 没有复核手段就不点, 明确报错
            msg = (f"无法进行点击复核 (默认像素差需要实时截图): {type(exc).__name__}: {exc}。"
                   "纯图色链路必须有模拟器窗口 (先启动雷电并确认在对局画面), "
                   "或显式传 verify= 一个不依赖截图的复核回调。")
            raise ClickFailed(msg, result=ClickResult(False, None, 0, detail)) from exc
    else:
        detail["verify"] = note or describe_verify(verify, x, y)
    fg_is_adb = bool(getattr(default_config, "ALLOW_FOREGROUND_FALLBACK", False)) is False
    if "fg" in chans and "adb" in chans and fg_is_adb:
        say("[!] fg 通道当前等价 adb (ALLOW_FOREGROUND_FALLBACK=False), "
            "两者都会走 ldconsole adb tap; 想让 fg 真是前台鼠标请把该开关置 True")

    records: list[dict] = detail["records"]
    attempts = 0
    click_errors: list[str] = []
    for rnd in range(1, retries + 1):
        for channel in chans:
            attempts += 1
            rec: dict = {"round": rnd, "channel": channel, "attempt": attempts}
            records.append(rec)
            try:
                _channel_impl(channel)(x, y)
                rec["clicked"] = True
            except Exception as exc:   # noqa: BLE001 - 单通道失败(如 ldconsole 找不到)换下一个
                rec.update(clicked=False, error=f"{channel} 通道点击失败: "
                                                f"{type(exc).__name__}: {exc}")
                click_errors.append(rec["error"])
                say(f"[!] {rec['error']}")
                continue
            if settle > 0:
                time.sleep(settle)
            try:
                ok, extra = _normalize_verify(_call_verify(verifier, rec))
            except Exception as exc:   # noqa: BLE001 - 复核异常也算"这次没验成"
                rec.update(ok=False, error=f"复核异常: {type(exc).__name__}: {exc}")
                say(f"[!] 通道 {channel} 第{rnd}轮 复核异常: {type(exc).__name__}: {exc}")
                continue
            rec.update(ok=ok, **{f"verify_{k}": v for k, v in extra.items() if k != "ok"})
            if ok:
                rec["verify"] = extra
                detail.update(ok=True, channel=channel, attempts=attempts,
                              rounds=rnd, last_verify=extra)
                extra_txt = ", ".join(f"{k}={v}" for k, v in extra.items()
                                      if k in ("diff", "diff_min", "verify"))
                say(f"[OK] 点击 ({int(x)}, {int(y)}) 生效: 通道 {channel} 第{rnd}轮第{attempts}次"
                    f"{(' (' + extra_txt + ')') if extra_txt else ''}")
                return ClickResult(ok=True, channel=channel, attempts=attempts, detail=detail)
            extra_txt = ", ".join(f"{k}={v}" for k, v in extra.items()
                                  if k in ("diff", "diff_min", "verify"))
            say(f"[点击] 通道 {channel} 第{rnd}轮未生效"
                f"{(' (' + extra_txt + ')') if extra_txt else ''}, 换通道/重试")
        # 一轮结束后重拍 before: 下一轮从"当前画面"起算, 否则会把本轮已发生的
        # 微小变化记到下一轮头上 (假阳性)
        if rnd < retries and verify is None:
            try:
                verifier.reset()      # type: ignore[attr-defined]
            except Exception:         # noqa: BLE001 - 拍不到就沿用旧基准, 不致命
                pass
    detail.update(ok=False, channel=None, attempts=attempts, click_errors=click_errors)
    raise ClickFailed(_failure_message(x, y, records, chans, attempts),
                      result=ClickResult(False, None, attempts, detail))


def click_many(plan: Sequence[tuple[float, float, Callable | None]], *,
               settle: float = 0.2, **kw) -> list[ClickResult]:
    """按顺序点多个点 (每个点各自带 verify), 任一失败即抛 ClickFailed (不静默跳过).

    Args:
        plan: [(x, y, verify_or_None), ...]
        其余 kwargs 透传给 `click_with_fallback` (channels / retries / diff_min / dry_run ...)。
    """
    out: list[ClickResult] = []
    for x, y, verify0 in plan:
        out.append(click_with_fallback(x, y, verify=verify0, settle=settle, **kw))
    return out


__all__ = [
    "ClickFailed", "ClickResult", "click_with_fallback", "click_many",
    "PixelDiffVerifier", "pixel_diff", "region_around", "describe_verify",
    "DEFAULT_CHANNELS", "CHANNEL_NOTES", "PIXEL_CHANNEL_THRESHOLD",
]
