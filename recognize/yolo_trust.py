"""YOLO per-class 可信度闸门 (用户 m06113 第 6 条): 只读缓存 + `is_trusted(name)`.

用户口径
--------
「你的 yolo 识别类别是否可信, 要结合你测试的 yolo 各个类别的识别准确率。
**低于一定阈值的, 一律不予采信, 要继续走详情点击去确认。**」

=> "免点击"资格是**逐类别**判定, 不是全局阈值:
     血条说有 + YOLO `score >= hi` + **该类别在白名单里** -> 才允许免点击。

探针侧的唯一判据是 `unit_ok(u)` (用户 m09594 选项 c "只点没被信任的格"):
    `extract_info/roster.py` 用它决定"哪些格不点击", `train_yolo/collect_data_agent/collector.py`
    用它决定"YOLO 来源的名字能不能直接落盘" —— **两处必须问同一个函数**, 否则会出现
    "探针信了、落盘却被闸门扔掉" (每帧整帧丢弃) 或反过来的错标签。

数据来源
--------
`scripts/yolo_class_trust.py` 离线从 manifest 真值 (经详情卡确认的名字) 生成, **提交进仓库**:
  * `config/yolo_trust/v<N>.json`  —— 版本化存档 (**默认读版本号最大的那份**, 用户 m09495);
  * `config/yolo_class_trust.json` —— 最新一份的快捷方式 (兼容老路径)。
表里记了 `version` (哪一版) 和 `model_size` (便于识别"换模型没重跑")。

安全默认 (AGENTS.md §1 "宁可漏采, 不可采错")
-------------------------------------------
任何异常路径一律返回 **不可信**:
  - 表文件不存在 / JSON 坏 / 结构不对;
  - 表里的 `model_size` 与当前在用权重不一致 (说明换了模型却没重跑) -> 整表作废;
  - 类别不在表里 (含 `None` / `unknown`)。
这样最坏后果只是"多点几次详情卡", 绝不会让未验证的名字混进标签。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from config.config import default_config

# 项目根走 config (2026-10-18 exe 打包: 打包后 `__file__` 在解包目录, 不能用它推项目根)。
PROJECT_ROOT = default_config.PROJECT_ROOT
DEFAULT_PATH = PROJECT_ROOT / "config" / "yolo_class_trust.json"     # 最新一份的快捷方式 (兼容老路径)
TRUST_DIR = PROJECT_ROOT / "config" / "yolo_trust"                   # 版本化存档目录

_LOCK = threading.Lock()
_CACHE: dict | None = None
_CACHE_KEY: tuple | None = None
_LAST_ERROR = ""


def latest_table() -> tuple[Path, int | None]:
    """挑信任表: **优先按当前 ONNX 文件名取同名表**, 否则退回老版本号机制。

    用户 m05261: 「名字改的直观一点, 不要 v4 v5, 直接用模型名相同的信任表名」。
    表本来就绑定模型 (`model_size` 不一致整表作废), 按模型名找是**唯一不会配错**的方式:
    线上换 11s 就读 `yolo_v4.1_units_11s.json`, 换 11n 就读 `yolo_v4.1_units_11n.json`,
    不用再靠"最新版本号最大"这种猜测 (两份表可能交替更新, 版本号大小 ≠ 当前在用模型)。

    老 `v<N>.json` 仍兼容 (用户 m09495 的版本号机制), 只在找不到同名表时才用。
    """
    mp = _default_model_path()
    if mp is not None:
        by_model = TRUST_DIR / f"{Path(mp).stem}.json"
        if by_model.exists():
            return by_model, None
    best: tuple[int, Path] | None = None
    if TRUST_DIR.exists():
        for p in TRUST_DIR.glob("v*.json"):
            m = re.fullmatch(r"v(\d+)", p.stem)
            if m and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), p)
    if best is not None:
        return best[1], best[0]
    return DEFAULT_PATH, None


def _default_model_path() -> Path | None:
    try:
        from config.config import default_config
        path = getattr(default_config, "YOLO_UNITS_ONNX", None)
        return Path(path) if path else None
    except Exception:            # noqa: BLE001 - 配置不可用时按"无模型"处理
        return None


def _model_size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path and Path(path).exists() else 0
    except OSError:
        return 0


def load(path: str | Path | None = None, *, model_path: str | Path | None = None,
         force: bool = False) -> dict:
    """读表 (带缓存). 返回 dict; 任何失败都返回 `{"ok": False, "trusted": {}, ...}` 而不是抛异常。"""
    global _CACHE, _CACHE_KEY, _LAST_ERROR
    table_path, table_ver = (Path(path), None) if path else latest_table()
    mp = Path(model_path) if model_path else _default_model_path()
    key = (str(table_path), str(mp) if mp else "", _model_size(mp))
    with _LOCK:
        if not force and _CACHE is not None and _CACHE_KEY == key:
            return _CACHE
        result: dict = {"ok": False, "path": str(table_path), "version": table_ver, "trusted": {},
                        "classes": {}, "reason": "", "generated_at": "", "model_name": "", "model_size": 0}
        try:
            payload = json.loads(table_path.read_text(encoding="utf-8"))
            classes = payload.get("classes") or {}
            if not isinstance(classes, dict):
                raise ValueError("classes 不是 dict")
            recorded_size = int(payload.get("model_size") or 0)
            now_size = _model_size(mp)
            if recorded_size and now_size and recorded_size != now_size:
                result["reason"] = (f"表里的模型大小 {recorded_size} 与当前权重 {now_size} 不一致 "
                                    f"-> 换过模型, 请重跑 scripts/yolo_class_trust.py")
                _LAST_ERROR = result["reason"]
                _CACHE, _CACHE_KEY = result, key
                return result
            result.update({
                "ok": True,
                "trusted": {n: bool(v.get("trusted")) for n, v in classes.items()},
                "classes": classes,
                "generated_at": payload.get("generated_at") or "",
                "model_name": payload.get("model_name") or "",
                "model_size": recorded_size,
                "version": payload.get("version", table_ver),
                "min_acc": payload.get("min_acc"),
                "min_support": payload.get("min_support"),
                "frames": payload.get("frames"),
                "free_rate": payload.get("free_rate"),
                "free_acc": payload.get("free_acc"),
            })
            _LAST_ERROR = ""
        except FileNotFoundError:
            result["reason"] = f"可信度表不存在: {table_path} (请先跑 scripts/yolo_class_trust.py)"
        except Exception as exc:      # noqa: BLE001 - 坏表 = 一律不可信
            result["reason"] = f"可信度表读取失败: {type(exc).__name__}: {exc}"
        if result["reason"]:
            _LAST_ERROR = result["reason"]
        _CACHE, _CACHE_KEY = result, key
        return result


def invalidate() -> None:
    """清缓存 (测试/换模型后用)."""
    global _CACHE, _CACHE_KEY
    with _LOCK:
        _CACHE, _CACHE_KEY = None, None


def last_error() -> str:
    return _LAST_ERROR


def is_trusted(name: str | None, score: float | None = None, path: str | Path | None = None,
               model_path: str | Path | None = None) -> bool:
    """该类别 (在给定 score 下) 是否允许免点击. 表缺失/模型不符/类别未知 一律 False。

    不传 `score` 时只回答"这个类别有没有免点击资格", 不判本次分数。
    """
    threshold = min_score_for(name, path, model_path)
    if threshold is None:
        return False
    if score is None:
        return True
    return float(score) >= threshold


def min_score_for(name: str | None, path: str | Path | None = None,
                  model_path: str | Path | None = None) -> float | None:
    """该类别免点击所需的最低 score (离线逐类校准值); **None = 该类永不赦免点击**。"""
    if not name:
        return None
    entry = info(name, path, model_path)
    if not entry:
        return None
    threshold = entry.get("min_score")
    return float(threshold) if threshold is not None else None


def table_conf(path: str | Path | None = None, *, model_path: str | Path | None = None) -> float:
    """当前信任表**标定时所用的检出 conf**；表缺失/字段无效时回退 `config.YOLO_UNITS_CONF`。

    用户 m06561/m06562: 「每个表, 都应该有自己对应的 conf, 一刀切不是个好办法」
    「要结合实际的 免点率、精确度, 来计算出这个 conf, 才是合理的方案」。
    所以 conf 跟着表走：生成表时把标定用的 conf 写进 `payload["conf"]`，线上读表就用它，
    不再全局写死 0.35（2026-10-29 conf 扫描实测：conf 越低，同精度下免点率越高，见
    `dev_docs/train_yolo/yolo_units_免点率理论上限_20261028.md` §13）。
    """
    table = load(path, model_path=model_path)
    try:
        c = float((table or {}).get("conf") or 0.0)
    except (TypeError, ValueError):
        c = 0.0
    if c > 0.0:
        return c
    return float(default_config.YOLO_UNITS_CONF)


def info(name: str | None, path: str | Path | None = None,
         model_path: str | Path | None = None) -> dict | None:
    """该类别在表里的明细 (support/matched/name_ok/acc/min_score/reason); 查不到返回 None。"""
    if not name:
        return None
    table = load(path, model_path=model_path)
    return (table.get("classes") or {}).get(name)


def unit_ok(u: dict, *, require_bar: bool | None = None) -> bool:
    """探针条目能否**免点击** —— (c) 方案的唯一判据 (用户 m09594 选项 c)。

    必须**全部**满足 (任何异常/字段缺失一律 False: 宁可多点一次详情卡):
      1. `kind == "unit"` 且 `name` 非空 —— 名字为空就得点击去问详情卡;
      2. 不是"同格多框"(`ambiguous`) —— 那是模型自己在两个类别之间拿不定主意
         (实测 3-1 帧 r2c4: 约里克 0.498 / 洛 0.375 同格, 分数分不开), 只能点击;
      3. (require_bar, 缺省取 `config.YOLO_UNITS_UNION_BAR`) 有血条佐证 `bar_seen`;
      4. 该类别在信任表里, 且本次 `score >= 该类的 min_score`。

    3 与 4 是**两道独立闸**: 信任表来自离线 val 集 (245 帧) 的逐类校准, 只管"名字可不可信";
    血条并集来自**当前这一帧**, 管"这里到底有没有东西" —— 实测 95/1079 = 8.8% 的预测框与任何
    真值框 IoU < 0.5 (幻觉框), 凭空多一个单位就是采错, 比漏采严重 (AGENTS §1)。
    """
    try:
        if not isinstance(u, dict):
            return False
        if u.get("kind") != "unit" or not u.get("name"):
            return False
        if u.get("ambiguous"):
            return False
        if require_bar is None:
            try:
                from config.config import default_config
                require_bar = bool(getattr(default_config, "YOLO_UNITS_UNION_BAR", True))
            except Exception:          # noqa: BLE001 - 取不到配置就按最保守的来
                require_bar = True
        if require_bar and not u.get("bar_seen"):
            return False
        return is_trusted(str(u.get("name")), u.get("score"))
    except Exception:                  # noqa: BLE001 - 判据出错就退回"要点击"
        return False


def summary(path: str | Path | None = None, model_path: str | Path | None = None) -> dict:
    """一行状态, 供启动日志用 (GBK 可编码, 不含 emoji)。"""
    table = load(path, model_path=model_path)
    classes = table.get("classes") or {}
    trusted = [n for n, v in classes.items() if v.get("min_score") is not None]
    return {"ok": bool(table.get("ok")), "path": table.get("path"),
            "reason": table.get("reason", ""), "generated_at": table.get("generated_at", ""),
            "model_name": table.get("model_name", ""), "classes": len(classes),
            "version": table.get("version"),
            "trusted_n": len(trusted), "trusted": trusted,
            "free_rate": table.get("free_rate"), "free_acc": table.get("free_acc")}
