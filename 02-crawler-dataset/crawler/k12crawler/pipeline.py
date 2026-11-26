"""爬取流水线：索引页 -> 题目卡片 -> 详情页 -> 入库。

一次 run() 就是一次完整的、可中断、可续跑的采集循环：
    · 从上次中断的页继续（断点续爬）
    · 并发抓详情，但整体仍受全局节流保护
    · 边抓边入库，进程被杀也不丢已采数据
    · 定期导出 JSON 快照
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .fetcher import Fetcher
from .spiders import get_spider
from .storage import SQLiteStore
from .utils import ensure_dir, now_stamp, sleep_for

log = logging.getLogger("k12.pipeline")

ProgressFn = Callable[[str, Dict[str, Any]], None]


class CrawlPipeline:
    """把配置、抓取器、解析器、存储串成一条自动流水线。"""

    def __init__(self, cfg: Config, store: Optional[SQLiteStore] = None, fetcher: Optional[Fetcher] = None):
        self.cfg = cfg
        self.fetcher = fetcher or Fetcher(cfg)
        self.store = store or SQLiteStore(
            cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"),
            cfg.path_of("storage.jsonl_path", "data/questions.jsonl"),
        )
        self.source_name = cfg.get("crawler.source", "jiajiao1010")
        self.spider = get_spider(self.source_name, cfg, self.fetcher)
        # 游标作用域：默认等于适配器名；可"分频道"的适配器（如学科栏目路线）
        # 会带上频道（column），从而各学科各跑各的断点。
        self.scope = self.spider.cursor_scope

    # ------------------------------------------------------------------
    def run(
        self,
        *,
        pages: Optional[int] = None,
        start_page: Optional[int] = None,
        max_questions: Optional[int] = None,
        fetch_detail: Optional[bool] = None,
        reset_cursor: bool = False,
        progress: Optional[ProgressFn] = None,
        note: str = "",
    ) -> Dict[str, Any]:
        c = self.cfg.section("crawler")
        pages = int(c.get("max_pages", 40)) if pages is None else int(pages)
        max_questions = int(c.get("max_questions", 400)) if max_questions is None else int(max_questions)
        fetch_detail = bool(c.get("fetch_detail", True)) if fetch_detail is None else bool(fetch_detail)
        step = max(1, int(c.get("page_step", 1)))
        concurrency = max(1, int(c.get("concurrency", 4)))
        # 稀疏矿脉策略：连续多少空页后开始跳跃探矿，以及跳跃基数
        tolerance = max(1, int(c.get("empty_tolerance", 3)))
        jump = max(2, int(c.get("empty_jump", 8)))
        # 安全阀：无论怎么跳跃，一轮最多尝试这么多次索引页，避免无限循环
        attempt_budget = max(200, pages * 12) if pages > 0 else 5000

        if reset_cursor:
            self.store.set_state(f"{self.scope}:next_page", int(c.get("start_page", 1)))
        page = int(start_page) if start_page else self.store.next_page(self.scope, c.get("start_page", 1))

        run_id = self.store.start_run(self.scope, note or f"pages={pages} detail={fetch_detail}")
        t0 = time.time()
        summary: Dict[str, Any] = {
            "run_id": run_id,
            "source": self.scope,
            "start_page": page,
            "pages_crawled": 0,
            "pages_attempted": 0,
            "pages_skipped": 0,
            "probes": 0,
            "max_index_page": None,
            "cards_seen": 0,
            "questions": 0,
            "inserted": 0,
            "duplicated": 0,
            "errors": 0,
            "next_page": page,
            "stopped_by": "completed",
        }
        snapshot_dir = self.cfg.path_of("storage.json_snapshot_dir", "data/json")
        snap_every = int(self.cfg.get("storage.snapshot_every", 200) or 200)

        pages_done = 0
        consecutive_empty = 0
        max_page: Optional[int] = None

        while True:
            # ---- 终止条件 ----
            if pages > 0 and pages_done >= pages:
                summary["stopped_by"] = "pages_limit"
                break
            if max_questions and summary["questions"] >= max_questions:
                summary["stopped_by"] = "max_questions"
                break
            if max_page is not None and page > max_page:
                summary["stopped_by"] = "reached_end"
                break
            if summary["pages_attempted"] >= attempt_budget:
                summary["stopped_by"] = "attempt_budget"
                break

            url = self.spider.index_url(page)
            summary["pages_attempted"] += 1
            html = self.fetcher.get(url, allow_404=True)
            cards = self.spider.parse_index(html, url) if html else []
            if not html:
                summary["errors"] += 1
            elif max_page is None:
                # 拿到第一个有效索引页就把分页上限记下来 —— 必须是"每页都探"，
                # 不能只在空页分支里探：密矿（如学科栏目）永远不会出现空页，
                # 那样无人值守时就不知道什么时候到头了。
                max_page = self.spider.max_index_page(html)
                summary["max_index_page"] = max_page

            # ---- 空页 / 死区：不停止，改为跳跃探矿 ----
            if not cards:
                consecutive_empty += 1
                summary["pages_skipped"] += 1
                if consecutive_empty >= tolerance:
                    # 指数探矿：越久没找到题，跳得越远（上限 5000 页），
                    # 这样即使面对上万页的死区也能在十几次尝试内穿过去。
                    skip = min(jump * (2 ** (consecutive_empty - tolerance)), 5000)
                    summary["probes"] += 1
                    self._emit(
                        progress, "probe",
                        {"from_page": page, "to_page": page + skip,
                         "empty_streak": consecutive_empty, **summary},
                    )
                    page += skip
                else:
                    page += step
                continue

            # ---- 下钻展开（索引页 -> 中间页 -> 题目页 的站点需要）----
            if self.spider.expands:
                cards = self._expand_cards(cards, concurrency, max_questions)
                if not cards:
                    consecutive_empty += 1
                    summary["pages_skipped"] += 1
                    page += step
                    continue

            # ---- 正常页 ----
            consecutive_empty = 0
            summary["max_index_page"] = max_page
            self._emit(progress, "page", {"page": page, "cards": len(cards), **summary})
            summary["cards_seen"] += len(cards)

            details = self._fetch_details(cards, concurrency) if fetch_detail else {}

            batch: List = []
            for card in cards:
                if max_questions and summary["questions"] >= max_questions:
                    break
                det = details.get(self.spider.detail_url(card))
                try:
                    q = self.spider.to_question(card, det)
                except Exception as exc:  # noqa: BLE001
                    summary["errors"] += 1
                    log.warning("题目组装失败 %s：%s", card.get("url"), exc)
                    continue
                if q is None:
                    continue
                batch.append(q)
                summary["questions"] += 1

            if batch:
                st = self.store.save_many(batch)
                summary["inserted"] += st["inserted"]
                summary["duplicated"] += st["duplicated"]
                if self.cfg.get("storage.save_raw_html"):
                    self._dump_raw(cards, details)

            page += step
            pages_done += 1
            summary["pages_crawled"] = pages_done
            summary["next_page"] = page
            self.store.set_state(f"{self.scope}:next_page", page)

            if snap_every and summary["inserted"] and summary["inserted"] % snap_every < len(batch):
                self._snapshot(snapshot_dir)

            self._emit(progress, "page_done", summary)

        self._snapshot(snapshot_dir)
        summary["elapsed_sec"] = round(time.time() - t0, 2)
        summary["fetch_stats"] = self.fetcher.stats.as_dict()
        summary["stored_total"] = self.store.count()
        self.store.finish_run(
            run_id,
            pages=summary["pages_crawled"],
            fetched=summary["cards_seen"],
            inserted=summary["inserted"],
            duplicated=summary["duplicated"],
            errors=summary["errors"],
            note=summary["stopped_by"],
        )
        return summary

    # ------------------------------------------------------------------
    def _expand_cards(
        self, cards: List[Dict[str, Any]], concurrency: int, max_questions: int = 0
    ) -> List[Dict[str, Any]]:
        """并发下钻，把索引卡片展开成题目卡片。"""
        # 给每张卡片一个建议上限，避免"一页卡片 × 每卡数十题"把请求量放大到失控
        per_card = 0
        if max_questions:
            per_card = max(3, max_questions // max(1, len(cards)))
        out: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(self._one_expand, card, per_card): card for card in cards}
            for fut in as_completed(futures):
                try:
                    out.extend(fut.result() or [])
                except Exception as exc:  # noqa: BLE001
                    log.debug("下钻展开失败：%s", exc)
        return out

    def _one_expand(self, card: Dict[str, Any], limit: int = 0) -> List[Dict[str, Any]]:
        url = card.get("url") or ""
        if not url:
            return []
        html = self.fetcher.get(url, allow_404=True)
        if not html:
            return []
        return self.spider.expand(html, url, card, limit=limit) or []

    # ------------------------------------------------------------------
    def _fetch_details(self, cards: List[Dict[str, Any]], concurrency: int) -> Dict[str, Any]:
        """并发抓详情页；返回 {detail_url: parsed_detail}。"""
        out: Dict[str, Any] = {}
        ref = self.spider.index_url(1)
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(self._one_detail, card, ref): self.spider.detail_url(card)
                for card in cards
                if self.spider.detail_url(card)
            }
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    res = fut.result()
                    if res is not None:
                        out[url] = res
                except Exception as exc:  # noqa: BLE001
                    log.debug("详情抓取失败 %s：%s", url, exc)
        return out

    def _one_detail(self, card: Dict[str, Any], referer: str) -> Optional[Dict[str, Any]]:
        url = self.spider.detail_url(card)
        if not url:
            return None
        html = self.fetcher.get(url, referer=referer, allow_404=True)
        if not html:
            return None
        return self.spider.parse_detail(html, url, card)

    # ------------------------------------------------------------------
    def _snapshot(self, snapshot_dir: Path) -> None:
        try:
            ensure_dir(snapshot_dir)
            path = Path(snapshot_dir) / f"questions-{now_stamp()}.json"
            self.store.export_json(path)
            log.debug("已导出快照：%s", path)
        except Exception as exc:  # noqa: BLE001
            log.warning("快照导出失败：%s", exc)

    def _dump_raw(self, cards: List[Dict[str, Any]], details: Dict[str, Any]) -> None:
        raw_dir = self.cfg.path_of("storage.raw_html_dir", "data/raw")
        ensure_dir(raw_dir)
        import json

        for card in cards:
            key = self.spider.detail_url(card).rsplit("/", 1)[-1] or "unknown"
            try:
                with open(Path(raw_dir) / f"{key}.json", "w", encoding="utf-8") as f:
                    json.dump(
                        {"card": {k: v for k, v in card.items() if k != "images"},
                         "detail": {k: v for k, v in (details.get(self.spider.detail_url(card)) or {}).items()}},
                        f, ensure_ascii=False, indent=1,
                    )
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    @staticmethod
    def _emit(progress: Optional[ProgressFn], event: str, payload: Dict[str, Any]) -> None:
        if progress:
            try:
                progress(event, payload)
            except Exception:  # noqa: BLE001
                pass
