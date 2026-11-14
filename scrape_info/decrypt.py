"""datatft.com 前端资源发现与静态数据集解密.

发现链: 首页 HTML -> entry js (assets/index-*.js) -> bootstrap js
bootstrap 中包含: 版本数组(如 Mr=[{value:"18.3b"},{value:"18.3"},...])
                 与 data-cn-*.js 加密数据集的 import 路径.
数据集为 AES-128-ECB 加密的 base64 串: const c="....";export{c as data};
"""
import base64
import json
import re

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from . import config

_ENTRY_RE = re.compile(r"/assets/(index-[A-Za-z0-9_-]+\.js)")
_BOOTSTRAP_RE = re.compile(r"(bootstrap-[A-Za-z0-9_-]+\.js)")
_DATACN_RE = re.compile(r"(data-cn-[A-Za-z0-9_-]+\.js)")
# 加密载荷: const <变量>="....";export{<变量> as data};  变量名随构建变化, 不能写死
_PAYLOAD_RE = re.compile(r'const \w+="([^"]+)";export\{\w+ as data\}')
# 下拉选项数组, 如 [{value:"18.3b"},{value:"18.3"},...]
_ARRAY_RE = re.compile(r"\[(?:\{value:\"([^\"]+)\"\},?)+\]")
_VERSION_LIKE_RE = re.compile(r"^\d+\.\d+[a-z]?$")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Referer": config.SITE_BASE + "/"})
    return s


def decrypt_payload(data_b64: str) -> bytes:
    """解密 data-cn 数据集的 base64 载荷 (AES-128-ECB + PKCS7)."""
    raw = base64.b64decode(data_b64)
    key = base64.b64decode(config.AES_KEY_B64)
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    dec = cipher.decryptor()
    padded = dec.update(raw) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def fetch_bootstrap_js(session: requests.Session | None = None) -> str:
    """首页 -> entry js -> bootstrap js, 返回 bootstrap 源码文本."""
    s = session or make_session()
    html = s.get(config.SITE_BASE, timeout=config.REQUEST_TIMEOUT).text
    m = _ENTRY_RE.search(html)
    if not m:
        raise RuntimeError("首页未找到 entry js (assets/index-*.js)")
    entry = s.get(f"{config.SITE_BASE}/assets/{m.group(1)}", timeout=config.REQUEST_TIMEOUT).text
    m = _BOOTSTRAP_RE.search(entry)
    if not m:
        raise RuntimeError("entry js 未找到 bootstrap 引用")
    return s.get(f"{config.SITE_BASE}/assets/{m.group(1)}", timeout=config.REQUEST_TIMEOUT).text


def get_version_list(session: requests.Session | None = None) -> list[str]:
    """从 bootstrap chunk 提取正式版本列表(排除 jcc_/pbe 前缀), 首元素为最新."""
    bootstrap = fetch_bootstrap_js(session)
    for m in _ARRAY_RE.finditer(bootstrap):
        values = re.findall(r'\{value:"([^"]+)"\}', m.group(0))
        versions = [v for v in values if _VERSION_LIKE_RE.match(v)]
        if len(versions) >= 3:
            return versions
    raise RuntimeError("bootstrap 中未找到版本数组")


def get_latest_version(session: requests.Session | None = None) -> str:
    return get_version_list(session)[0]


def fetch_static_dataset(session: requests.Session | None = None) -> dict:
    """下载并解密 data-cn-*.js, 返回静态数据集 dict (heros18/equips18/races18/jobs18/charms18...)."""
    s = session or make_session()
    bootstrap = fetch_bootstrap_js(s)
    m = _DATACN_RE.search(bootstrap)
    if not m:
        raise RuntimeError("bootstrap 中未找到 data-cn chunk 引用")
    text = s.get(f"{config.SITE_BASE}/assets/{m.group(1)}", timeout=config.REQUEST_TIMEOUT).text
    m = _PAYLOAD_RE.search(text)
    if not m:
        raise RuntimeError("data-cn chunk 中未找到加密载荷 (const x=\"...\";export{x as data})")
    return json.loads(decrypt_payload(m.group(1)).decode("utf-8"))
