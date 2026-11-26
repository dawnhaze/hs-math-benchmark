"""HTTP 抓取器：限速、重试、UA 轮换、robots 遵守、GBK 解码。

一个爬虫能不能"跑得住"，八成取决于这个文件写得稳不稳。
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from .config import Config
from .utils import jittered

log = logging.getLogger("k12.fetcher")

_CHARSET_RE = re.compile(rb"charset\s*=\s*[\"']?([\w-]+)", re.I)
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)", re.I)


@dataclass
class FetchStats:
    requests: int = 0
    ok: int = 0
    errors: int = 0
    retries: int = 0
    bytes: int = 0
    blocked_by_robots: int = 0
    by_status: Dict[int, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "ok": self.ok,
            "errors": self.errors,
            "retries": self.retries,
            "bytes": self.bytes,
            "blocked_by_robots": self.blocked_by_robots,
            "by_status": {str(k): v for k, v in sorted(self.by_status.items())},
        }


class Fetcher:
    """线程安全的、带礼貌节流的 HTTP 抓取器。"""

    def __init__(self, cfg: Config, session: Optional[requests.Session] = None):
        self.cfg = cfg
        c = cfg.section("crawler")

        self.base_url: str = c.get("base_url", "")
        self.timeout: int = int(c.get("timeout", 20))
        self.retries: int = int(c.get("retries", 3))
        self.backoff: float = float(c.get("backoff", 2.0))
        self.delay: float = float(c.get("delay", 0.6))
        self.jitter: float = float(c.get("jitter", 0.3))
        self.respect_robots: bool = bool(c.get("respect_robots", True))
        self.user_agents = list(c.get("user_agents") or ["Mozilla/5.0"])
        self.default_encoding: str = c.get("encoding", "gbk")

        self.stats = FetchStats()
        self._lock = threading.Lock()
        self._last_request_at: float = 0.0
        self._robots: Dict[str, Optional[RobotFileParser]] = {}

        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
            }
        )
        proxy = (c.get("proxy") or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
            log.info("使用代理：%s", proxy)

        cookie = ((c.get("account") or {}).get("cookie") or "").strip()
        if cookie:
            self.session.headers["Cookie"] = cookie
            log.info("已注入登录 Cookie（%d 字节）", len(cookie))

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _next_ua(self) -> str:
        return random.choice(self.user_agents) if self.user_agents else "Mozilla/5.0"

    def _throttle(self) -> None:
        """全局节流：保证同一时刻只有一个请求在跑，并保证最小间隔。"""
        with self._lock:
            wait = jittered(self.delay, self.jitter) - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _robots_for(self, url: str) -> Optional[RobotFileParser]:
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            if root in self._robots:
                return self._robots[root]
        rp: Optional[RobotFileParser] = None
        try:
            robots_url = urljoin(root, "/robots.txt")
            resp = self.session.get(
                robots_url, timeout=self.timeout, headers={"User-Agent": self._next_ua()}
            )
            rp = RobotFileParser()
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp = None  # 没有 robots.txt -> 视为允许
        except Exception as exc:  # noqa: BLE001
            log.debug("robots.txt 获取失败（按允许处理）：%s", exc)
            rp = None
        with self._lock:
            self._robots[root] = rp
        return rp

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch("*", url)
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _detect_encoding(raw: bytes, content_type: str, default: str = "gbk") -> str:
        m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
        if m:
            return m.group(1)
        m2 = _CHARSET_RE.search(raw[:4096])
        if m2:
            return m2.group(1).decode("ascii", "ignore")
        m3 = _META_CHARSET_RE.search(raw[:4096])
        if m3:
            return m3.group(1).decode("ascii", "ignore")
        return default

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
        encoding: Optional[str] = None,
        allow_404: bool = False,
    ) -> Optional[str]:
        """抓取文本页面。失败返回 None（不抛异常，交给上层决定）。"""
        if not self._allowed(url):
            self.stats.blocked_by_robots += 1
            log.warning("robots.txt 禁止抓取，已跳过：%s", url)
            return None

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            headers = {"User-Agent": self._next_ua()}
            if referer:
                headers["Referer"] = referer
            try:
                self.stats.requests += 1
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
                self.stats.by_status[resp.status_code] = (
                    self.stats.by_status.get(resp.status_code, 0) + 1
                )
                self.stats.bytes += len(resp.content or b"")

                if resp.status_code == 404 and allow_404:
                    self.stats.ok += 1
                    return None
                if resp.status_code >= 400:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")

                enc = encoding or self._detect_encoding(
                    resp.content, resp.headers.get("Content-Type", ""), self.default_encoding
                )
                try:
                    text = resp.content.decode(enc, errors="replace")
                except LookupError:
                    text = resp.content.decode("utf-8", errors="replace")
                self.stats.ok += 1
                return text

            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.stats.errors += 1
                if attempt < self.retries:
                    self.stats.retries += 1
                    wait = self.backoff ** attempt
                    log.debug("抓取失败(%d/%d) %s：%s，%.1fs 后重试", attempt, self.retries, url, exc, wait)
                    time.sleep(wait)
        log.warning("放弃抓取 %s：%s", url, last_exc)
        return None

    def get_bytes(self, url: str, *, referer: Optional[str] = None) -> Optional[bytes]:
        """抓取二进制（图片等）。"""
        if not self._allowed(url):
            self.stats.blocked_by_robots += 1
            return None
        for attempt in range(1, self.retries + 1):
            self._throttle()
            headers = {"User-Agent": self._next_ua(), "Accept": "image/*,*/*;q=0.8"}
            if referer:
                headers["Referer"] = referer
            try:
                self.stats.requests += 1
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
                self.stats.by_status[resp.status_code] = (
                    self.stats.by_status.get(resp.status_code, 0) + 1
                )
                if resp.status_code >= 400:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                data = resp.content
                self.stats.bytes += len(data or b"")
                self.stats.ok += 1
                return data
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                if attempt < self.retries:
                    self.stats.retries += 1
                    time.sleep(self.backoff ** attempt)
                else:
                    log.debug("图片抓取失败 %s：%s", url, exc)
        return None

    def absolute(self, url: str) -> str:
        """把相对/协议相对地址补全成绝对地址（本站大量使用 //thumb.zyjl.cn/...）。"""
        if not url:
            return ""
        if url.startswith("//"):
            scheme = urlparse(self.base_url).scheme or "http"
            return f"{scheme}:{url}"
        if url.startswith("http://") or url.startswith("https://"):
            return url
        return urljoin(self.base_url or "", url)
