"""YOLO 单次前向识别我方奕子 (onnxruntime) —— 替代"逐格点击读详情卡"的 20s 链路。

任务口径 (用户 2026-10-12)
--------------------------
给定**一张 prep 阶段截图**, 一次前向就给出我方奕子的**位置 + 类别**; 目标 <1s
(现网 `extract_info.roster.extract_live_roster` 要 16~25s: 12~18 次点击 × 0.8~1.0s)。

本模块的契约
------------
`extract_units(img)` 的返回 shape 与 `recognize.board.extract_units` **完全一致**
(`{"board": [unit, ...], "bench": [unit, ...]}`), 所以 `extract_info.roster.locate_roster`
可以一行切换过去, 下游 (board_info / dynamic_probe / runner / prompt) 零改动。
每个 unit:
    name          —— 冻结类别表 (train_yolo/classes.json) 里的真名; 置信度不够时 None (不猜)
    score         —— 检出置信度
    box / rect    —— 整帧像素框 (x1, y1, x2, y2)
    center        —— 框中心
    cell_anchor   —— 送进棋盘映射的落脚点 (= 框顶 + BOARD_BAR_TO_CELL_Y, 与血条法同源)
    location      —— grid.board_position / bench_position 的结果 (row/column/own_row 或 slot)
    stars         —— None (YOLO 不认星级; 要星级仍走详情卡)
    kind          —— "unit"
    source        —— "yolo"
    identity_source —— "yolo"
    verified      —— False (verified 的语义是"详情卡确认过", YOLO 不冒充)

为什么直接吃 1920×1080 全帧
---------------------------
训练框就是 `utils/yolo_unit_box.yolo_unit_rects` 的"整格框" (宽=血条宽, top=血条下边,
bottom=格底), 实测 82×(114~146)px。所以推理侧的格位映射沿用同一套常量, YOLO 与血条法
对同一只奕子会落到**同一个格**, 方便 A/B 对拍。

设计取舍
--------
- 依赖只有 `onnxruntime` + `numpy` (+ PIL 由调用方传入的 img 决定): 主环境**不装 torch**;
- 会话惰性单例 + 预分配输入张量: 每帧零额外分配, CPU 上 1280 输入约 0.1~0.3s;
- NMS 自己用 numpy 实现 (不引入 cv2 依赖, 也便于 --selftest 无模型验证);
- 类别号 → 名字 一律走 `train_yolo.class_table` (冻结表), 与训练/标注同一张表。

自检 (不需要模型):
    python -m recognize.yolo_units --selftest
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:      # 允许 `python recognize/yolo_units.py --selftest` 直跑
    sys.path.insert(0, str(PROJECT_ROOT))

from config.config import default_config                      # noqa: E402
from recognize.grid import bench_position, board_position     # noqa: E402
from train_yolo import class_table                            # noqa: E402

# 框 → 格位的换算常量 (与 recognize/board.py + utils/yolo_unit_box.py 同源)
#   棋盘: cell_anchor = (cx, 框顶 + BOARD_BAR_TO_CELL_Y)   ← board.py: cell_anchor = y2_bar + 100
#   备战席: bench 判定点 = (cx, 框顶 + BENCH_CENTER_DY)      ← board.py: center = y2_bar + (3+92)/2
BENCH_CENTER_DY = 47
LEGEND_NAME = "小小英雄"          # 非奕子附加类 (与 train_yolo/classes.json 的 extra 一致)

_SESSION = None                 # 最近一次用到的会话 (兼容旧引用)
_SESSION_KEY: tuple | None = None
_SESSIONS: dict = {}            # (path, providers) → ort.InferenceSession (多模型共存, 用户 m02239)
_LAST_TIMING: dict = {}
_LAST_STATS: dict = {}
_LAST_BALLS: list = []          # 上次被丢弃的 ball 框 (仅供调试图框选, 不进奕子名单)
_LAST_LEGENDS: list = []        # 上次被丢弃的小小英雄框 (仅供调试图框选, 不进奕子名单)
_WARNED: dict = {}              # 一次性告警 (如 ONNX 类别数与冻结表不一致)
_IMGSZ_OVERRIDE: int | None = None   # 自检/--fake: 强制 imgsz, 不查模型


# --------------------------------------------------------------------------- #
# 会话 / 可用性
# --------------------------------------------------------------------------- #
def onnx_path() -> Path:
    """认"奕子"的权重 (默认模型)。"""
    return Path(default_config.YOLO_UNITS_ONNX)


def legend_onnx_path() -> Path:
    """认"小小英雄"的权重 (用户 m02239: 11s 模型缺"小小英雄"类, 单独走 v2.1)。"""
    p = getattr(default_config, "YOLO_LEGEND_ONNX", None)
    return Path(p) if p else onnx_path()


def ball_onnx_path() -> Path:
    """认"奖励球 ball"的权重 (用户 m02239: 11s 模型缺"ball"类, 单独走 v2.1)。"""
    p = getattr(default_config, "YOLO_BALL_ONNX", None)
    return Path(p) if p else onnx_path()


def debug_onnx_path() -> Path:
    """采集/调试图框选用权重 (一次推理同时拿"球 + 小小英雄"两类)。"""
    p = getattr(default_config, "YOLO_DEBUG_ONNX", None)
    return Path(p) if p else onnx_path()


#: 功能点 → config 里的模型路径常量名 (用户 m02239/m02354「每个功能点用特定 yolo, 在 config 配置」)。
#: **业务代码一律走 `model_for(功能点)`, 不要直接读 config 常量或写死路径。**
FEATURE_ONNX_ATTR: dict[str, str] = {
    "units": "YOLO_UNITS_ONNX",        # 认奕子/上阵单位名单
    "legend": "YOLO_LEGEND_ONNX",      # 认小小英雄 (捡球排序)
    "ball": "YOLO_BALL_ONNX",          # 认奖励球 (捡球)
    "debug": "YOLO_DEBUG_ONNX",        # 采集/调试图框选 (球+小小英雄)
}

_FEATURE_PRINTED = False


def model_for(feature: str = "units") -> Path:
    """按**功能点**取该功能点该用的 YOLO 权重 (config 可改)。

    feature ∈ FEATURE_ONNX_ATTR 的键; 未知功能点退回 `units` (认奕子) 并告警一次。
    为什么要有这一层: 类别表尾部新增的 ball/小小英雄 在 11s 模型里没有,
    不同功能点必须用不同权重, 否则「检不到」会被误当成「画面里没有」。
    """
    global _FEATURE_PRINTED
    key = str(feature or "units").strip().lower()
    attr = FEATURE_ONNX_ATTR.get(key)
    if attr is None:
        if not _WARNED.get("feature:" + key):
            _WARNED["feature:" + key] = True
            print(f"[yolo] [!] 未知功能点 {feature!r}, 回退 units 模型; "
                  f"可选: {sorted(FEATURE_ONNX_ATTR)}", flush=True)
        key, attr = "units", FEATURE_ONNX_ATTR["units"]
    if not _FEATURE_PRINTED:
        _FEATURE_PRINTED = True
        pairs = " · ".join(f"{k}={Path(getattr(default_config, v)).name}"
                           for k, v in FEATURE_ONNX_ATTR.items())
        print(f"[yolo] 功能点→模型: {pairs}", flush=True)
    p = getattr(default_config, attr, None)
    return Path(p) if p else onnx_path()


def feature_models() -> dict[str, str]:
    """当前「功能点 → 模型文件名」清单 (启动自检/报告用)。"""
    return {k: model_for(k).name for k in FEATURE_ONNX_ATTR}


def predict_for(feature: str, img, **kwargs) -> list[dict]:
    """按功能点预测 (等价 `predict(img, model=model_for(feature), **kwargs)`)。

    注意: `ball` / `legend` 两类**不会出现在返回值里** —— `_to_cells` 会把它们丢弃,
    分别收进 `last_balls()` / `last_legends()` (这是既有口径, 别改)。
    """
    kwargs.pop("model", None)
    return predict(img, model=model_for(feature), **kwargs)


def available() -> bool:
    """权重存在 + onnxruntime 可导入 (模型没训好时静默返回 False, 调用方回落血条法)."""
    if not onnx_path().exists():
        return False
    try:
        import onnxruntime        # noqa: F401
        return True
    except Exception:             # noqa: BLE001
        return False


#: onnxruntime-gpu 需要的 CUDA/cuDNN DLL 所在的 nvidia pip 包子目录 (相对 site-packages/nvidia)。
_CUDA_DLL_SUBDIRS = ("cublas", "cudnn", "cufft", "curand", "cuda_runtime",
                     "cuda_nvrtc", "nvjitlink")

#: `os.add_dll_directory()` 返回的句柄必须**一直持有**, 否则被 GC 回收后目录立刻失效
#: (Python 官方文档明确要求)。实测不持有句柄时 `_cuda_usable()` 返回 True 但
#: `InferenceSession` 仍回落到 CPU —— 2026-10-29 踩到这个坑。
_DLL_DIR_HANDLES: list = []


def _add_cuda_dll_dirs() -> list[str]:
    """把 `site-packages/nvidia/*/bin` 加进 DLL 搜索路径, 返回加进去的目录。

    为什么要这一步 (2026-10-29 实测): `jcc_robot` 环境装的是 pip 版 CUDA 运行库
    (`nvidia-cublas-cu12` / `nvidia-cudnn-cu12` / ...), DLL 在
    `site-packages/nvidia/<pkg>/bin` 里, **不在默认 DLL 搜索路径**。
    只把目录加进 `PATH` 对 Python 3.8+ 无效 (DLL 搜索不再看 PATH), 必须用
    `os.add_dll_directory()`; 加完之后 onnxruntime 的 CUDA provider 才能加载成功。

    **必须在 `import onnxruntime` 之前调用** —— onnxruntime 在**导入时**就探测并
    加载 provider DLL, 之后再补路径已经晚了 (实测: 先 import ort 再改 PATH 无效;
    先改 PATH 再 import ort 有效)。所以 `_providers()` / `session()` 里都是先
    调本函数再 import ort。
    """
    import os
    import site
    out: list[str] = []
    # 注意: conda 环境里 `site.getsitepackages()` 第一个元素是**环境根目录**而不是
    # `Lib\site-packages` (实测 `['...\envs\jcc_robot', '...\envs\jcc_robot\Lib\site-packages']`),
    # 所以必须**遍历所有 root** 再拼 `nvidia` 子目录, 不能只取 [0]。
    try:
        roots = list(site.getsitepackages() or [])
    except Exception:      # noqa: BLE001
        roots = []
    import sysconfig
    purelib = sysconfig.get_paths().get("purelib")
    if purelib and purelib not in roots:
        roots.append(purelib)
    found: list[str] = []
    for root in roots:
        nvidia = Path(root) / "nvidia"
        if not nvidia.is_dir():
            continue
        for pkg in _CUDA_DLL_SUBDIRS:
            d = nvidia / pkg / "bin"
            if d.is_dir() and str(d) not in found:
                found.append(str(d))
    # 双保险: ① 改 os.environ["PATH"] (onnxruntime 内部用 LoadLibrary 时会看);
    #          ② add_dll_directory (Python 3.8+ 的正式机制, 句柄必须持有)。
    if found:
        cur = os.environ.get("PATH", "")
        prefix = ";".join(found)
        if prefix not in cur:
            os.environ["PATH"] = prefix + ";" + cur
        for d in found:
            try:
                _DLL_DIR_HANDLES.append(os.add_dll_directory(d))
                out.append(d)
            except Exception:      # noqa: BLE001
                pass
    return out


def _cuda_usable() -> bool:
    """CUDA provider 是否**真能用**.

    主环境装了 onnxruntime-gpu, 但机器上 (`jcc_robot` 环境里) 既没有 `cublasLt64_12.dll`
    也没有 cudnn → 建会话时会先刷两行 provider 报错再回落 CPU (实测 2026-10-12)。
    这里先探一下运行库: 不可用就**只报 CPU**, 日志干净、也少一次失败的初始化。

    2026-10-29 修正: 原来只 `ctypes.WinDLL("cublasLt64_12.dll")` 直接探, 但 pip 版
    nvidia 包的 DLL 在 `site-packages/nvidia/<pkg>/bin` 里、不在默认搜索路径, 于是
    **明明 GPU 可用也被判成不可用**。现在先 `_add_cuda_dll_dirs()` **再 import ort**
    (onnxruntime 在导入时就探测 provider DLL, 顺序反了就无效), 然后 `WinDLL` 双确认。
    """
    try:
        import ctypes
        _add_cuda_dll_dirs()            # 必须先补 DLL 路径, 再 import onnxruntime
        import onnxruntime as ort
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            return False
        for dll in ("cublasLt64_12.dll", "cudnn64_9.dll"):
            ctypes.WinDLL(dll)          # 找不到会抛 OSError/FileNotFoundError
        return True
    except Exception:      # noqa: BLE001 - 任何异常都当"不可用", 回落 CPU
        return False


def _providers() -> list[str]:
    want = getattr(default_config, "YOLO_UNITS_PROVIDER", None)
    if want:
        return [want]
    return (["CUDAExecutionProvider", "CPUExecutionProvider"]
            if _cuda_usable() else ["CPUExecutionProvider"])


def set_imgsz_override(imgsz: int | None) -> None:
    """自检 / `--fake` 用: 强制输入边长, **不去问 ONNX** (那两个场景本来就没有/不该用模型)."""
    global _IMGSZ_OVERRIDE
    _IMGSZ_OVERRIDE = int(imgsz) if imgsz else None


def session(path=None):
    """惰性建会话 (改 config.YOLO_UNITS_ONNX / PROVIDER 会自动重建).

    `path` 指定权重时按该权重建/取缓存 —— **支持同时挂多个模型**
    (认奕子 11s / 认小小英雄 v2.1 / 认球 v2.1, 用户 m02239 2026-11-01)。
    """
    global _SESSION, _SESSION_KEY
    p = Path(path) if path is not None else onnx_path()
    if not p.exists():
        # 纯图色工程的权重必须真在盘上: 缺了就是路径配错 (config 指到别的工程/临时目录),
        # 这里立刻用中文报清楚"哪个功能点在找哪个文件", 不要等 onnxruntime 抛英文栈。
        feat = next((k for k, attr in FEATURE_ONNX_ATTR.items()
                     if getattr(default_config, attr, None)
                     and Path(str(getattr(default_config, attr))) == p), None)
        raise FileNotFoundError(
            f"[X] YOLO 权重文件不存在: {p} (功能点={feat or '自定义'}) —— "
            f"请检查 config/config.py 里 YOLO_UNITS_ONNX/YOLO_LEGEND_ONNX/"
            f"YOLO_BALL_ONNX/YOLO_DEBUG_ONNX 是否指向本工程 models/ 下的 .onnx"
        )
    key = (str(p), tuple(_providers()))
    if _SESSION is not None and _SESSION_KEY == key:
        return _SESSION
    cached = _SESSIONS.get(key)
    if cached is not None:
        _SESSION, _SESSION_KEY = cached, key
        return cached
    _add_cuda_dll_dirs()                # 必须先补 DLL 路径, 再 import onnxruntime
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = int(getattr(default_config, "OCR_INTRA_OP_THREADS", 2) or 2)
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = ort.InferenceSession(str(p), sess_options=options, providers=list(key[1]))
    _SESSIONS[key] = sess
    _SESSION, _SESSION_KEY = sess, key
    print(f"[yolo] 载入 {p.name} · providers={sess.get_providers()}", flush=True)
    return sess


def model_imgsz(model=None) -> int | None:
    """ONNX 自己的输入边长 (模型说了算).

    **必须用它而不是 config.YOLO_UNITS_IMGSZ**: 导出时 imgsz=640 的模型喂 1280 会直接
    抛 onnxruntime 的输入形状错误。config 里的值只当缺省, 不一致时这里会告警并纠正。
    """
    try:
        shape = session(model).get_inputs()[0].shape       # [1, 3, H, W]
        height = shape[2] if len(shape) > 2 else None
        return int(height) if isinstance(height, int) else None
    except Exception:      # noqa: BLE001
        return None


def _resolve_imgsz(imgsz: int | None, model=None) -> int:
    if _IMGSZ_OVERRIDE:
        return int(_IMGSZ_OVERRIDE)
    want = int(imgsz or default_config.YOLO_UNITS_IMGSZ)
    actual = model_imgsz(model)
    if actual and actual != want:
        if not _WARNED.get("imgsz"):
            _WARNED["imgsz"] = True
            print(f"[yolo] [!] config.YOLO_UNITS_IMGSZ={want} 与模型的 {actual} 不一致 → "
                  f"按模型来 ({actual}); 请把 config 改成 {actual} 保持一致", flush=True)
        return actual
    return want


# --------------------------------------------------------------------------- #
# 预处理 / 后处理 (纯 numpy, 可单测)
# --------------------------------------------------------------------------- #
def letterbox(img, imgsz: int) -> tuple[np.ndarray, float, int, int]:
    """等比缩放 + 灰边填充到正方形 (与 ultralytics 训练/验证时的 letterbox 一致).

    Returns: (float32 RGB 张量 [imgsz, imgsz, 3], scale, pad_x, pad_y)
    优先用 cv2.INTER_LINEAR (与 ultralytics 的 letterbox 同款插值), 没有 cv2 时回落 PIL。
    """
    width, height = img.size
    scale = min(imgsz / width, imgsz / height)
    new_w, new_h = int(round(width * scale)), int(round(height * scale))
    pad_x, pad_y = (imgsz - new_w) // 2, (imgsz - new_h) // 2
    canvas = np.full((imgsz, imgsz, 3), 114, dtype=np.uint8)     # ultralytics 灰边 = 114
    if (new_w, new_h) != (width, height):
        try:
            import cv2
            resized = cv2.resize(np.asarray(img.convert("RGB")), (new_w, new_h),
                                 interpolation=cv2.INTER_LINEAR)
        except Exception:      # noqa: BLE001 - 没有 cv2 也不影响正确性, 只是插值略差
            from PIL import Image as _Image
            resized = np.asarray(img.convert("RGB").resize((new_w, new_h), _Image.BILINEAR))
    else:
        resized = np.asarray(img.convert("RGB"))
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
    return canvas, scale, pad_x, pad_y


def nms(boxes: np.ndarray, scores: np.ndarray, classes: np.ndarray, iou_thres: float,
        max_det: int) -> list[int]:
    """类别内 NMS (xyxy). 返回保留的下标 (按分数降序). numpy 实现, 不依赖 cv2."""
    keep: list[int] = []
    for cls in np.unique(classes):
        idx = np.where(classes == cls)[0]
        idx = idx[np.argsort(-scores[idx])]
        while idx.size:
            best = idx[0]
            keep.append(int(best))
            if idx.size == 1:
                break
            rest = idx[1:]
            xx1 = np.maximum(boxes[best, 0], boxes[rest, 0])
            yy1 = np.maximum(boxes[best, 1], boxes[rest, 1])
            xx2 = np.minimum(boxes[best, 2], boxes[rest, 2])
            yy2 = np.minimum(boxes[best, 3], boxes[rest, 3])
            inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
            area_best = (boxes[best, 2] - boxes[best, 0]) * (boxes[best, 3] - boxes[best, 1])
            area_rest = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
            iou = inter / np.maximum(area_best + area_rest - inter, 1e-9)
            idx = rest[iou <= iou_thres]
    keep.sort(key=lambda i: -scores[i])
    return keep[:max_det]


def decode(raw: np.ndarray, scale: float, pad_x: int, pad_y: int, size: tuple[int, int],
           conf: float, iou_thres: float, max_det: int) -> list[dict]:
    """原始输出 → 全帧像素框列表.

    兼容三种导出形态:
      (1, 4+nc, N)  YOLOv8/v11 默认导出 (xywh, 模型输入像素系)
      (1, N, 4+nc)  个别版本/后端会转置
      (1, N, 6)     已带 NMS 的端到端导出 (x1,y1,x2,y2,score,cls)
    """
    out = np.asarray(raw)
    if out.ndim == 3:
        out = out[0]
    width, height = size

    def _unletterbox(xyxy: np.ndarray) -> np.ndarray:
        xyxy = xyxy.copy()
        xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - pad_x) / scale
        xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - pad_y) / scale
        xyxy[:, [0, 2]] = xyxy[:, [0, 2]].clip(0, width)
        xyxy[:, [1, 3]] = xyxy[:, [1, 3]].clip(0, height)
        return xyxy

    if out.ndim == 2 and out.shape[1] == 6:          # 端到端: 已 NMS
        xyxy = _unletterbox(out[:, :4])
        return [{"box": tuple(float(v) for v in xyxy[i]), "score": float(out[i, 4]),
                 "class_id": int(out[i, 5])}
                for i in range(out.shape[0]) if float(out[i, 4]) >= conf]

    if out.ndim != 2:
        return []
    # 判定"哪一维是类别": 优先用冻结表的类数当指纹 (最稳), 两边都对不上才退回尺寸启发式.
    # 实测坑: 候选数 N 可能小于 4+nc (自检打桩/NMS 端到端导出), 只看大小会把轴认反。
    expected = {class_table.num_classes(), class_table.num_classes() + 1}
    as_rows, as_cols = out.shape[1] - 4, out.shape[0] - 4      # (N,4+nc) / (4+nc,N) 两种解读
    if as_cols in expected and as_rows not in expected:
        out = out.T                                            # (4+nc, N) → (N, 4+nc)
    elif as_rows not in expected and as_cols not in expected and out.shape[0] < out.shape[1]:
        out = out.T
    if out.shape[1] < 6:                             # 只有框没有类别 → 当"整类"处理没意义
        return []
    n_cls = out.shape[1] - 4
    if n_cls not in expected and not _WARNED.get("nc"):
        _WARNED["nc"] = True
        # **2026-10-22 更正**: classes.json 的约定是"新类别只往**表尾追加**"(见 _comment),
        #   所以 n_cls < 冻结表类数时**类别号并不错位** —— 模型只是不认识表尾新增的那几类
        #   (当前 yolo_v2.1_balls.onnx 70 类 vs 表 73 类: 缺 其他小小英雄/石皮树/生命花)。
        #   只有 n_cls > 表类数 或表被重排过才是真错位。这里按实际情况分两句话打, 免误导。
        if n_cls < class_table.num_classes():
            print(f"[yolo] [!] ONNX 有 {n_cls} 类 < 冻结表 {class_table.num_classes()} 类 → "
                  f"类别号**不错位** (表只往尾部追加), 模型只是不认识表尾新增的 "
                  f"{class_table.num_classes() - n_cls} 类, 这些类会走点击确认", flush=True)
        else:
            print(f"[yolo] [!] ONNX 有 {n_cls} 类 > 冻结表 {class_table.num_classes()} 类 → "
                  f"模型与类别表不匹配, 类别名会错位 (重新训练或用对应版本的 classes.json)", flush=True)
    boxes_xywh, class_scores = out[:, :4], out[:, 4:]
    scores = class_scores.max(axis=1)
    classes = class_scores.argmax(axis=1)
    mask = scores >= conf
    if not mask.any():
        return []
    boxes_xywh, scores, classes = boxes_xywh[mask], scores[mask], classes[mask]
    xyxy = np.empty_like(boxes_xywh)
    xyxy[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2
    xyxy[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2
    xyxy[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2
    xyxy[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2
    keep = nms(xyxy, scores, classes, iou_thres, max_det)
    xyxy = _unletterbox(xyxy)
    return [{"box": tuple(float(v) for v in xyxy[i]), "score": float(scores[i]),
             "class_id": int(classes[i])} for i in keep]


# --------------------------------------------------------------------------- #
# 推理
# --------------------------------------------------------------------------- #
def _infer(tensor: np.ndarray, model=None) -> np.ndarray:
    """跑一次会话 (单独成函数便于 --selftest 打桩)."""
    sess = session(model)
    return sess.run(None, {sess.get_inputs()[0].name: tensor})[0]


def predict(img, *, conf: float | None = None, iou: float | None = None,
            imgsz: int | None = None, max_det: int | None = None,
            model=None) -> list[dict]:
    """全帧预测: 返回 [{name, score, box, class_id, area}, ...] (只含认得出类别的框).

    `model`: 权重路径; None=认奕子的默认模型 (config.YOLO_UNITS_ONNX)。
             认球 / 认小小英雄传 `ball_onnx_path()` / `legend_onnx_path()` (用户 m02239)。
    """
    t0 = time.perf_counter()
    imgsz = _resolve_imgsz(imgsz, model)
    if conf is None:
        # conf 跟着信任表走（每张表有自己标定用的 conf）；表不可用时退回全局配置。
        # 用户 m06561/m06562: 一刀切不是好办法，应按实测免点率/精度算出该表的 conf。
        try:
            from recognize import yolo_trust as _trust
            conf = _trust.table_conf()
        except Exception:          # 表坏了/导入异常都不该影响采集，直接退回配置值
            conf = float(default_config.YOLO_UNITS_CONF)
    conf = float(conf)
    iou = float(default_config.YOLO_UNITS_IOU if iou is None else iou)
    max_det = int(max_det or default_config.YOLO_UNITS_MAX_DET)
    raw, scale, pad_x, pad_y, pre_ms, infer_ms = _forward(img, imgsz, model)
    t2 = time.perf_counter()
    dets = decode(raw, scale, pad_x, pad_y, (img.size[0], img.size[1]), conf, iou, max_det)
    dets = _to_cells(dets)
    _LAST_TIMING.update({"preprocess_ms": pre_ms, "infer_ms": infer_ms,
                         "postprocess_ms": (time.perf_counter() - t2) * 1000,
                         "total_ms": (time.perf_counter() - t0) * 1000, "imgsz": imgsz})
    if getattr(default_config, "YOLO_UNITS_TIMING", False):
        print(f"[yolo] 预处理 {_LAST_TIMING['preprocess_ms']:.1f}ms · 推理 "
              f"{_LAST_TIMING['infer_ms']:.1f}ms · 后处理 {_LAST_TIMING['postprocess_ms']:.1f}ms "
              f"· 共 {_LAST_TIMING['total_ms']:.1f}ms (imgsz={imgsz}, {len(dets)} 个)", flush=True)
    return dets


# 同帧前向缓存: 低阈值复核要对"同一帧"再解一次检测头输出, 重新前向就白花 1s (用户 2026-10-20)。
_FORWARD_CACHE: dict = {"key": None, "payload": None}


def _forward(img, imgsz: int, model=None) -> tuple[np.ndarray, float, int, int, float, float]:
    """单帧前向 (带同帧缓存): 返回 `(raw, scale, pad_x, pad_y, 预处理ms, 推理ms)`.

    命中缓存时预处理/推理耗时都记 0 (确实没跑), 调用方的"总耗时"不会虚高。
    缓存只留最近一帧: 逐帧扫描时每次都是新对象, 不会累积内存。
    **缓存 key 必须含模型路径**: 同一帧要用不同模型各跑一次时 (认球/认奕子), 不然会串味。
    """
    mkey = str(Path(model)) if model is not None else str(onnx_path())
    key = (id(img), int(imgsz), mkey)
    payload = _FORWARD_CACHE.get("payload")
    if payload is not None and _FORWARD_CACHE.get("key") == key:
        return payload + (0.0, 0.0)
    t0 = time.perf_counter()
    tensor, scale, pad_x, pad_y = letterbox(img, imgsz)
    chw = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
    t1 = time.perf_counter()
    raw = _infer(chw, model)
    t2 = time.perf_counter()
    _FORWARD_CACHE.update({"key": key, "payload": (raw, scale, pad_x, pad_y)})
    return raw, scale, pad_x, pad_y, (t1 - t0) * 1000.0, (t2 - t1) * 1000.0


def clear_forward_cache() -> None:
    """清空同帧前向缓存 (测试/换模型时用)."""
    _FORWARD_CACHE.update({"key": None, "payload": None})


def lowconf_candidates(img, centers, *, conf: float | None = None,
                       radius: float | None = None,
                       exclude: Iterable[str] | None = None,
                       min_score: float | None = None) -> list[dict | None]:
    """**低阈值复核**: 对每个格位中心找最近的候选类别框 (用户 2026-10-20 要求)。

    调用口径 (顺序不能反): 血条/YOLO **已经定位到**这一格确实有奕子 → 回溯法补名 →
    回溯也补不上 → 才来这里用低阈值再解一次同帧检测输出, 判断"这个位置到底是什么类"。

    * `conf` 缺省 `YOLO_UNITS_CONF_LOW` (0.10), 远低于主阈值 (检出 0.35 / 命名 0.45);
    * `radius` 缺省 `YOLO_UNITS_LOWCONF_RADIUS` (70px): 候选框中心离格心太远就不要 ——
      **宁可 unknown, 不瞎认** (AGENTS §1);
    * `exclude` 缺省 `YOLO_UNITS_LOWCONF_EXCLUDE` (unknown/ball/小小英雄): 这几类给不出真实名字。
    返回与 `centers` **等长**的列表, 每项 `{name, score, box, dist}` 或 `None`; 复用 `_forward`
    的同帧缓存, 只多一次 decode (几 ms), 不额外推理。
    """
    if not centers:
        return []
    if not getattr(default_config, "YOLO_UNITS_LOWCONF", True):
        return [None] * len(centers)
    conf = float(getattr(default_config, "YOLO_UNITS_CONF_LOW", 0.10) if conf is None else conf)
    min_score = float(getattr(default_config, "YOLO_UNITS_LOWCONF_MIN_SCORE", 0.20)
                      if min_score is None else min_score)
    radius = float(getattr(default_config, "YOLO_UNITS_LOWCONF_RADIUS", 70.0)
                   if radius is None else radius)
    exclude_set = set(exclude if exclude is not None
                      else getattr(default_config, "YOLO_UNITS_LOWCONF_EXCLUDE", ()))
    imgsz = _resolve_imgsz(None)
    raw, scale, pad_x, pad_y, _pre, _inf = _forward(img, imgsz)
    dets = decode(raw, scale, pad_x, pad_y, (img.size[0], img.size[1]), conf,
                  float(getattr(default_config, "YOLO_UNITS_IOU", 0.5)), 300)
    out: list[dict | None] = []
    for center in centers:
        try:
            cx, cy = float(center[0]), float(center[1])
        except (TypeError, ValueError, IndexError):
            out.append(None)
            continue
        best: dict | None = None
        for det in dets:
            name = class_table.id_to_name(det["class_id"])
            if not name or name in exclude_set:
                continue
            # 命名下限 (用户 2026-10-21 实机反馈): 低于它的一律**不当候选** —— 那是噪声, 不是猜名依据。
            # 实测反例: r1c1 的 峡谷迅捷蟹 被 YOLO 给了个 conf=0.108 的 `乐芙兰` 框, 结果一个 0.11 的
            # 胡思乱想被当成真名 (比 unknown 更糟: unknown 不会误导卖牌/上阵)。
            if float(det["score"]) < min_score:
                continue
            x1, y1, x2, y2 = det["box"]
            dx, dy = (x1 + x2) / 2 - cx, (y1 + y2) / 2 - cy
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > radius:
                continue
            if best is None or float(det["score"]) > float(best["score"]):
                best = {"name": name, "score": round(float(det["score"]), 3),
                        "box": [int(round(v)) for v in (x1, y1, x2, y2)],
                        "dist": round(dist, 1)}
        out.append(best)
    return out


def lowconf_class_topk(img, centers, *, conf: float | None = None,
                       radius: float | None = None,
                       exclude: Iterable[str] | None = None,
                       topk: int = 3,
                       max_anchors: int = 40) -> list[list[dict]]:
    """**多类别候选**: 每个格位中心附近, 各类别能达到的最高分 (NMS 之前, 用户 2026-10-21 方案2).

    和 `lowconf_candidates` 的区别:
    * 那个返回"框"的 top1 (已 NMS, 一个位置只剩最高类);
    * 这个返回"类别分数"的 top-k —— 同一个 anchor 位置上不同类的分数都留着,
      用来判断"模型到底有多确定" (top1 比 top2 高出很多 = 确定; 两个类咬得很紧 = 别猜)。

    返回与 `centers` 等长的列表, 每项是 `[{name, score}, ...]` (按分数降序, 最多 topk 个);
    空列表 = 该位置附近没有可用的类别候选 (纯噪声/扣不出前景)。
    """
    if not centers:
        return []
    if not getattr(default_config, "YOLO_UNITS_LOWCONF", True):
        return [[] for _ in centers]
    conf = float(getattr(default_config, "YOLO_UNITS_CONF_LOW", 0.10) if conf is None else conf)
    radius = float(getattr(default_config, "YOLO_UNITS_LOWCONF_RADIUS", 70.0)
                   if radius is None else radius)
    exclude_set = set(exclude if exclude is not None
                      else getattr(default_config, "YOLO_UNITS_LOWCONF_EXCLUDE", ()))
    imgsz = _resolve_imgsz(None)
    raw, scale, pad_x, pad_y, _pre, _inf = _forward(img, imgsz)

    out = np.asarray(raw)
    if out.ndim == 3:
        out = out[0]
    if out.ndim != 2 or out.shape[1] < 6:
        return [[] for _ in centers]
    expected = {class_table.num_classes(), class_table.num_classes() + 1}
    as_rows, as_cols = out.shape[1] - 4, out.shape[0] - 4
    if as_cols in expected and as_rows not in expected:
        out = out.T
    elif as_rows not in expected and as_cols not in expected and out.shape[0] < out.shape[1]:
        out = out.T
    if out.shape[1] < 6:
        return [[] for _ in centers]
    boxes = out[:, :4]
    class_scores = out[:, 4:]
    # 中心点: 模型输出是 letterbox 后的像素系, 换回原图坐标才和 center 可比
    cx = (boxes[:, 0] - pad_x) / float(scale)
    cy = (boxes[:, 1] - pad_y) / float(scale)
    row_max = class_scores.max(axis=1)
    r2 = radius * radius
    results: list[list[dict]] = []
    for center in centers:
        try:
            tx, ty = float(center[0]), float(center[1])
        except (TypeError, ValueError, IndexError):
            results.append([])
            continue
        near = np.where((cx - tx) ** 2 + (cy - ty) ** 2 <= r2)[0]
        if near.size == 0:
            results.append([])
            continue
        # 只处理附近分数最高的若干 anchor, 避免整帧 anchor 全扫 (70 类 × N 很慢)
        near = near[np.argsort(-row_max[near])][:max_anchors]
        best: dict[str, float] = {}
        for idx in near:
            scores = class_scores[idx]
            if float(row_max[idx]) < conf:
                continue
            for cid in np.argsort(-scores)[: max(1, topk + 2)]:
                s = float(scores[cid])
                if s < conf:
                    continue
                nm = class_table.id_to_name(int(cid))
                if not nm or nm in exclude_set:
                    continue
                if s > best.get(nm, 0.0):
                    best[nm] = s
        ranked = sorted(best.items(), key=lambda kv: -kv[1])[:topk]
        results.append([{"name": n, "score": round(float(s), 3)} for n, s in ranked])
    return results


def last_timing() -> dict:
    return dict(_LAST_TIMING)


def _to_cells(dets: list[dict]) -> list[dict]:
    """框 → 格位: 先按棋盘落脚点映射, 落不上再按备战席判定点映射 (与血条法同口径).

    **映射不上的框直接丢弃** (宁可漏, 不可错): 实测敌方半场的奕子/UI 误检会落在 row<5,
    它们既不是"我方棋盘格"也不是"备战席格"; 混进来会污染下游的阵容/人口统计。
    丢弃数量记在 `last_stats()` 里, 便于观测模型有没有跑偏。
    """
    min_score = float(default_config.YOLO_UNITS_MIN_SCORE)
    board_anchor_dy = int(default_config.BOARD_BAR_TO_CELL_Y)
    stats = {"dets": len(dets), "board": 0, "bench": 0, "dropped_enemy": 0,
             "dropped_unmapped": 0, "dropped_ball": 0, "dropped_legend": 0,
             "unnamed": 0, "ambiguous": 0}
    out = []
    balls: list[dict] = []
    legends: list[dict] = []
    for det in dets:
        x1, y1, x2, y2 = det["box"]
        cx = (x1 + x2) / 2
        name = class_table.id_to_name(det["class_id"])
        if name == "ball":
            # 球不做格位映射、不进奕子名单 (捡球走 reward_ball 颜色法);
            # v2.1 起 ball 进训练集, 模型会输出球框, 必须在此显式丢弃。
            # 丢弃前留一份框坐标: 供 `--debug_yolo_and_blood_bar` 在调试图上把球也框出来
            # (用户 2026-10-17), 只给画图看, 绝不进奕子名单。
            balls.append({"name": "ball", "model_name": "ball",
                          "score": round(det["score"], 3), "class_id": det["class_id"],
                          "box": [int(round(v)) for v in (x1, y1, x2, y2)]})
            stats["dropped_ball"] += 1
            continue
        if name == LEGEND_NAME:
            # 小小英雄 (非奕子附加类): 与血条法同口径**不进奕子名单** (board.extract_units 默认
            # include_legend=False 已过滤)。丢弃前同样留框坐标, 供调试图把人框也画出来 (用户 2026-10-18)。
            legends.append({"name": LEGEND_NAME, "model_name": LEGEND_NAME,
                            "score": round(det["score"], 3), "class_id": det["class_id"],
                            "box": [int(round(v)) for v in (x1, y1, x2, y2)]})
            stats["dropped_legend"] += 1
            continue
        item = {"name": name if det["score"] >= min_score else None,
                "model_name": name,
                "score": round(det["score"], 3),
                "class_id": det["class_id"],
                "box": [int(round(v)) for v in (x1, y1, x2, y2)],
                "rect": [int(round(v)) for v in (x1, y1, x2, y2)],
                "center": [int(round(cx)), int(round((y1 + y2) / 2))],
                "source": "yolo", "identity_source": "yolo", "kind": "unit",
                "stars": None, "verified": False, "legend": False,
                "ambiguous": False, "ambiguous_peers": []}
        if item["name"] is None:
            stats["unnamed"] += 1
        # 棋盘: 落脚点 = 框顶 + BOARD_BAR_TO_CELL_Y (血条法同一常量)
        anchor_y = int(round(y1 + board_anchor_dy))
        loc = board_position([cx, anchor_y])
        if loc is not None and loc.get("row", 0) >= 5:           # 只认己方 1~4 行
            item.update({"cell_anchor": [int(round(cx)), anchor_y], "location": loc,
                         "area": "board", "status": "located"})
            stats["board"] += 1
            out.append(item)
            continue
        if loc is not None and loc.get("row", 0) < 5:
            stats["dropped_enemy"] += 1                          # 敌方半场 (未训练/UI 误检)
            continue
        # 备战席: 判定点 = 框顶 + BENCH_CENTER_DY (血条法同一常量)
        bench_y = int(round(y1 + BENCH_CENTER_DY))
        bench = bench_position([cx, bench_y])
        if bench is None:
            stats["dropped_unmapped"] += 1
            continue
        item.update({"cell_anchor": [int(round(cx)), bench_y], "location": bench,
                     "area": "bench", "status": "located"})
        stats["bench"] += 1
        out.append(item)
    # 同格多框 = 模型在纠结 -> 打 ambiguous 标志 (用户 m06003 §3.3 确认"就点击验证"没问题).
    # 实测依据 (3-1 帧的 r2c4): 模型同时输出 `约里克 0.498`(错) 与 `洛 0.375`(对), 两者 location 完全相同
    # -> **高分错低分对**, 单靠 score 阈值分不开对错; 而"同格出现多个框"本身是免费的确定性信号。
    groups: dict[tuple, list[dict]] = {}
    for item in out:
        loc = item["location"]
        key = ((item["area"], loc.get("own_row"), loc.get("column"))
               if item["area"] == "board" else (item["area"], loc.get("slot")))
        groups.setdefault(key, []).append(item)
    for members in groups.values():
        if len(members) < 2:
            continue
        stats["ambiguous"] += len(members)
        for item in members:
            item["ambiguous"] = True
            item["ambiguous_peers"] = [{"model_name": m["model_name"], "score": m["score"]}
                                       for m in members if m is not item]
    _LAST_STATS.clear()
    _LAST_STATS.update(stats)
    _LAST_BALLS.clear()
    _LAST_BALLS.extend(balls)
    _LAST_LEGENDS.clear()
    _LAST_LEGENDS.extend(legends)
    return out


def last_stats() -> dict:
    """上次 extract_units 的框数分布 (board/bench/丢弃), 供日志与对拍脚本使用."""
    return dict(_LAST_STATS)


def last_balls() -> list[dict]:
    """上次 extract_units 被丢弃的 ball 框 (只给调试图框选; 不进奕子名单/不参与定位)."""
    return [dict(b) for b in _LAST_BALLS]


def last_legends() -> list[dict]:
    """上次 extract_units 被丢弃的小小英雄框 (只给调试图框选; 不进奕子名单/不参与定位)."""
    return [dict(b) for b in _LAST_LEGENDS]


# --------------------------------------------------------------------------- #
# 与 recognize.board.extract_units 同 shape 的入口
# --------------------------------------------------------------------------- #
def extract_units(img, *, conf: float | None = None, iou: float | None = None,
                  imgsz: int | None = None, include_legend: bool = False, **_kw) -> dict:
    """drop-in 替代 `recognize.board.extract_units` (签名兼容多余的 kwargs).

    只采我方: 棋盘 leave 对方半场 (row<5) 的框直接丢弃 (与 roster 的过滤口径一致)。
    """
    dets = predict(img, conf=conf, iou=iou, imgsz=imgsz)
    result: dict = {"board": [], "bench": []}
    for item in dets:
        area = item.get("area") or "bench"
        result["board" if area == "board" else "bench"].append(item)
    result["board"].sort(key=lambda u: (u["center"][1], u["center"][0]))
    result["bench"].sort(key=lambda u: u["center"][0])
    return result


# --------------------------------------------------------------------------- #
# 自检: 不需要模型, 用打桩的原始输出验证 letterbox / NMS / 类别映射 / 格位映射
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    from PIL import Image

    imgsz = int(default_config.YOLO_UNITS_IMGSZ)
    size = (1920, 1080)
    scale = min(imgsz / size[0], imgsz / size[1])
    new_w, new_h = int(round(size[0] * scale)), int(round(size[1] * scale))
    pad_x, pad_y = (imgsz - new_w) // 2, (imgsz - new_h) // 2

    def to_letterbox(box):
        x1, y1, x2, y2 = box
        return [x1 * scale + pad_x, y1 * scale + pad_y, x2 * scale + pad_x, y2 * scale + pad_y]

    # 真值框取自采集数据集 manifest (frame_222932_924)
    cases = [((970, 412, 1052, 545), "蔚", "board"),        # r2c4
             ((628, 565, 710, 702), "奥恩", "board"),        # r4c1
             ((804, 696, 886, 810), "约里克", "bench"),      # s4
             ((578, 688, 660, 810), "蔚", "bench"),          # s2
             ((676, 185, 758, 318), "凯特琳", "enemy")]      # 敌方半场 → 必须被丢掉
    nc = class_table.num_classes()
    rows = []
    for (box, name, _area) in cases:
        x1, y1, x2, y2 = to_letterbox(box)
        row = [0.0] * (4 + nc)
        row[0], row[1] = (x1 + x2) / 2, (y1 + y2) / 2
        row[2], row[3] = x2 - x1, y2 - y1
        row[4 + class_table.name_to_id(name)] = 0.9
        rows.append(row)
    # 再来一个重复框 (测 NMS) 与一个低置信框 (测 MIN_SCORE 不猜类别)
    dup = list(rows[0])
    dup[0] += 2
    rows.append(dup)
    low_box = to_letterbox((439, 664, 521, 810))              # 备战席 s1, 凯尔
    low = [0.0] * (4 + nc)
    low[0], low[1] = (low_box[0] + low_box[2]) / 2, (low_box[1] + low_box[3]) / 2
    low[2], low[3] = low_box[2] - low_box[0], low_box[3] - low_box[1]
    low[4 + class_table.name_to_id("凯尔")] = max(0.1, float(default_config.YOLO_UNITS_MIN_SCORE) - 0.1)
    rows.append(low)

    raw = np.asarray(rows, dtype=np.float32).T[None]          # (1, 4+nc, N)
    # 真实导出 N=8400 远大于 4+nc; 这里补零通道把 N 撑到 100, 顺便测"全零候选不产生框".
    # (注意别补成方阵: 方阵时"哪一维是类别"无法判定)
    if raw.shape[2] < 100:
        pad = np.zeros((1, raw.shape[1], 100 - raw.shape[2]), dtype=np.float32)
        raw = np.concatenate([raw, pad], axis=2)
    global _infer
    original = _infer
    _infer = lambda _tensor: raw                              # 打桩: 不加载模型
    set_imgsz_override(imgsz)                # 自检不查 ONNX: 仓库里有没有模型都要能过
    try:
        result = extract_units(Image.new("RGB", size, (20, 20, 20)))
    finally:
        _infer = original
        set_imgsz_override(None)

    problems: list[str] = []
    got = {(u["name"], u["area"]): u for u in result["board"] + result["bench"]}
    print(f"打桩输出 {len(rows)} 个候选 → 解析出 board {len(result['board'])} / "
          f"bench {len(result['bench'])}")
    for u in result["board"] + result["bench"]:
        loc = u.get("location") or {}
        where = (f"own_row{loc.get('own_row')}-col{loc.get('column')}" if u["area"] == "board"
                 else f"slot{loc.get('slot')}")
        print(f"   {u['area']:<5} {u['box']} → {where:<20} name={u['name']} score={u['score']}")
    for (box, name, area) in cases:
        if area == "enemy":
            if any(abs(u["box"][0] - box[0]) < 3 and abs(u["box"][1] - box[1]) < 3
                   for u in result["board"] + result["bench"]):
                problems.append(f"敌方半场框没被丢弃: {name} {box}")
            continue
        key = (name, area)
        if key not in got:
            problems.append(f"漏检/映射失败: {name} {area} {box}")
            continue
        if got[key]["location"] is None:
            problems.append(f"格位映射为空: {name} {area}")
    if len(result["board"]) != 2:
        problems.append(f"棋盘应 2 个, 实得 {len(result['board'])} (NMS/敌方过滤有问题?)")
    if len(result["bench"]) != 3:      # 2 个备战席 + 1 个低置信 (仍保留框, 但 name=None)
        problems.append(f"备战席应 3 个 (含低置信), 实得 {len(result['bench'])}")
    stats = last_stats()
    if stats.get("dropped_enemy") != 1:
        problems.append(f"敌方半场框应丢 1 个, 实得 {stats.get('dropped_enemy')} "
                        f"(stats={stats})")
    if stats.get("board") != 2 or stats.get("bench") != 3:
        problems.append(f"框数统计不对: {stats}")
    low_name = [u["name"] for u in result["bench"] if u["score"] < float(default_config.YOLO_UNITS_MIN_SCORE)]
    if low_name != [None]:
        problems.append(f"低置信框应保留但 name=None, 实得 {low_name}")
    if not _LAST_TIMING.get("total_ms"):
        problems.append("没有记录分步耗时")
    if problems:
        print("[X] 自检失败:")
        for p in problems:
            print("   -", p)
        return 1
    print("[OK] 自检通过: letterbox 往返 / 同类 NMS / 冻结表类别映射 / "
          "棋盘(own_row)与备战席(slot)格位映射 / 敌方半场过滤 / 低置信不猜类别")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="YOLO 单次前向识别我方奕子 (onnxruntime)")
    ap.add_argument("--selftest", action="store_true", help="用打桩输出跑自检 (不需要模型)")
    ap.add_argument("--onnx", help="临时指定权重路径")
    ap.add_argument("--img", help="对一张图跑一次, 打印结果 (需要真的 ONNX)")
    ap.add_argument("--imgsz", type=int)
    args = ap.parse_args(argv)
    if args.onnx:
        default_config.YOLO_UNITS_ONNX = Path(args.onnx)
    if args.imgsz:
        default_config.YOLO_UNITS_IMGSZ = args.imgsz
    if args.selftest:
        return _selftest()
    if args.img:
        from PIL import Image
        t0 = time.perf_counter()
        units = extract_units(Image.open(args.img).convert("RGB"))
        dt = (time.perf_counter() - t0) * 1000
        print(f"{args.img}: board {len(units['board'])} / bench {len(units['bench'])} · {dt:.0f}ms")
        for u in units["board"] + units["bench"]:
            print(f"   {u['name']} {u['score']} {u['box']} {u['location']}")
        return 0
    print(__doc__)
    print(f"权重: {onnx_path()} (存在={onnx_path().exists()}, available={available()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
