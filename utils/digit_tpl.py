"""顶栏数字模板匹配: 用 0~9 字模读 stage(回合号) / round_time(倒计时).

为什么要有它（用户 2026-11-20 m02824 提出）
----------------------------------------
顶栏这两项**每个动作前都要读一次**，而它们原本走 OCR：常驻服务实测
**235~295 ms/次**（冷启动 854ms）。改成固定区域 + 连通域切字 + 0~9 字模
匹配后实测 **4.74 ms/帧**（stage 3.00 + round_time 1.75），快 50~60 倍，
准确率 99.49%（195 帧，含跨日期 holdout）。

  同帧对比：模板 4.74ms  vs  OCR 235~295ms
  端到端（含 PrintWindow 截图 41ms）：模板 ≈46ms  vs  OCR ≈276~336ms
  复用 poll 已经拿到的那一帧时，边际成本只有 4.74ms。

1-x 与 2-x+ 的位置不同（实测，用户提醒后标定）
--------------------------------------------
  1-x   : stage box x 728~804（宽 76，左边还有个装饰图标）, round_time x 1136~1172
  2-x+  : stage box x 663~707（宽 44）,                      round_time x 1227~1262
即 stage 右移 65px、round_time 左移 90px。

**不需要判断"现在该用哪套"**：两套区域读出来的东西天然互斥 ——
  1-x 帧在 2-x+ 区域里 169 次抽样有 167 次只读到一个 `6`（1 位，不合法）、2 次读不到、
      零次读到 2 位合法值；2-1 帧在 1-x 区域里读到 `02`（没有 0 阶段）。
所以规则是「先试 2-x+ 区域 → 合法就用；否则试 1-x 区域 → 合法就用；都不合法返回 None」。

失败语义（安全第一）
------------------
读不到一律返回 ``None``，**绝不猜**：调用方按"不知道"处理（回落 OCR / 走内存判据）。
覆盖不到的场面（加时赛、倒计时变红闪烁、海克斯浮层遮挡、选秀顶栏隐藏、ANR 弹窗）
都是这个分支，不会给出错值。

用法
----
    from utils import digit_tpl

    stage, region, ms = digit_tpl.read_stage(img_bgr)      # ("3-2", "late", 3.0)
    rt, ms = digit_tpl.read_round_time(img_bgr, region)     # ("17", 1.7)
    print(digit_tpl.format_stats())                         # 累计性能 / 失败次数

    # 模板重建（游戏版本或主题变了以后跑一次，几分钟）
    python -X utf8 -u -m utils.digit_tpl --build --limit 520
    # 离线校验
    python -X utf8 -u -m utils.digit_tpl --verify --limit 200
    # 纯性能基准（给复盘用例/人工看）
    python -X utf8 -u -m utils.digit_tpl --bench

详细说明见 docs/顶栏数字模板匹配.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from recognize import regions

ROOT = Path(__file__).resolve().parent.parent

# 模板文件（自举产物；改游戏版本/主题后需重建）
TEMPLATE_PATH = ROOT / "recognize" / "templates" / "stage_digits.npz"

# 归一化尺寸: 顶栏数字实测高 17px、宽 10~14px，统一缩到 (20, 28) 再比
TPL_SIZE = (20, 28)

# ---- 两套区域（1-x / 2-x+）----------------------------------------------
# 2-x+ 与 regions.STAGE 同起点，但右边界收到 713：实测 2-x+ 的 stage box 是
# x 639~711，而 1-x 的 box 从 728 开始 —— 收到 713 可以让两套区域**完全不重叠**，
# 判定互斥性不靠"碰巧读不到"而是靠几何。
STAGE_LATE = (regions.STAGE[0], regions.STAGE[1], 713, regions.STAGE[3])
RT_LATE = regions.ROUND_TIME        # (1216, 5, 1281, 55)
# 1-x 由 1247 张 stage=1-x 真实帧标定（见模块文档）
STAGE_EARLY = (715, 6, 820, 52)
RT_EARLY = (1122, 4, 1190, 52)

# 合法性: 阶段 1..9、回合 1..7（金铲铲最多 7-x）
STAGE_NUMS = "123456789"
ROUND_NUMS = "1234567"

# 连通域最小高度: 真数字高 17px，左侧装饰图标碎片高 10~12px、连字符高 1~3px
MIN_DIGIT_H = 15

# ---- 性能基线（用户 2026-11-20 m02947: 设基线 + 波动阈值, 退化要能发现）----
# 单位 ms，取值来自 195 帧实测（stage 3.00 / rt 1.75 / 合计 4.75）
BASELINE_MS = {"stage": 3.00, "round_time": 1.75, "total": 4.75}
# 相对基线的倍率阈值: <=2x 正常 / 2~4x WARN / >4x ERROR
PERF_WARN_RATIO = 2.0
PERF_ERROR_RATIO = 4.0
# OCR 对照值（只用于文档/报告里算"快了多少倍"）
OCR_REF_MS = {"stage_band": 235.0, "stage_band_slow": 295.0}

_LOCK = threading.Lock()
_TPL_CACHE: tuple[np.ndarray, np.ndarray] | None = None
_STATS = {
    "stage_calls": 0, "stage_ok": 0, "stage_none": 0, "stage_early": 0, "stage_late": 0,
    "rt_calls": 0, "rt_ok": 0, "rt_none": 0,
    "stage_ms_sum": 0.0, "rt_ms_sum": 0.0,
    "stage_ms_max": 0.0, "rt_ms_max": 0.0,
}


# ====================== 模板管理 ======================

def load_templates(path: Path | str | None = None) -> tuple[np.ndarray, np.ndarray]:
    """载入字模 (tpls, labels)，进程内只读一次。

    tpls   : (N, 28, 20) float32 归一化灰度
    labels : (N,) 字符，元素是 "0"~"9"

    文件不存在 → FileNotFoundError（调用方应回落 OCR，不要崩）。
    """
    global _TPL_CACHE
    p = Path(path) if path else TEMPLATE_PATH
    if path is None and _TPL_CACHE is not None:
        return _TPL_CACHE
    if not p.exists():
        raise FileNotFoundError(
            f"数字模板不存在: {p} —— 跑 `python -X utf8 -u -m utils.digit_tpl --build` 重建")
    z = np.load(p, allow_pickle=False)
    tpls, labels = z["tpls"], z["labels"]
    if path is None:
        _TPL_CACHE = (tpls, labels)
    return tpls, labels


def has_templates() -> bool:
    """模板是否就绪（给调用方/复盘用例判断用，不抛异常）。"""
    return TEMPLATE_PATH.exists()


# ====================== 切字 ======================

def _roi_gray(img, x1: int, y1: int, x2: int, y2: int):
    """取 ROI 并转灰度，**同时支持 numpy (cv2 口径) 和 PIL.Image**。

    实机 2026-11-20 踩到（用户 m03339 开实机测试时暴露）：
    `recognize/extract.py` 传进来的是 **PIL.Image**，而本模块原来默认 numpy，
    直接 `img[y1:y2, x1:x2]` 抛
    `TypeError: 'Image' object is not subscriptable` —— 异常被上层吞掉后回落 OCR，
    等于模板这功能根本没生效（还多了一层性能回退）。

    这里对 PIL 走 `img.crop(region).convert("L")`，**只在 ROI 上转**（65×50，
    <0.5ms）；不要 `np.asarray(整图)` 再切片，那样 1080p 整图转换要 3~6ms，
    提速收益会被吃掉一半。numpy 输入维持原路径。
    """
    if isinstance(img, np.ndarray):
        roi = img[y1:y2, x1:x2]
        if roi.size == 0:
            return None
        return cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi
    crop = getattr(img, "crop", None)
    if callable(crop):
        roi = crop((x1, y1, x2, y2))
        conv = getattr(roi, "convert", None)
        return np.asarray(conv("L") if callable(conv) else roi)
    arr = np.asarray(img)
    roi = arr[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    return cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi


def _full_gray(img):
    """整图转灰度（支持 numpy / PIL）。只给低频的 learn_from_ocr 用。"""
    if isinstance(img, np.ndarray):
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    conv = getattr(img, "convert", None)
    if callable(conv):
        return np.asarray(conv("L"))
    arr = np.asarray(img)
    return cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY) if arr.ndim == 3 else arr


def digit_boxes(gray: np.ndarray, min_h: int = MIN_DIGIT_H) -> list[tuple[int, int, int, int]]:
    """在 ROI 灰度图里找数字框，按 x 从左到右返回 [(x, y, w, h), ...]。

    坑（都是实机踩出来的）：
      ① Otsu 阈值失效 —— 背景均值 77、白字 242，Otsu 切出来整块连成 1 个连通域，
         所以用固定高阈值 ``max(150, max*0.70)``。
      ② 连字符 "-" 高 1~3px、装饰图标碎片高 10~12px，真数字高 17px，
         用 ``min_h=15`` 一并滤掉，剩下的就是纯数字。
    """
    thr = max(150, int(gray.max() * 0.70))
    th = (gray > thr).astype(np.uint8) * 255
    th[:1, :] = 0
    th[-1:, :] = 0
    th[:, :1] = 0
    th[:, -1:] = 0
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        bx, by, bw, bh = cv2.boundingRect(c)
        if bw < 2 or bh < min_h:
            continue
        boxes.append((bx, by, bw, bh))
    boxes.sort(key=lambda b: b[0])
    return boxes


def read_field(img: np.ndarray, region, tpls=None, labels=None,
               max_digits: int = 2) -> tuple[str | None, float, int]:
    """读一个区域里的数字串。

    返回 (文本 | None, 耗时 ms, 连通域个数)。
    连通域超过 ``max_digits`` 或一个都没有 → 文本 None（宁可读不到，不可读错）。

    img 需是 BGR（cv2 口径）；region 是 (x1, y1, x2, y2) 绝对坐标。
    """
    t0 = time.perf_counter()
    if tpls is None or labels is None:
        tpls, labels = load_templates()
    x1, y1, x2, y2 = region
    gray = _roi_gray(img, x1, y1, x2, y2)
    if gray is None or gray.size == 0:
        return None, (time.perf_counter() - t0) * 1000, 0
    boxes = digit_boxes(gray)
    if not boxes or len(boxes) > max_digits:
        return None, (time.perf_counter() - t0) * 1000, len(boxes)
    out = []
    for bx, by, bw, bh in boxes:
        patch = cv2.resize(gray[by:by + bh, bx:bx + bw], TPL_SIZE,
                           interpolation=cv2.INTER_AREA)
        best, best_lab = -2.0, "?"
        for tpl, lab in zip(tpls, labels):
            v = float(cv2.matchTemplate(patch, tpl, cv2.TM_CCOEFF_NORMED).max())
            if v > best:
                best, best_lab = v, str(lab)
        out.append(best_lab)
    return "".join(out), (time.perf_counter() - t0) * 1000, len(boxes)


# ====================== 对外识别接口 ======================

def read_stage(img: np.ndarray, tpls=None, labels=None
               ) -> tuple[str | None, str | None, float]:
    """读回合号（如 "3-2"）。

    返回 (stage | None, 命中的区域 "late"/"early" | None, 耗时 ms)。
    先试 2-x+ 区域，不合法再试 1-x 区域；两套都不合法 → (None, None, ms)。
    """
    if tpls is None or labels is None:
        tpls, labels = load_templates()
    t_all = 0.0
    for region, tag in ((STAGE_LATE, "late"), (STAGE_EARLY, "early")):
        txt, ms, _ = read_field(img, region, tpls, labels)
        t_all += ms
        if txt and len(txt) == 2 and txt[0] in STAGE_NUMS and txt[1] in ROUND_NUMS:
            _bump_stage(ok=True, tag=tag, ms=t_all)
            return f"{txt[0]}-{txt[1]}", tag, t_all
    _bump_stage(ok=False, tag=None, ms=t_all)
    return None, None, t_all


def read_round_time(img: np.ndarray, region_tag: str | None,
                    tpls=None, labels=None) -> tuple[str | None, float]:
    """读倒计时秒数。``region_tag`` 由 read_stage 给出（"late"/"early"），

    这样 round_time 跟着 stage 的判定走，不用自己再猜位置。
    region_tag 为 None（stage 都没读到时）→ 两套都试，取第一个能读出的。
    """
    if tpls is None or labels is None:
        tpls, labels = load_templates()
    if region_tag == "early":
        regions_try = ((RT_EARLY, "early"),)
    elif region_tag == "late":
        regions_try = ((RT_LATE, "late"),)
    else:
        regions_try = ((RT_LATE, "late"), (RT_EARLY, "early"))
    for region, _tag in regions_try:
        txt, ms, _ = read_field(img, region, tpls, labels)
        if txt:
            _bump_rt(ok=True, ms=ms)
            return txt, ms
    _bump_rt(ok=False, ms=0.0)
    return None, 0.0


def read_both(img: np.ndarray) -> dict:
    """一次读 stage + round_time（最常用），返回 dict。"""
    stage, tag, ms1 = read_stage(img)
    rt, ms2 = read_round_time(img, tag)
    return {"stage": stage, "stage_region": tag, "round_time": rt,
            "stage_ms": round(ms1, 3), "rt_ms": round(ms2, 3),
            "total_ms": round(ms1 + ms2, 3)}


# ====================== 自动学习（模板过期自救）======================
# 用户 2026-11-20 m03065 口径: 模板失效 → OCR 兜底 + 弹警告; 连续失效报 ERROR 级
# (但不打断运行); 并问「能不能自动更新模板」—— 这里就是: 让 OCR 当老师,
# 从同一帧裁出模板没见过的字模, 攒够一致次数后入库并落盘, 下次启动直接生效。
#
# 为什么要「多次一致才入库」: OCR 自己也会错。单次错标签若直接入库, 模板会被
# 越学越歪(错字模反而抢走正确字模的分数), 比不学更糟。攒够 3 次一致才采纳。

AUTO_LEARN = True                 # 总开关 (config.STAGE_DIGIT_TPL_AUTO_LEARN)
AUTO_LEARN_MIN_AGREE = 3          # 同一数字攒够几次一致才入库 (防 OCR 单次错标签污染)
AUTO_LEARN_SKIP_SIM = 0.90        # 与已有字模相似度 >= 此值 → 视为"已认识", 不学
REPLACE_SIM = 0.70                # 旧字模与新字模相似度 < 此值 → 判定"过时", 淘汰 (用户 m03081)
MAX_PER_DIGIT = 12                # 每个数字最多留几张 (硬上限, 防模板库膨胀)

# 熔断: 连续失败这么多次后, 本局剩余时间直接走 OCR (不再先试模板),
# 保证「模板彻底失效」时性能退化到改造前水平, 而不是每帧白跑一次模板。
BYPASS_AFTER_STREAK = 30

_LEARN = {
    "pending": {},   # digit -> [{"patch", "count", "sigs"}, ...]
    "added": 0,      # 累计入库张数
    "replaced": 0,   # 累计淘汰的过时字模张数 (用户 m03081: 不许膨胀)
    "skipped": 0,    # 已认识 → 跳过
    "rejected": 0,   # 一致性/上限不达标 → 丢弃
    "save_fail": 0,  # 落盘失败次数
}

_PENDING_COMMIT: list[tuple[str, np.ndarray]] = []

_HEALTH = {
    "stage_miss_streak": 0,   # stage 连续读不到次数
    "rt_miss_streak": 0,
    "stage_miss_total": 0,
    "rt_miss_total": 0,
    "ocr_rescue": 0,          # 模板失败但 OCR 救回来
    "bypass": False,          # 熔断: True = 本进程后续直接走 OCR
    "warned": set(),          # 已发过的告警档位 (防刷屏)
}

# 告警档位: 连续失败次数 -> (级别, 文案)
_MISS_TIERS = (
    (10, "WARN", "模板连续读不到 {n} 次, 已回落 OCR 兜底"),
    (25, "ERROR", "模板连续读不到 {n} 次 (约一个回合), 疑似模板过期; 已自动补 {added} 张, "
                  "建议核对 docs/顶栏数字模板匹配.md"),
)
_MISS_BYPASS = 30


def _norm_patch(gray: np.ndarray, box) -> np.ndarray:
    """按框裁字并归一化到 TPL_SIZE (与 read_field 里同一口径)。"""
    bx, by, bw, bh = box
    return cv2.resize(gray[by:by + bh, bx:bx + bw], TPL_SIZE,
                      interpolation=cv2.INTER_AREA)


def _best_sim(patch: np.ndarray, tpls: np.ndarray,
              labels: np.ndarray | None = None) -> tuple[float, str]:
    """与现有字模库比一遍，返回 (最高相似度, 对应标签)。"""
    best, best_lab = -2.0, "?"
    for i, tpl in enumerate(tpls):
        v = float(cv2.matchTemplate(patch, tpl, cv2.TM_CCOEFF_NORMED).max())
        if v > best:
            best = v
            best_lab = str(labels[i]) if labels is not None else "?"
    return best, best_lab


def _save_templates_atomic(tpls: np.ndarray, labels: np.ndarray,
                           path: Path | str | None = None) -> bool:
    """原子落盘 (写 .tmp 再 os.replace)，避免写一半崩掉把模板文件写坏。"""
    p = Path(path) if path else TEMPLATE_PATH
    tmp = p.with_name(p.name + ".tmp")
    try:
        with open(tmp, "wb") as f:  # 传文件对象, 避免 np.savez 自动补 .npz 后缀
            np.savez_compressed(f, tpls=tpls, labels=labels)
        os.replace(tmp, p)
        return True
    except OSError as exc:
        print(f"[!] 字模落盘失败 ({exc!r}), 本次学习只在内存生效", flush=True)
        with _LOCK:
            _LEARN["save_fail"] += 1
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _commit_learned(digit: str, patch: np.ndarray) -> bool:
    """把一个攒够一致次数的字模写进库：**先淘汰过时旧模板，再加新的**。

    用户 2026-11-20 m03081 口径: 识别失败说明旧字模可能已经过时, 要剔除旧的,
    否则模板库只增不减, 迟早膨胀到难处理。所以这里的语义是"替换"而非"追加":

      * 与该 digit 现有字模相似度 < ``REPLACE_SIM``(0.70) → 判为过时, 淘汰
        （字体/描边/主题整体变了时, 旧字模就是这个下场）
      * 相似度 >= 0.70 → 保留（同一数字的另一种渲染状态, 如高亮/普通, 都有用）
      * 每数字硬上限 ``MAX_PER_DIGIT`` 张, 超了淘汰该类最早入库的（数组最靠前的）
      * 淘汰后至少保留 1 张: 新字模本身顶上, 不会出现某数字无模板
    """
    global _TPL_CACHE
    try:
        tpls, labels = load_templates()
    except FileNotFoundError:
        return False
    keep: list[int] = []
    replaced = 0
    for i, (tpl, lab) in enumerate(zip(tpls, labels)):
        if str(lab) != digit:
            keep.append(i)          # 别的数字, 原样保留
            continue
        sim = float(cv2.matchTemplate(patch, tpl, cv2.TM_CCOEFF_NORMED).max())
        if sim < REPLACE_SIM:
            replaced += 1           # 过时 → 淘汰
        else:
            keep.append(i)
    # 硬上限: 同类超了就淘汰最早的
    same = [i for i in keep if str(labels[i]) == digit]
    while len(same) >= MAX_PER_DIGIT:
        drop = same.pop(0)
        keep.remove(drop)
        replaced += 1
    new_tpls = np.concatenate([tpls[keep], patch[None, ...].astype(tpls.dtype)], axis=0)
    new_labels = np.concatenate([labels[keep], np.array([digit], dtype=labels.dtype)])
    ok = _save_templates_atomic(new_tpls, new_labels)
    with _LOCK:
        _TPL_CACHE = (new_tpls, new_labels)  # 内存里立刻生效, 不等下次启动
        _LEARN["added"] += 1
        _LEARN["replaced"] += replaced
        if replaced:
            print(f"[i] 字模库更新: 数字 {digit} 淘汰过时 {replaced} 张, "
                  f"现有 {sum(1 for x in new_labels if str(x) == digit)} 张 "
                  f"(库总 {len(new_labels)} 张)", flush=True)
    return ok


def learn_from_ocr(img: np.ndarray, items, stage: str | None,
                   round_time: int | None) -> int:
    """模板读不到、OCR 读到了 → 用 OCR 的框和标签当老师补字模。

    ``items`` 是 OCR 的原始条目 (含 text/box)，用它定位每个数字的真实位置。
    返回本次新入库的字模张数（多数情况下是 0，攒够一致次数才 +1）。
    """
    if not AUTO_LEARN or not items:
        return 0
    try:
        tpls, labels = load_templates()
    except FileNotFoundError:
        return 0

    # 收集 (标签, 字模) 待学样本
    cands: list[tuple[str, np.ndarray]] = []
    gray = _full_gray(img)

    def _collect(text: str, digits: str) -> None:
        """在 OCR 条目里找到 text 这个条目, 按框切字, 与 digits 逐位对齐。"""
        if not text or not digits:
            return
        box = next((it["box"] for it in items
                    if isinstance(it.get("text"), str) and it["text"].strip() == text
                    and it.get("box")), None)
        if box is None:
            # OCR 条目常带图标噪声前缀 (如 '总 2-6'), 退一步用"包含"匹配
            box = next((it["box"] for it in items
                        if isinstance(it.get("text"), str) and text in it["text"]
                        and it.get("box")), None)
        if box is None:
            return
        x1, y1, x2, y2 = (int(v) for v in box)
        if x2 - x1 < 4 or y2 - y1 < MIN_DIGIT_H:
            return
        roi = gray[max(0, y1):y2, max(0, x1):x2]
        if roi.size == 0:
            return
        boxes = digit_boxes(roi)
        if len(boxes) != len(digits):
            return  # 切出来的字数和标签位数对不上 → 不猜
        for b, d in zip(boxes, digits):
            cands.append((d, _norm_patch(roi, b)))

    if stage:
        _collect(stage, stage.replace("-", ""))
    if round_time is not None:
        _collect(str(round_time), str(round_time))

    added = 0
    with _LOCK:
        pending = _LEARN["pending"]
        for digit, patch in cands:
            sim, _lab = _best_sim(patch, tpls, labels)
            if sim >= AUTO_LEARN_SKIP_SIM:
                _LEARN["skipped"] += 1
                continue
            # 帧去重: 同一帧重复调用不该重复计数, 否则"攒够 3 次"等于 1 帧跑 3 次
            sig = hashlib.md5(patch.tobytes()).hexdigest()
            bucket = pending.setdefault(digit, [])
            hit = None
            for slot in bucket:
                if float(cv2.matchTemplate(patch, slot["patch"],
                                           cv2.TM_CCOEFF_NORMED).max()) >= AUTO_LEARN_SKIP_SIM:
                    hit = slot
                    break
            if hit is not None:
                if sig in hit["sigs"]:
                    continue        # 同一帧的同一个字, 不重复计数
                hit["sigs"].add(sig)
                hit["count"] += 1
                if hit["count"] >= AUTO_LEARN_MIN_AGREE:
                    bucket.remove(hit)
                    _PENDING_COMMIT.append((digit, hit["patch"]))
                    added += 1
            elif len(bucket) < 8:
                bucket.append({"patch": patch, "count": 1, "sigs": {sig}})
            else:
                _LEARN["rejected"] += 1
    # 落盘放在锁外 (I/O 不占锁)
    for digit, patch in _PENDING_COMMIT:
        _commit_learned(digit, patch)
    _PENDING_COMMIT.clear()
    return added


_PENDING_COMMIT: list[tuple[str, np.ndarray]] = []


def note_miss(kind: str, rescued: bool = False) -> str | None:
    """记一次「模板读不到」。返回需要打印的告警文本（None = 不用打）。

    ``kind`` 取 "stage" / "round_time"；``rescued`` 表示本次 OCR 兜底救回来了。
    分级见 ``_MISS_TIERS``：连续 10 次 WARN、连续 25 次 ERROR；
    连续 ``BYPASS_AFTER_STREAK`` 次触发熔断（后续直接走 OCR，不再白试模板）。
    """
    msgs = []
    with _LOCK:
        # _HEALTH 里的键用短前缀: stage_* / rt_*（round_time 缩成 rt）
        pre = "rt" if kind.startswith("rt") or kind.startswith("round") else "stage"
        key = f"{pre}_miss_streak"
        _HEALTH[key] = _HEALTH.get(key, 0) + 1
        _HEALTH[f"{pre}_miss_total"] += 1
        if rescued:
            _HEALTH["ocr_rescue"] += 1
        streak = _HEALTH[key]
        for n, level, text in _MISS_TIERS:
            mark = f"{pre}:{level}"
            if streak >= n and mark not in _HEALTH["warned"]:
                _HEALTH["warned"].add(mark)
                msgs.append(f"[{level if level == 'ERROR' else '!'}] "
                            f"{text.format(n=streak, added=_LEARN['added'])}")
        if streak >= _MISS_BYPASS and not _HEALTH["bypass"]:
            _HEALTH["bypass"] = True
            msgs.append(f"[X] 模板连续 {streak} 次失效 → 本进程熔断, 后续直接走 OCR "
                        f"(性能回到改造前, 不影响正确性)")
    return "\n".join(msgs) if msgs else None


def note_hit(kind: str) -> None:
    """模板读成功 → 清零该类连续失败计数，并解除熔断。"""
    pre = "rt" if kind.startswith("rt") or kind.startswith("round") else "stage"
    with _LOCK:
        _HEALTH[f"{pre}_miss_streak"] = 0
        if _HEALTH["bypass"]:
            _HEALTH["bypass"] = False
            _HEALTH["warned"].discard(f"{pre}:WARN")
            _HEALTH["warned"].discard(f"{pre}:ERROR")


def bypassed() -> bool:
    """是否已熔断（True = 调用方应直接走 OCR，别再试模板）。"""
    with _LOCK:
        return bool(_HEALTH["bypass"])


def health() -> dict:
    """模板健康度快照（给 runner 局末打印 / 复盘用例 PR22 用）。"""
    with _LOCK:
        return {**_HEALTH, "warned": sorted(_HEALTH["warned"]),
                "added": _LEARN["added"], "replaced": _LEARN["replaced"],
                "skipped": _LEARN["skipped"],
                "rejected": _LEARN["rejected"], "save_fail": _LEARN["save_fail"]}


def reset_health() -> None:
    """新一局开始时清空健康度（累计入库数保留，那是跨局的成果）。"""
    with _LOCK:
        for k in ("stage_miss_streak", "rt_miss_streak", "stage_miss_total",
                  "rt_miss_total", "ocr_rescue"):
            _HEALTH[k] = 0
        _HEALTH["bypass"] = False
        _HEALTH["warned"] = set()


def format_health(prefix: str = "[性能]") -> str:
    """一行健康度摘要，例如给 runner 局末打：
    ``[性能] 数字模板: stage 400次 命中399 ... | 兜底 1 次 自动补 2 张``"""
    h = health()
    st = h["stage_miss_total"]
    rt = h["rt_miss_total"]
    return (f"{prefix} 数字模板兜底: stage 未命中 {st} 次 / rt 未命中 {rt} 次 / "
            f"OCR 救回 {h['ocr_rescue']} 次 / 自动补字模 {h['added']} 张 "
            f"淘汰过时 {h['replaced']} 张 (跳过已认识 {h['skipped']}, "
            f"丢弃 {h['rejected']}, 落盘失败 {h['save_fail']})")


# ====================== 性能统计 ======================

def _bump_stage(ok: bool, tag: str | None, ms: float) -> None:
    with _LOCK:
        _STATS["stage_calls"] += 1
        _STATS["stage_ms_sum"] += ms
        _STATS["stage_ms_max"] = max(_STATS["stage_ms_max"], ms)
        if ok:
            _STATS["stage_ok"] += 1
            _STATS["stage_late" if tag == "late" else "stage_early"] += 1
        else:
            _STATS["stage_none"] += 1


def _bump_rt(ok: bool, ms: float) -> None:
    with _LOCK:
        _STATS["rt_calls"] += 1
        _STATS["rt_ms_sum"] += ms
        _STATS["rt_ms_max"] = max(_STATS["rt_ms_max"], ms)
        _STATS["rt_ok" if ok else "rt_none"] += 1


def stats() -> dict:
    """当前进程累计统计（线程安全快照）。"""
    with _LOCK:
        s = dict(_STATS)
    sc = max(1, s["stage_calls"])
    rc = max(1, s["rt_calls"])
    s["stage_avg_ms"] = round(s["stage_ms_sum"] / sc, 3)
    s["rt_avg_ms"] = round(s["rt_ms_sum"] / rc, 3)
    s["stage_hit_rate"] = round(s["stage_ok"] / sc, 4)
    s["rt_hit_rate"] = round(s["rt_ok"] / rc, 4)
    s["stage_ms_max"] = round(s["stage_ms_max"], 3)
    s["rt_ms_max"] = round(s["rt_ms_max"], 3)
    return s


def reset_stats() -> None:
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0.0 if k.endswith("_ms_sum") or k.endswith("_ms_max") else 0


def format_stats(prefix: str = "[性能]") -> str:
    """一行摘要，给 runner 局末打印 / 复盘用例 grep。"""
    s = stats()
    if not s["stage_calls"]:
        return f"{prefix} 数字模板: 本局未调用"
    return (f"{prefix} 数字模板: stage {s['stage_calls']}次 命中{s['stage_ok']} "
            f"均{s['stage_avg_ms']}ms 峰{s['stage_ms_max']}ms (late{s['stage_late']}/early{s['stage_early']}) | "
            f"rt {s['rt_calls']}次 命中{s['rt_ok']} 均{s['rt_avg_ms']}ms 峰{s['rt_ms_max']}ms")


def check_perf(stage_ms: float | None = None, rt_ms: float | None = None,
               total_ms: float | None = None) -> dict:
    """与基线对比，返回 {"level": "OK"|"WARN"|"ERROR", "detail": str, ...}。

    用户口径（m02947）：设基线 + 波动阈值，退化要及时发现。
    倍率 <=2x 正常 / 2~4x WARN / >4x ERROR（阈值见 PERF_WARN_RATIO / PERF_ERROR_RATIO）。

    total_ms 给了就按它算（现场只测到合计时用）；否则 = stage_ms + rt_ms。
    """
    stage_ms = BASELINE_MS["stage"] if stage_ms is None else stage_ms
    rt_ms = BASELINE_MS["round_time"] if rt_ms is None else rt_ms
    total = (stage_ms + rt_ms) if total_ms is None else total_ms
    ratio = total / BASELINE_MS["total"] if BASELINE_MS["total"] else 0.0
    if ratio > PERF_ERROR_RATIO:
        level = "ERROR"
    elif ratio > PERF_WARN_RATIO:
        level = "WARN"
    else:
        level = "OK"
    return {
        "level": level, "ratio": round(ratio, 3),
        "stage_ms": round(stage_ms, 3), "rt_ms": round(rt_ms, 3),
        "total_ms": round(total, 3), "baseline_ms": BASELINE_MS["total"],
        "warn_ratio": PERF_WARN_RATIO, "error_ratio": PERF_ERROR_RATIO,
        "detail": (f"识别耗时 {total:.2f}ms = 基线 {BASELINE_MS['total']:.2f}ms 的 "
                   f"{ratio:.2f}x（WARN>{PERF_WARN_RATIO:g}x / ERROR>{PERF_ERROR_RATIO:g}x）"),
    }


# ====================== 模板自举 ======================

def _find_frames(limit: int, seed: int = 20261120,
                 base_dir: Path | str | None = None) -> list[Path]:
    """从截图目录随机抽帧（模板自举/验证的原料）。

    两种目录布局都支持：
      1. 目录下直接放 png（overlay 客户端「保存帧」写出的 `tempdir/frames/`）；
      2. 目录下是若干子目录，每个子目录里放 png（采集器按日期分桶的布局）。
    不给 `base_dir` 时依次找 `workspace/agent_debug`、`tempdir/frames`。
    """
    if base_dir:
        bases = [Path(base_dir)]
    else:
        bases = [ROOT / "workspace" / "agent_debug", ROOT / "tempdir" / "frames"]
    base = next((b for b in bases if b.is_dir()), None)
    if base is None:
        return []
    rng = random.Random(seed)
    flat = sorted(base.glob("*.png"))
    if flat:                                   # 扁平布局
        rng.shuffle(flat)
        return flat[:limit]
    dirs = sorted(d for d in base.iterdir() if d.is_dir())
    rng.shuffle(dirs)
    out: list[Path] = []
    per = max(1, limit // max(1, len(dirs)))
    for d in dirs:
        imgs = sorted(d.glob("*.png"))
        if not imgs:
            continue
        rng.shuffle(imgs)
        out.extend(imgs[:per])
        if len(out) >= limit:
            break
    return out[:limit]


def build_templates(limit: int = 520, out_path: Path | str | None = None,
                    per_digit: int = 6,
                    frames_dir: Path | str | None = None) -> dict:
    """从真实截图自举 0~9 字模。

    做法：对每张图跑一次顶栏 OCR 拿 stage/round_time 文本（= 标签），
    再在自己的固定区域里切连通域、逐字符打标签；每个数字留 ``per_digit`` 张
    （取灰度标准差最大的 = 最清晰的）做多模板。

    依赖 OCR（只在重建时用一次，运行时不用）。
    """
    from recognize import extract, ocr

    out = Path(out_path) if out_path else TEMPLATE_PATH
    frames = _find_frames(limit, base_dir=frames_dir)
    if not frames:
        raise SystemExit("没找到可用截图: 指定 --dir, 或先往 "
                         "workspace/agent_debug / tempdir/frames 里存几局截图")
    print(f"[重建] 抽帧 {len(frames)} 张，开始 OCR 打标签（较慢，几分钟）", flush=True)

    pools: dict[str, list[tuple[float, np.ndarray]]] = {d: [] for d in "0123456789"}
    used = 0
    for i, f in enumerate(frames, 1):
        try:
            img = np.array(cv2.imread(str(f)))
            if img is None:
                continue
            items = ocr.recognize_region(img, extract.STAGE_BAND, 1) or []
            stage = extract.extract_stage(items) if hasattr(extract, "extract_stage") else None
            rt = extract.extract_round_time(items, img) if stage else None
        except Exception:
            continue
        if not stage:
            continue
        used += 1
        for region, text in ((STAGE_LATE, str(stage).replace("-", "")),
                             (STAGE_EARLY, str(stage).replace("-", "")),
                             (RT_LATE, str(rt) if rt is not None else ""),
                             (RT_EARLY, str(rt) if rt is not None else "")):
            if not text or len(text) > 2:
                continue
            x1, y1, x2, y2 = region
            gray = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            boxes = digit_boxes(gray)
            if len(boxes) != len(text):
                continue
            for (bx, by, bw, bh), ch in zip(boxes, text):
                if ch not in pools:
                    continue
                patch = cv2.resize(gray[by:by + bh, bx:bx + bw], TPL_SIZE,
                                   interpolation=cv2.INTER_AREA)
                score = float(patch.std())        # 越清晰（对比越强）越好
                pools[ch].append((score, patch))
        if i % 50 == 0:
            got = sum(len(v) for v in pools.values())
            print(f"  [{i}/{len(frames)}] 已收样本 {got} 张", flush=True)

    tpls, labels = [], []
    summary = {}
    for ch, items in pools.items():
        items.sort(key=lambda t: -t[0])
        keep = items[:per_digit]
        summary[ch] = len(items)
        for _score, patch in keep:
            tpls.append(patch.astype(np.float32))
            labels.append(ch)
    if not tpls:
        raise SystemExit("没采到任何字模样本（OCR 打标签失败？）")
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, tpls=np.stack(tpls), labels=np.array(labels))
    print(f"[重建] 完成: {len(tpls)} 张模板 -> {out}", flush=True)
    print(f"[重建] 每数字样本数: {summary}", flush=True)
    return {"templates": len(tpls), "out": str(out), "samples": summary,
            "frames": len(frames), "labeled": used}


# ====================== CLI ======================

def _cli() -> int:
    ap = argparse.ArgumentParser(description="顶栏数字模板匹配 (stage / round_time)")
    ap.add_argument("--build", action="store_true", help="从真实截图重建 0~9 字模")
    ap.add_argument("--verify", action="store_true", help="对真实截图做准确率/耗时验证")
    ap.add_argument("--bench", action="store_true", help="只跑纯性能基准")
    ap.add_argument("--limit", type=int, default=200, help="抽帧数")
    ap.add_argument("--dir", default=None,
                    help="截图目录（默认找 workspace/agent_debug, 再找 tempdir/frames）")
    ap.add_argument("--out", default=None, help="模板输出路径（默认 recognize/templates/stage_digits.npz）")
    args = ap.parse_args()

    if args.build:
        r = build_templates(limit=args.limit, out_path=args.out, frames_dir=args.dir)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    if not has_templates():
        print(f"模板不存在: {TEMPLATE_PATH}（先 --build）")
        return 2

    from recognize import extract, ocr
    frames = _find_frames(args.limit, base_dir=args.dir)
    if not frames:
        print("没找到可用截图（用 --dir 指定目录）")
        return 2
    tpls, labels = load_templates()
    print(f"[验证] 模板 {len(tpls)} 张 / 帧 {len(frames)} 张", flush=True)

    n = ok = none = 0
    rt_n = rt_ok = 0
    ms_sum = 0.0
    ms_list: list[float] = []
    wrong: list[str] = []
    for f in frames:
        img = cv2.imread(str(f))
        if img is None:
            continue
        try:
            items = ocr.recognize_region(img, extract.STAGE_BAND, 1) or []
            st_ocr = extract.extract_stage(items) if hasattr(extract, "extract_stage") else None
            rt_ocr = extract.extract_round_time(items, img) if st_ocr else None
        except Exception:
            continue
        if not st_ocr:
            continue
        n += 1
        t0 = time.perf_counter()
        st, tag, _ = read_stage(img, tpls, labels)
        rt, _ = read_round_time(img, tag, tpls, labels)
        dt = (time.perf_counter() - t0) * 1000
        ms_sum += dt
        ms_list.append(dt)
        if st is None:
            none += 1
            if len(wrong) < 10:
                wrong.append(f"{f.name} OCR={st_ocr} TPL=None")
        elif st == st_ocr:
            ok += 1
        elif len(wrong) < 10:
            wrong.append(f"{f.name} OCR={st_ocr} TPL={st} 区域={tag}")
        if rt_ocr is not None:
            rt_n += 1
            if rt == str(rt_ocr):
                rt_ok += 1
        if n % 50 == 0:
            print(f"  [{n}] stage_ok={ok} none={none}", flush=True)

    ms_list.sort()
    p95 = ms_list[int(len(ms_list) * 0.95)] if ms_list else 0.0
    print("")
    print(f"stage  正确 {ok}/{n} = {100.0*ok/max(1,n):.2f}%  None {none}")
    print(f"rt     正确 {rt_ok}/{rt_n} = {100.0*rt_ok/max(1,rt_n):.2f}%")
    print(f"耗时   平均 {ms_sum/max(1,n):.2f} ms  P95 {p95:.2f} ms")
    perf = check_perf(total_ms=ms_sum / max(1, n))
    print(f"基线   {perf['detail']} -> {perf['level']}")
    if wrong:
        print("失败样例:")
        for s in wrong:
            print("   ", s)
    if args.bench:
        print(json.dumps({"n": n, "stage_ok": ok, "rt_ok": rt_ok,
                          "avg_ms": round(ms_sum / max(1, n), 3), "p95_ms": round(p95, 3),
                          "perf": perf}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())