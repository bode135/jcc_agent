"""OCR 通道兼容层 (纯本地): 历史版本的**远程 OCR 常驻服务客户端**已删除.

为什么保留这个模块
------------------
历史调用方可能 `from recognize import ocr_client` 后调 `use_remote()` / `optional()` /
`available()`, 甚至用 `OcrBusy` 捕获取消异常。新工程 `jcc_robot_by_img` 不跑
常驻 OCR 服务 (也不读内存/不跨进程), 所以这里**只剩一层转发**:

    * `available()` / `ensure_server()` -> 恒 False (没有远程通道可拉,
      且**绝不**拉起子进程, 更不会等 20s);
    * `use_remote()` -> 恒 False (告诉调用方"用本地引擎就好");
    * `recognize()` / `recognize_line()` / `recognize_region()` -> 直接转发
      `recognize.ocr` 的本地实现 (行为与直接调 ocr 完全一致, 不做任何降级);
    * `OcrBusy` / `optional()` -> 保留空壳 (本地引擎没有排队/忙等, 永不抛 OcrBusy)。

一句话: 这个模块现在是"本地 OCR 的别名", 导入它不会拉起任何进程、不会发任何 HTTP 请求。
"""

import contextlib

# 本地从来不会"忙" -> 永不抛出; 保留异常类型只为兼容历史 `except ocr_client.OcrBusy`。
class OcrBusy(RuntimeError):
    """历史远程客户端在服务忙时抛出; 本地模式下**永不抛出**, 仅作兼容保留。"""


@contextlib.contextmanager
def optional(label: str = ""):
    """历史: 申请远程识别许可, 服务忙则抛 OcrBusy 让调用方跳过。本地模式什么都不做."""
    yield


def available() -> bool:
    """是否存在可用的**远程** OCR 服务 -> 恒 False (本工程没有远程通道)。"""
    return False


def ensure_server(wait: float = 20.0) -> bool:
    """历史: 后台拉起常驻 OCR 服务并等它就绪 -> 恒 False, **不启进程、不等待**。"""
    return False


def use_remote(timeout: float = 2.0) -> bool:
    """历史: 探测/切换远程通道 -> 恒 False (调用方继续用本地引擎)。"""
    return False


def recognize(img) -> list[dict]:
    """转发本地 `recognize.ocr.recognize` (整图 OCR)。"""
    from recognize import ocr
    return ocr.recognize(img)


def recognize_line(img) -> tuple[str, float]:
    """转发本地 `recognize.ocr.recognize_line` (已定位单行)。"""
    from recognize import ocr
    return ocr.recognize_line(img)


def recognize_region(img, region: tuple[int, int, int, int],
                     upscale: float = 1.0) -> list[dict]:
    """转发本地 `recognize.ocr.recognize_region` (区域裁剪 + 放大)。"""
    from recognize import ocr
    return ocr.recognize_region(img, region, upscale)
