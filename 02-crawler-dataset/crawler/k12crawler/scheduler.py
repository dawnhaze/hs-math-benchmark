"""自动运行：把爬虫变成"填完配置就不用管"的守护进程。

三种运行姿态，覆盖从演示到生产：
  1. `k12crawler crawl`        —— 跑一轮就退出（人工触发 / CI 定时任务调用）
  2. `k12crawler daemon`       —— 常驻进程，按 interval_minutes 自动增量续爬
  3. 系统级调度（Windows 任务计划 / cron / systemd）调用方式 1

守护进程内部使用 APScheduler；若环境里没装，会优雅降级为 sleep 轮询。
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime, time as dtime
from typing import Any, Callable, Dict, Optional

from .config import Config
from .pipeline import CrawlPipeline
from .utils import now_iso

log = logging.getLogger("k12.scheduler")

RoundFn = Callable[[Dict[str, Any]], None]


def _in_window(spec: str, now: Optional[datetime] = None) -> bool:
    """判断当前时间是否落在 "01:00-06:00" 这类窗口内。空串表示不限制。"""
    spec = (spec or "").strip()
    if not spec:
        return True
    now = now or datetime.now()
    m = None
    import re

    m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", spec)
    if not m:
        log.warning("时间窗口格式无法识别：%s（应为 01:00-06:00）", spec)
        return True
    sh, sm, eh, em = (int(x) for x in m.groups())
    start, end = dtime(sh, sm), dtime(eh, em)
    cur = now.time()
    if start <= end:
        return start <= cur <= end
    # 跨零点
    return cur >= start or cur <= end


class CrawlScheduler:
    """自动运行调度器。"""

    def __init__(self, cfg: Config, on_round: Optional[RoundFn] = None):
        self.cfg = cfg
        self.s = cfg.section("schedule")
        self.interval = max(1, int(self.s.get("interval_minutes", 30) or 30))
        self.run_on_start = bool(self.s.get("run_on_start", True))
        self.window = str(self.s.get("window") or "")
        self.max_rounds = int(self.s.get("max_rounds", 0) or 0)
        self.on_round = on_round
        self._stop = threading.Event()
        self.rounds = 0

    # ------------------------------------------------------------------
    def stop(self, *_args: Any) -> None:
        if not self._stop.is_set():
            log.info("收到停止信号，将在当前轮结束后退出……")
            self._stop.set()

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError, AttributeError):  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    def run_one_round(self) -> Dict[str, Any]:
        """跑一轮增量爬取。"""
        self.rounds += 1
        log.info("=" * 68)
        log.info("第 %d 轮开始 · %s", self.rounds, now_iso())
        t0 = time.time()
        try:
            pipe = CrawlPipeline(self.cfg)
            result = pipe.run(note=f"daemon round {self.rounds}")
        except Exception as exc:  # noqa: BLE001
            log.exception("本轮异常：%s", exc)
            result = {"error": str(exc), "round": self.rounds}
        result["round"] = self.rounds
        result["elapsed_sec_total"] = round(time.time() - t0, 2)
        log.info(
            "第 %d 轮结束 · 页 %s · 新题 %s · 重复 %s · 存库总计 %s",
            self.rounds,
            result.get("pages_crawled"),
            result.get("inserted"),
            result.get("duplicated"),
            result.get("stored_total"),
        )
        if self.on_round:
            try:
                self.on_round(result)
            except Exception:  # noqa: BLE001
                pass
        return result

    # ------------------------------------------------------------------
    def serve(self) -> int:
        """常驻运行，直到收到停止信号或跑满 max_rounds。"""
        self.install_signal_handlers()
        log.info(
            "守护进程启动：每 %d 分钟一轮 · 启动即跑=%s · 时间窗口=%s",
            self.interval, self.run_on_start, self.window or "(全天)",
        )

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
        except ImportError:
            log.warning("未安装 APScheduler，降级为 sleep 轮询模式")
            return self._serve_loop()

        sched = BackgroundScheduler(timezone="Asia/Shanghai")
        sched.add_job(
            self._guarded_round,
            IntervalTrigger(minutes=self.interval),
            id="k12-crawl",
            max_instances=1,
            coalesce=True,
            next_run_time=None,
        )
        sched.start()
        try:
            if self.run_on_start:
                self._guarded_round()
            while not self._stop.is_set():
                if self.max_rounds and self.rounds >= self.max_rounds:
                    log.info("已完成 %d 轮，按配置退出", self.rounds)
                    break
                self._stop.wait(5)
        finally:
            sched.shutdown(wait=True)
        return 0

    def _guarded_round(self) -> None:
        if self._stop.is_set():
            return
        if self.max_rounds and self.rounds >= self.max_rounds:
            return
        if not _in_window(self.window):
            log.info("当前不在允许的时间窗口 %s 内，跳过本轮", self.window)
            return
        self.run_one_round()

    def _serve_loop(self) -> int:
        first = True
        while not self._stop.is_set():
            if self.max_rounds and self.rounds >= self.max_rounds:
                break
            if not first or self.run_on_start:
                self._guarded_round()
            first = False
            self._stop.wait(self.interval * 60)
        return 0


def run_daemon(cfg: Config, on_round: Optional[RoundFn] = None) -> int:
    return CrawlScheduler(cfg, on_round=on_round).serve()
