"""RapidOCR 引擎封装: 懒加载单例, 支持整图与区域裁剪识别 (纯本地, 无远程服务).

条目结构: {"text": str, "score": float, "box": (x1, y1, x2, y2)}, 按 (y, x) 排序.

口径 (2026-11-14 纯图色工程改造)
--------------------------------
新工程 `jcc_robot_by_img` **不依赖任何内存/常驻服务**, OCR 只走进程内 `rapidocr_onnxruntime`:
- 只有本地一路: 没有常驻服务要探, 也绝不因为"服务没起"打警告或重试等待;
- `JCC_OCR_LOCAL_ONLY` 仍被识别, 但语义退化为"恒本地"(本工程只有本地一路), 保留它是为了
  兼容历史启动脚本里的环境变量, 行为与不设置一致;
- 本地引擎初始化失败只留痕一次 (`_engine_init_error`) 并抛 `OcrEngineUnavailable`,
  调用方按"识别不到"处理 —— 不降级、不重试、不换通道。
"""

import hashlib
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image
from config.config import default_config

_engine = None
_engine_gpu = False             # 当前引擎是否真跑在 GPU 上 (get_engine 建引擎时写; 见 engine_gpu)
_cache: dict[str, list] = {}    # OCR 结果缓存 {md5(图像字节+识别参数): items}
_CACHE_MAX = 64                 # 缓存条目上限 (FIFO 淘汰), 防长跑内存膨胀
_CACHE_LOCK = threading.Lock()  # 缓存互斥: 静态提取并行化后多线程共用本模块
# 引擎构建互斥 (2026-11-07 实测): 多线程同时首次 OCR 时无锁的 `get_engine()` 会各建一个引擎
# (实测并发 4 线程 = 4 个 RapidOCR), 显存/内存白吃好几份, 还可能是 GPU OOM 的伏笔。
_ENGINE_LOCK = threading.Lock()
# 本地引擎初始化失败的原因 (str, 一次留痕): 失败后缓存原因并直接抛 OcrEngineUnavailable,
# 不再每次重试白等 (实测快速导入失败会连带把业务链路整段打死)。
_engine_init_error: str | None = None

# 历史环境变量 `JCC_OCR_LOCAL_ONLY` 的语义落点: 本工程只有本地一路 OCR, 所以恒为 True。
# 保留这个名字是给旧启动脚本/旧日志看的 —— 设不设它行为完全一致 (不会去连任何服务)。
LOCAL_ONLY = True


class OcrEngineUnavailable(RuntimeError):
    """本地引擎不可用 (导入/初始化失败)。调用方应按"识别不到"处理。"""


def _cache_key(img, region=None, upscale: float = 1.0) -> str | None:
    """缓存键: 图像字节与影响识别结果的参数 (区域/放大倍数) 一起做 md5.

    同一张图不同 region/upscale 的识别结果不同 (放大重读专为整图漏检的小字),
    参数必须折进键里各自独立缓存, 否则不同参数的调用会互相短路拿到残缺结果.

    USE_OCR_CACHE 关闭时返回 None, 跳过整图 md5 的计算开销 (键为 None 永不命中).
    """
    if not default_config.USE_OCR_CACHE:
        return None
    h = hashlib.md5(np.ascontiguousarray(_to_array(img)))
    h.update(repr((None if region is None else tuple(region),
                   round(float(upscale), 3))).encode("utf-8"))
    return h.hexdigest()


def _cache_put(key: str | None, items: list) -> None:
    """写入缓存, 超上限时按 FIFO 淘汰最早的条目 (换帧后旧帧自然过期); key=None 跳过."""
    if key is None:
        return
    with _CACHE_LOCK:
        _cache[key] = items
        while len(_cache) > _CACHE_MAX:
            _cache.pop(next(iter(_cache)))


def get_engine():
    """获取全局唯一的 RapidOCR 实例 (首次调用时初始化, 约 0.5s; 设备由 USE_CPU_OCR 决定).

    USE_CPU_OCR 三态: True=CPU; False=GPU(不可用回落 CPU 并警告); None=自动(优先 GPU)。
    **本工程只有本地引擎**, 所以自动档就是"有 GPU 用 GPU", 不再像带常驻服务的版本那样
    把本地当成兜底而强制 CPU (那是为了不让兜底引擎抢占用于服务的显存; 现在没有服务,
    强制 CPU 只会让每帧 OCR 慢 3~4s)。`JCC_OCR_LOCAL_GPU=1` 无需再设。
    可用物理内存 < `OCR_MIN_FREE_GB` 时仍**一律**不许建 GPU 引擎 (CPU 可用, 只是慢)。
    """
    global _engine, _engine_gpu, _engine_init_error
    if _engine is not None:
        return _engine
    if _engine_init_error is not None:
        raise OcrEngineUnavailable(_engine_init_error)
    with _ENGINE_LOCK:            # 多线程同时首次 OCR 时只建一个引擎 (见 _ENGINE_LOCK 注释)
        if _engine is not None:
            return _engine
        if _engine_init_error is not None:
            raise OcrEngineUnavailable(_engine_init_error)
        try:
            from rapidocr_onnxruntime import RapidOCR

            from utils import mem_guard

            mode = default_config.USE_CPU_OCR
            gpu_ok = _cuda_available()
            use_gpu = (not mode) if mode is not None else gpu_ok
            if mode is False and not gpu_ok:
                print("[ocr] WARNING: USE_CPU_OCR=False 但 GPU 不可用 (需 onnnxruntime-gpu "
                      "且 NVIDIA 驱动/运行库齐全), 回落 CPU", flush=True)
            # 整机内存闸门: 可用内存过低时不许建 GPU 引擎
            if use_gpu:
                _low, _why = mem_guard.too_low(default_config.OCR_MIN_FREE_GB)
                if _low:
                    use_gpu = False
                    print(f"[ocr] {_why} -> 本地引擎改用 CPU "
                          f"({mem_guard.line('OCR 建引擎前')})", flush=True)
            if use_gpu and gpu_ok:
                _engine = RapidOCR(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
                _engine_gpu = True
                print("[ocr] 本地引擎: GPU (CUDAExecutionProvider)", flush=True)
            else:
                _engine = RapidOCR(intra_op_num_threads=default_config.OCR_INTRA_OP_THREADS,
                                   inter_op_num_threads=default_config.OCR_INTER_OP_THREADS)
                _engine_gpu = False
                print(f"[ocr] 本地引擎: CPU ({mem_guard.line('OCR 建引擎')})", flush=True)
        except Exception as exc:      # noqa: BLE001 - 任何初始化异常都转成明确不可用
            import traceback
            _engine_init_error = f"{type(exc).__name__}: {exc}"
            _tb = [ln.strip() for ln in traceback.format_exc().strip().splitlines()
                   if ln.strip().startswith('File "')]
            print(f"*** [ocr] 本地引擎初始化失败: {_engine_init_error} "
                  f"| 栈尾: {' <- '.join(_tb[-3:]) or '-'}", flush=True)
            raise OcrEngineUnavailable(_engine_init_error) from exc
    return _engine


def engine_gpu() -> bool:
    """当前 OCR 引擎是否**真跑在 GPU** 上 (界面/健康检查展示用)。

    引擎还没建 (懒加载, 首次 OCR 前) -> 恒 False —— 此刻显存里确实没有它, 报 False 才不骗人;
    建好后按 `RapidOCR(det_use_cuda=True, ...)` 实际走的那条分支记 (见 `get_engine()`)。
    """
    return _engine is not None and bool(_engine_gpu)


_CUDA_DLLS = ("cudart64_12.dll", "cublasLt64_12.dll", "cublas64_12.dll",
              "cufft64_11.dll", "cudnn64_9.dll")  # ORT CUDA EP 关键依赖, 缺一自动回落 CPU
_cuda_checked: bool | None = None  # 预加载+provider 检测结果缓存 (一次性)


def _cuda_available() -> bool:
    """GPU OCR 可用性: 预加载 CUDA 运行库后 onnxruntime 是否真正带 CUDA provider.

    Windows 上 os.add_dll_directory 对 ORT 依赖 DLL 的解析不生效, 必须用 ctypes
    预加载 nvidia pip 包内的 cublas/cudnn; cudnn 主库依赖同目录子 DLL, 先加载子库
    再加载主库 (两轮), 否则 ORT 会静默回落 CPU 甚至崩溃.
    """
    global _cuda_checked
    if _cuda_checked is None:
        _cuda_checked = False
        try:
            import ctypes
            import sysconfig
            base = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
            dirs = [d for d in base.glob("*/bin")] if base.is_dir() else []
            for d in dirs:
                os.add_dll_directory(str(d))
            dlls = [p for d in dirs for p in sorted(d.glob("*.dll"))]
            for _ in range(2):  # 首轮子库就绪前主库加载失败属预期
                for p in dlls:
                    try:
                        ctypes.CDLL(str(p))
                    except OSError:
                        pass
            have = {p.name for d in dirs for p in d.glob("*.dll")}
            import onnxruntime
            _cuda_checked = (all(name in have for name in _CUDA_DLLS)
                             and "CUDAExecutionProvider" in onnxruntime.get_available_providers())
        except Exception:  # noqa: BLE001 - 任何 GPU 探测异常都回落 CPU
            _cuda_checked = False
    return _cuda_checked


def _to_array(img) -> np.ndarray:
    """PIL Image 转 RGB numpy 数组; numpy 数组原样返回."""
    if isinstance(img, Image.Image):
        return np.asarray(img.convert("RGB"))
    return img


def recognize(img) -> list[dict]:
    """整图 OCR, 返回文本条目列表 (纯本地引擎)."""
    key = _cache_key(img)
    if key is not None:
        with _CACHE_LOCK:
            if key in _cache:
                return [dict(it) for it in _cache[key]]
    result, _ = get_engine()(_to_array(img))
    items = []
    for box, text, score in (result or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({
            "text": text,
            "score": float(score),
            "box": (min(xs), min(ys), max(xs), max(ys)),
        })
    items.sort(key=lambda it: (it["box"][1], it["box"][0]))
    _cache_put(key, items)
    return [dict(it) for it in items]


def recognize_line(img) -> tuple[str, float]:
    """已定位的单行文字直接识别，跳过文字检测/方向分类，供高频状态检查。"""
    try:
        result, _ = get_engine()(_to_array(img), use_det=False, use_cls=False)
    except OcrEngineUnavailable:
        return "", 0.0
    if not result:
        return "", 0.0
    return str(result[0][0]), float(result[0][1])


def recognize_scaled(img, width: int = 1280) -> list[dict]:
    """先缩小整图再 OCR，将检测框映射回原图坐标，降低内存占用。"""
    source = _to_array(img)
    height, original_width = source.shape[:2]
    if original_width <= width:
        return recognize(source)
    target_height = round(height * width / original_width)
    small = np.asarray(Image.fromarray(source).resize((width, target_height), Image.LANCZOS))
    items = recognize(small)
    sx, sy = original_width / width, height / target_height
    for item in items:
        x1, y1, x2, y2 = item["box"]
        item["box"] = (x1 * sx, y1 * sy, x2 * sx, y2 * sy)
    return items


def recognize_isolated(img, timeout: float = 30.0) -> list[dict]:
    """进程内 OCR (历史遗留签名: 曾用子进程 + stdout JSON 隔离内存, 已废弃)。

    timeout 参数保留仅为兼容旧调用方, 已无实际作用.
    """
    try:
        return recognize(_to_array(img))
    except OcrEngineUnavailable:
        return []


def recognize_scaled_isolated(img, width: int = 1280) -> list[dict]:
    """缩放 + 进程内 OCR (历史签名保留, 行为同 `recognize_scaled`)。"""
    try:
        return recognize_scaled(img, width)
    except OcrEngineUnavailable:
        return []


def recognize_region(img, region: tuple[int, int, int, int],
                     upscale: float = 1.0) -> list[dict]:
    """裁剪区域后 OCR, box 坐标还原到整图坐标系.

    Args:
        img: 整图 (PIL Image 或 numpy 数组).
        region: (x1, y1, x2, y2).
        upscale: 裁剪后放大倍数, 小字体/艺术字建议 3~4.

    同一张图相同 region+upscale 的调用直接命中缓存 (参数折进缓存键, 见 _cache_key),
    因此针对整图漏检小字的放大重读不会被整图结果短路.
    """
    # 尺寸保护: 空区域直接返回, 不让 OCR 引擎对 0 尺寸图报错
    # (观战帧上经常会裁出 0 尺寸的边角)。
    try:
        _x1, _y1, _x2, _y2 = (int(v) for v in region)
    except (TypeError, ValueError):
        return []
    if _x2 - _x1 <= 0 or _y2 - _y1 <= 0:
        return []
    _arr = _to_array(img)
    if _arr is None or getattr(_arr, "size", 0) <= 0:
        return []

    key = _cache_key(img, region, upscale)
    if key is not None:
        with _CACHE_LOCK:
            if key in _cache:
                return [dict(it) for it in _cache[key]]
    x1, y1, x2, y2 = _x1, _y1, _x2, _y2
    crop = _arr[y1:y2, x1:x2]
    if getattr(crop, "size", 0) <= 0:
        return []
    if upscale != 1.0:
        pil = Image.fromarray(crop)
        crop = np.asarray(pil.resize(
            (round(pil.width * upscale), round(pil.height * upscale)),
            Image.LANCZOS,
        ))
    try:
        items = recognize(crop)
    except OcrEngineUnavailable:
        return []
    for it in items:
        bx1, by1, bx2, by2 = it["box"]
        it["box"] = (bx1 / upscale + x1, by1 / upscale + y1,
                     bx2 / upscale + x1, by2 / upscale + y1)
    _cache_put(key, items)
    return [dict(it) for it in items]


def region_text(img, region: tuple[int, int, int, int],
                upscale: float = 1.0) -> str:
    """区域识别并按 x 顺序拼接为单行文本, 适合单字段区域."""
    items = sorted(recognize_region(img, region, upscale),
                   key=lambda it: it["box"][0])
    return "".join(it["text"] for it in items)
