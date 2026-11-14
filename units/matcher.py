"""棋子头像识别: 官方头像模板库 + 掩码 NCC, 给出名字与分数.

纯图色口径 (2026-11-14 新工程 `jcc_robot_by_img`)
------------------------------------------------
两条实现路径, 由素材是否到位自动选择 (不靠 config 开关):

1. **首选 portrait_match** (`utils/portrait_match.py` + `units/templates_alpha/` 138 张):
   透明蒙版库 (GrabCut 去背景) + 前景交集 Pearson 相关 + 多尺度/水平翻转,
   是这套图色方案原本的头像口径。库存在就用它。
2. **兜底 art_fallback**: `utils/portrait_match` 或其模板库缺失时, 退回 `units.db` 里
   每只棋子的 `image` (即 `workspace/scrape_info/v18.3b/images/units/<chessId>.jpg`,
   65 张 96×96 官方原画) 做整图 `cv2.TM_CCOEFF_NORMED` 单点相关。

阈值口径不变: 高信线 `DEFAULT_THRESHOLD = 0.55` (等价 portrait_match.DEFAULT_THRESHOLD),
低信复核线 `config.MATCH_LOWCONF = 0.10` (match_lowconf 用)。
**宁可返回 None, 不猜名字** —— 错标签比漏标更糟; 认不出的身份由调用方交给 YOLO 或标记 unknown。

已知局限 (实测, 写进 T1 报告): 官方原画(大头特写) 与实机 3D 小模型形态差异大,
纯原画库 NCC 分数只有 0.14~0.39, 全库排名多在 10 名外 —— 所以 identity 的**主力**是
YOLO(0.97+), 头像匹配只作交叉校验/血条路径兜底。
"""

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"   # 旧截图模板目录 (仅 match_legacy 用)

DEFAULT_THRESHOLD = 0.55
TEMPLATE_SIZE = 96          # art_fallback: 官方头像 96×96

_lib: list[tuple[str, np.ndarray]] | None = None   # art_fallback 库: [(棋子名, RGB 模板)]
_lib_error: str | None = None
_pm: object | None = None                          # utils.portrait_match 模块 / False(不可用)
_warned = False


# ---------------------------------------------------------------- 路径 1: portrait_match
def _pm_module():
    """惰性导入 utils.portrait_match; 不可用返回 None (只 import 失败, 不抛异常)."""
    global _pm, _warned
    if _pm is False:
        return None
    if _pm is None:
        try:
            from utils import portrait_match as _mod
            _pm = _mod
        except Exception as exc:  # noqa: BLE001 - 素材/模块缺失只降级, 不杀主链路
            _pm = False
            if not _warned:
                _warned = True
                print(f"[!] utils.portrait_match 不可用 ({type(exc).__name__}: {exc}) "
                      f"-> 头像匹配降级到官方原画库", flush=True)
            return None
    return _pm


def _pm_lib():
    """共享模板库; 库为空/加载失败返回 None."""
    mod = _pm_module()
    if mod is None:
        return None
    try:
        lib = mod.TemplateLib.shared()
        if not lib.names:                     # 触发 load(), 空库视为不可用
            return None
        return lib
    except Exception as exc:  # noqa: BLE001 - 模板库坏了就当没有
        global _warned
        if not _warned:
            _warned = True
            print(f"[!] portrait_match 模板库加载失败 ({type(exc).__name__}: {exc}) "
                  f"-> 降级到官方原画库", flush=True)
        return None


def library_source() -> str:
    """当前实际生效的头像库: 'portrait_match' / 'art_fallback' / 'none'."""
    if _pm_lib() is not None:
        return "portrait_match"
    return "art_fallback" if _load_library() else "none"


def library_size() -> int:
    """模板库条目数 (portrait_match 按名字数, 兜底库按素材张数; 0 = 身份识别不可用)."""
    lib = _pm_lib()
    if lib is not None:
        try:
            return len(lib.names)
        except Exception:  # noqa: BLE001
            return 0
    return len(_load_library())


def variants() -> int:
    """模板变体总数 (portrait_match 的口径: 同一棋子可能有 v2~v5 多个姿态)."""
    lib = _pm_lib()
    if lib is not None:
        try:
            return int(lib.n_variants)
        except Exception:  # noqa: BLE001
            return 0
    return len(_load_library())


# ---------------------------------------------------------------- 路径 2: 原画兜底库
def _load_library() -> list[tuple[str, np.ndarray]]:
    """懒加载官方原画兜底库: [(棋子名, 96×96 RGB ndarray)]."""
    global _lib, _lib_error, _warned
    if _lib is not None:
        return _lib
    lib: list[tuple[str, np.ndarray]] = []
    try:
        from units import db
        for unit in (db.load_db().get("units") or []):
            name = unit.get("name")
            if not name:
                continue
            path = db.unit_image(unit)
            if not path or not Path(path).exists():
                continue
            try:
                tpl = np.asarray(Image.open(path).convert("RGB").resize(
                    (TEMPLATE_SIZE, TEMPLATE_SIZE), Image.LANCZOS))
            except Exception:  # noqa: BLE001 - 单张素材坏了就跳过
                continue
            lib.append((str(name), tpl))
        if not lib:
            _lib_error = "units.db 里没有任何棋子头像素材 (unit['image'] 指向的文件都不存在)"
    except Exception as exc:  # noqa: BLE001 - 数据库不可用: 空库 + 留痕
        _lib_error = f"{type(exc).__name__}: {exc}"
        lib = []
    if not lib and not _warned:
        _warned = True
        print(f"[X] 棋子头像模板库为空 ({_lib_error or '未知原因'}) -> 身份识别一律返回 None "
              f"(位置/血条仍可用)", flush=True)
    _lib = lib
    return lib


def reset() -> None:
    """清空两个模板库缓存 (调试/换素材后强制重建)."""
    global _lib, _lib_error, _pm, _warned
    _lib = None
    _lib_error = None
    _pm = None
    _warned = False
    mod = _pm_module()
    if mod is not None:
        try:
            mod.TemplateLib.reload()
        except Exception:  # noqa: BLE001
            pass


def _probe_rgb(portrait) -> np.ndarray | None:
    """探测头像 -> 96×96 RGB (兜底库口径); 尺寸不合法返回 None."""
    if portrait is None:
        return None
    try:
        rgb = portrait.convert("RGB") if isinstance(portrait, Image.Image) \
            else Image.fromarray(np.asarray(portrait)[:, :, :3]).convert("RGB")
    except Exception:  # noqa: BLE001 - 非图像对象按"抠不出"处理
        return None
    if rgb.width < 8 or rgb.height < 8:
        return None
    return np.asarray(rgb.resize((TEMPLATE_SIZE, TEMPLATE_SIZE), Image.LANCZOS))


def _ranked_art(portrait, candidates: list[str] | None = None) -> list[tuple[str, float]]:
    """原画兜底库打分 (降序)."""
    probe = _probe_rgb(portrait)
    if probe is None:
        return []
    lib = _load_library()
    if not lib:
        return []
    want = None if candidates is None else {str(c).split("__")[0] for c in candidates}
    ranked: list[tuple[str, float]] = []
    for name, tpl in lib:
        if want is not None and name not in want:
            continue
        try:
            score = float(cv2.matchTemplate(probe, tpl, cv2.TM_CCOEFF_NORMED)[0, 0])
        except Exception:  # noqa: BLE001 - 个别模板尺寸异常: 跳过
            continue
        if not np.isfinite(score):
            continue
        ranked.append((name, score))
    ranked.sort(key=lambda kv: kv[1], reverse=True)
    return ranked


# ---------------------------------------------------------------- 统一入口
def _ranked(portrait, candidates: list[str] | None = None,
            flip: bool = True, scales=None, mode: str = "mask") -> list[tuple[str, float]]:
    """全部模板打分降序 (portrait_match 优先, 否则原画兜底)."""
    lib = _pm_lib()
    if lib is not None:
        try:
            return list(lib.match(portrait, candidates=candidates, flip=flip,
                                  scales=scales, mode=mode))
        except Exception as exc:  # noqa: BLE001 - 单次比对异常退回兜底库
            print(f"[!] portrait_match.match 异常 ({type(exc).__name__}: {exc}) "
                  f"-> 本次退回原画库", flush=True)
    return _ranked_art(portrait, candidates)


def match(portrait, threshold: float | None = None) -> tuple[str | None, float]:
    """识别一个头像 (透明蒙版库 / 原画兜底), 高信线口径.

    Args:
        portrait: 约 104×89 的头像 PIL Image (RGB, 1920×1080 原生几何).
        threshold: 相似度阈值, None 则用 0.55; 低于阈值视为未知 (小小英雄/新单位/遮挡).

    Returns:
        (棋子名 | None, 相似度). 无模板或低于阈值时名字为 None.
    """
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    ranked = _ranked(portrait)
    if not ranked:
        return None, 0.0
    name, score = ranked[0]
    score = round(float(score), 3)
    return (name if score >= threshold else None), score


def match_lowconf(portrait, *, threshold: float | None = None
                  ) -> tuple[str | None, float]:
    """低阈值复核: 0.10 以上一律给 top1 名字, 不返回 None (用户 2026-10-21 口径).

    场景: 某个格已经确定"这里有个单位"(血条定位到), 但高信线 0.55 没比出来 ——
    这时不许留 unknown, 必须按 top1 给名字并让调用方标记低置信度。
    阈值来源 ``config.MATCH_LOWCONF`` (0.10)。
    """
    from config.config import default_config

    thr = float(default_config.MATCH_LOWCONF) if threshold is None else float(threshold)
    ranked = _ranked(portrait)
    if not ranked:
        return None, 0.0
    name, score = ranked[0]
    score = round(float(score), 3)
    return (name if score >= thr else None), score


def match_ranked(portrait, *, candidates: list[str] | None = None,
                 topk: int = 3) -> list[tuple[str, float]]:
    """候选制排名 (YOLO 先给 2~3 个候选时最准也最快).

    Args:
        portrait: 104×89 头像.
        candidates: 只比这些名字的模板; None 表示全库.
        topk: 返回前几名.

    Returns:
        [(名字, 分数)] 降序; 空列表表示没有可比模板/抠不出头像.
    """
    k = max(1, int(topk))
    return [(str(n), round(float(s), 3)) for n, s in _ranked(portrait, candidates)[:k]]


def match_legacy(portrait, threshold: float | None = None) -> tuple[str | None, float]:
    """旧实现: 整图 TM_CCOEFF_NORMED, 遍历 `units/templates/*.png` (对照/回滚用).

    截图模板对裁偏/装备图标极其敏感, 保留只为兼容历史调用方签名 ——
    无模板时返回 (None, 0.0), 不猜。
    """
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    templates = sorted(TEMPLATE_DIR.glob("*.png"))
    if not templates:
        return None, 0.0
    probe = np.array(Image.fromarray(np.asarray(portrait)[:, :, :3]).convert("RGB")
                     if not isinstance(portrait, Image.Image)
                     else portrait.convert("RGB"))
    best_name, best = None, -1.0
    for path in templates:
        tpl = np.array(Image.open(path).convert("RGB"))
        score = float(cv2.matchTemplate(probe, tpl, cv2.TM_CCOEFF_NORMED).max())
        if score > best:
            # 多变体模板命名约定: "名字__v2.png" = 同一棋子的第二个姿态/高光态
            best_name, best = path.stem.split("__")[0], score
    return (best_name if best >= threshold else None), round(best, 3)
