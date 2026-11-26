"""通用小工具：时间、哈希、文本归一化、图片占位符。"""

from __future__ import annotations

import hashlib
import os
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------


def now_iso() -> str:
    """当前时间的 ISO8601 字符串（本地时区，秒级）。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def now_stamp() -> str:
    """适合做文件名的时间戳：20261015-195241。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# 哈希
# --------------------------------------------------------------------------


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def short_hash(text: str, n: int = 12) -> str:
    return sha1_hex(text)[:n]


# --------------------------------------------------------------------------
# 文本归一化
# --------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)
_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """归一化题目文本：去零宽字符、统一空格、收敛空行。

    保留中文全角标点与换行结构，因为它们是题目语义的一部分。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text) if False else text  # 保留全角，不做 NFKC
    text = text.translate(_ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


def collapse_text(text: str) -> str:
    """把多行压成一行的紧凑版本，用于哈希与检索。"""
    return re.sub(r"\s+", " ", normalize_text(text)).strip()


def strip_html_tags(html: str) -> str:
    """极简去标签（用于兜底，不用来替代 BeautifulSoup）。"""
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return normalize_text(_unescape_entities(text))


def _unescape_entities(text: str) -> str:
    import html as _html

    return _html.unescape(text)


def make_content_hash(stem: str, options: Iterable[str] | None = None) -> str:
    """题目内容指纹：题干 + 选项。用于跨页去重。"""
    parts = [collapse_text(stem)]
    if options:
        parts.extend(collapse_text(o) for o in options)
    return sha256_hex("\u0001".join(parts))[:32]


# --------------------------------------------------------------------------
# 文件 / 路径
# --------------------------------------------------------------------------


def ensure_dir(path: str | os.PathLike) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


# --------------------------------------------------------------------------
# 随机 / 节流
# --------------------------------------------------------------------------


def jittered(base: float, jitter: float) -> float:
    """在 base 上下抖动，返回非负秒数。"""
    if base <= 0:
        return 0.0
    return max(0.0, base + random.uniform(-jitter, jitter))


def sleep_for(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


# --------------------------------------------------------------------------
# 公式图片占位符协议
#   题干里的数学公式在本站以 <img> 形式存在。解析时替换成 {{img:3}} 占位符，
#   后续可交给 VLM 转成 LaTeX —— 这是"多模态取题"的关键接口。
# --------------------------------------------------------------------------

IMG_PLACEHOLDER_RE = re.compile(r"\{\{img:(\d+)\}\}")


def placeholder(idx: int) -> str:
    return "{{img:%d}}" % idx


def resolve_placeholders(text: str, latex_map: dict[int, str]) -> str:
    """把 {{img:n}} 占位符替换成对应的 LaTeX（缺失的保留原样）。"""

    def _sub(m: re.Match) -> str:
        idx = int(m.group(1))
        val = latex_map.get(idx)
        return f" ${val}$ " if val else m.group(0)

    return IMG_PLACEHOLDER_RE.sub(_sub, text)
