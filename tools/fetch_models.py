"""下载 ONNX 权重 (不进 git, 由 Release 附件分发).

为什么权重不进仓库:
  * 两个模型合计 21.3MB, 占仓库一半体积; 每次 clone 都要拉, 且二进制 diff 不可读;
  * Gitee / GitHub 的「发行版 (Release)」附件走独立存储, 不占 git 历史 —— 适合模型这种
    "版本化的大文件". 仓库里只留 `models/README.md` (清单 + SHA256 + 用途)。

用法
----
    python -m tools.fetch_models                 # 缺什么下什么, 已有且校验通过就跳过
    python -m tools.fetch_models --check-only    # 只校验本地权重, 不下载 (自测/CI 用)
    python -m tools.fetch_models --force         # 强制重下
    python -m tools.fetch_models --proxy http://127.0.0.1:7890
    python -m tools.fetch_models --base URL      # 换镜像/换 Release 版本
    python -m tools.fetch_models --dir D:\\models # 下到别处 (不推荐, 配置里写死了 models/)

退出码: 0 全部就绪 / 1 有缺失或校验失败 / 2 参数错
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MARK_OK, MARK_BAD, MARK_INFO = "[OK]", "[X]", "[i]"

# 默认 Release 基址: Gitee 发行版附件地址形如
#   https://gitee.com/<owner>/<repo>/releases/download/<tag>/<filename>
DEFAULT_BASE = "https://gitee.com/bode135/jcc_agent/releases/download/v0.1.0"
DEFAULT_DIR = PROJECT_ROOT / "models"

# 文件名 -> (SHA256, 用途说明)
MODELS: dict[str, tuple[str, str]] = {
    "yolo_v2.1_balls.onnx": (
        "95a61b76625079218480f4bbee47db3109a8c526604be723c97c196aa9c13be5",
        "球 / 小小英雄 / 调试图 (70 类, imgsz 1280)",
    ),
    "yolo_v4.1_units_11n.onnx": (
        "6300f59b5641efff8fdb52b0d21c88632cb193207a573b0c281d0e6bf54145da",
        "棋子识别 (11n, 11MB, mAP50 0.774)",
    ),
}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}MB"


def _opener(proxy: str | None):
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    else:
        handler = urllib.request.ProxyHandler()   # 走环境变量 HTTP(S)_PROXY
    return urllib.request.build_opener(handler)


def download(url: str, dst: Path, proxy: str | None) -> None:
    """下载到 dst.part 再改名, 中途失败不留半个文件."""
    tmp = dst.with_suffix(dst.suffix + ".part")
    opener = _opener(proxy)
    req = urllib.request.Request(url, headers={"User-Agent": "jcc-robot-by-img/fetch_models"})
    try:
        with opener.open(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got, t0, last = 0, time.perf_counter(), 0.0
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        pct = got * 100.0 / total
                        print(f"\r    {dst.name}: {pct:5.1f}%  {human(got)}/{human(total)}", end="", flush=True)
                    else:
                        print(f"\r    {dst.name}: {human(got)}", end="", flush=True)
            print()
            if total and got != total:
                raise urllib.error.ContentTooShortError(f"只收到 {got}/{total} 字节", None)
            _ = time.perf_counter() - t0 + last
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"HTTP {exc.code} {exc.reason} —— {url}") from exc
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dst)


def check_one(name: str, want: str, directory: Path) -> tuple[bool, str]:
    p = directory / name
    if not p.is_file():
        return False, f"缺 {name}"
    mb = p.stat().st_size / 1024 / 1024
    if mb < 8.0:            # 防 LFS 指针文件 / 下到一半
        return False, f"{name} 只有 {mb:.1f}MB (< 8MB), 疑似 LFS 指针或下载不完整"
    got = sha256_of(p)
    if got != want:
        return False, f"{name} SHA256 不符: {got[:16]}… != {want[:16]}… (文件被改过或下错版本)"
    return True, f"{name} {mb:.1f}MB SHA256 OK"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下载 ONNX 权重 (Release 附件)")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help=f"权重目录 (默认 {DEFAULT_DIR})")
    ap.add_argument("--base", default=os.environ.get("JCC_RELEASE_BASE", DEFAULT_BASE),
                    help="Release 附件基址 (也可用环境变量 JCC_RELEASE_BASE)")
    ap.add_argument("--proxy", default=None, help="如 http://127.0.0.1:7890 (否则走环境变量代理)")
    ap.add_argument("--force", action="store_true", help="已有文件也重下")
    ap.add_argument("--check-only", action="store_true", help="只校验, 不下载")
    args = ap.parse_args(argv)

    directory = Path(args.dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    print(f"{MARK_INFO} 权重目录: {directory}")
    print(f"{MARK_INFO} Release 基址: {args.base}")

    missing: list[str] = []
    for name, (sha, use) in MODELS.items():
        good, msg = check_one(name, sha, directory)
        print(f"{MARK_OK if good else MARK_BAD} {msg}   # {use}")
        if not good:
            missing.append(name)

    if args.check_only:
        print(f"{MARK_OK if not missing else MARK_BAD} 校验完成: {len(MODELS) - len(missing)}/{len(MODELS)} 就绪")
        return 0 if not missing else 1
    if not missing:
        print(f"{MARK_OK} 权重已就绪, 无需下载")
        return 0

    failed = []
    for name in missing:
        target = directory / name
        if target.exists() and not args.force:
            target.unlink()          # 校验失败的文件没保留价值
        url = f"{args.base.rstrip('/')}/{name}"
        print(f"{MARK_INFO} 下载 {name} <- {url}")
        try:
            download(url, target, args.proxy)
        except Exception as exc:      # noqa: BLE001 - 下载失败要给人可操作的提示
            print(f"{MARK_BAD} 下载失败: {exc}")
            failed.append(name)
            continue
        good, msg = check_one(name, MODELS[name][0], directory)
        print(f"{MARK_OK if good else MARK_BAD} {msg}")
        if not good:
            failed.append(name)

    if failed:
        print(f"{MARK_BAD} 仍有 {len(failed)} 个权重未就绪: {', '.join(failed)}")
        print(f"{MARK_INFO} 手动兜底: 打开 {args.base.rsplit('/releases/', 1)[0]}/releases "
              f"下载对应附件放进 {directory}")
        print(f"{MARK_INFO} 网络不通时先试代理: --proxy http://127.0.0.1:7890")
        return 1
    print(f"{MARK_OK} 全部权重就绪 ({len(MODELS)} 个)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
