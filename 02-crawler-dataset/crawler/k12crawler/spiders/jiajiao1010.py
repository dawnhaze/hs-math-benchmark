"""站点适配器：精英家教网 1010jiajiao.com（青夏教育）。

为什么选它？
  * 公开可访问、无需登录即可取到题干与答案；
  * 覆盖小学 / 初中 / 高中 全学科，题目总量在百万量级；
  * 索引页分页规整（xiti_page_1 ... xiti_page_97066），每页 10 题，
    天然适合增量、断点续爬；
  * 题目公式以图片形式内嵌，正好用来驱动多模态解析链路。

索引页：  {base}/xiti_page_{N}
详情页：  {base}/xiti_id_{M}
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from ..models import Question
from ..parser import (
    build_question,
    make_qid,
    parse_column_cards,
    parse_detail,
    parse_index_cards,
    parse_max_index_page,
    split_options,
    subject_from_code,
)
from ..utils import normalize_text
from .base import BaseSpider, register_spider


@register_spider
class Jiajiao1010Spider(BaseSpider):
    name = "jiajiao1010"
    base_url = "http://www.1010jiajiao.com"

    # ------------------------------------------------------------------
    def index_url(self, page: int) -> str:
        return f"{self.base_url}/xiti_page_{int(page)}"

    def parse_index(self, html: str, page_url: str) -> List[Dict[str, Any]]:
        return parse_index_cards(html, page_url, self.resolve)

    def max_index_page(self, html: str) -> Optional[int]:
        return parse_max_index_page(html)

    # ------------------------------------------------------------------
    def detail_url(self, card: Dict[str, Any]) -> str:
        return card.get("url", "")

    def parse_detail(self, html: str, detail_url: str, card: Dict[str, Any]) -> Dict[str, Any]:
        return parse_detail(
            html,
            detail_url,
            self.resolve,
            fallback_subject=card.get("subject", ""),
        )

    # ------------------------------------------------------------------
    def to_question(
        self,
        card: Dict[str, Any],
        detail: Optional[Dict[str, Any]],
    ) -> Optional[Question]:
        url = self.detail_url(card)
        if not url:
            return None
        return build_question(
            source=self.name,
            qid=make_qid(self.name, url),
            url=url,
            card=card,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# 第二条路线：试卷 → 题目列表 → 完整解答
# ---------------------------------------------------------------------------
_TITLE_SUFFIX_RE = re.compile(r"[-—–]{2,}.*$")


@register_spider
class Jiajiao1010PaperSpider(Jiajiao1010Spider):
    """走"试卷"路线的适配器。

    路线：paper_page_{N} → paper_id_{M} → timu_id_{K} → shiti_id_{H}

    为什么还要第二条路线？实测发现两类详情页给的东西不一样：
      · xiti_id_ 页面：给答案，但**不给解析**；
      · shiti_id_ 页面：给的是完整解答过程（"分析：…解答：解：…"）。

    但必须如实说明其代价：`timu_id` 页面列出的是与选题**高度相似**的题目，
    并非原卷原题，因此这条路线产出的语料相似度偏高，去重压力大。适合在
    需要"带完整解答过程"的语料时按需开启（`crawler.source: jiajiao1010-paper`）。
    """

    name = "jiajiao1010-paper"
    expands = True

    # ------------------------------------------------------------------
    def index_url(self, page: int) -> str:
        return f"{self.base_url}/paper_page_{int(page)}"

    def parse_index(self, html: str, page_url: str) -> List[Dict[str, Any]]:
        """试卷列表页 -> 试卷卡片。"""
        soup = BeautifulSoup(html, "lxml")
        cards: List[Dict[str, Any]] = []
        seen = set()
        for a in soup.find_all("a", href=True):
            m = re.search(r"paper_id_(\d+)", a["href"])
            if not m:
                continue
            url = self.resolve(a["href"])
            if url in seen:
                continue
            seen.add(url)
            cards.append(
                {
                    "url": url,
                    "paper_id": m.group(1),
                    "kind": "paper",
                    "page_url": page_url,
                }
            )
        return cards

    def max_index_page(self, html: str) -> Optional[int]:
        nums = [int(n) for n in re.findall(r"paper_page_(\d+)", html)]
        return max(nums) if nums else None

    # ------------------------------------------------------------------
    def expand(
        self,
        html: str,
        url: str,
        card: Dict[str, Any],
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """试卷页 -> 逐题下钻，产出题目卡片（指向 shiti 详情页）。"""
        soup = BeautifulSoup(html, "lxml")
        title_node = soup.find("title")
        paper_title = normalize_text(title_node.get_text() if title_node else "")
        paper_title = _TITLE_SUFFIX_RE.sub("", paper_title).strip()

        timu_urls: List[str] = []
        seen = set()
        for a in soup.find_all("a", href=True):
            if "timu_id_" not in a["href"]:
                continue
            u = self.resolve(a["href"])
            if u not in seen:
                seen.add(u)
                timu_urls.append(u)

        cap = limit if limit > 0 else int(self.cfg.get("crawler.questions_per_paper", 6) or 6)
        out: List[Dict[str, Any]] = []
        for timu_url in timu_urls[:cap]:
            thtml = self.fetcher.get(timu_url, referer=url, allow_404=True)
            if not thtml:
                continue
            out.extend(self._cards_from_timu_page(thtml, timu_url, paper_title, card))
        return out

    def _cards_from_timu_page(
        self,
        html: str,
        timu_url: str,
        paper_title: str,
        paper_card: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """题目列表页 -> 题目卡片（取第一条匹配的 shiti 链接）。"""
        soup = BeautifulSoup(html, "lxml")
        title_node = soup.find("title")
        raw_title = normalize_text(title_node.get_text() if title_node else "")
        stem_hint = _TITLE_SUFFIX_RE.sub("", raw_title).strip()

        shiti_urls: List[str] = []
        seen = set()
        for a in soup.find_all("a", href=True):
            if "shiti_id_" not in a["href"]:
                continue
            u = self.resolve(a["href"])
            if u not in seen:
                seen.add(u)
                shiti_urls.append(u)

        if not shiti_urls:
            return []

        target = self._pick_best_match(shiti_urls, stem_hint, timu_url)

        stem, options = split_options(stem_hint)
        return [
            {
                "url": target,
                "subject": "",
                "paper": paper_title,
                "qtype": "",
                "stem": stem,
                "stem_html": "",
                "options": options,
                "images": [],
                "page_url": paper_card.get("page_url", ""),
                "via": timu_url,
                "kind": "question",
            }
        ]

    def _pick_best_match(self, candidates: List[str], stem_hint: str, referer: str) -> str:
        """在相似题里挑题干最接近的一条。

        站点把"最相关的一条"放在最前面，所以先取第一条；只有当它明显
        与题干预览完全不同（相似度过低）时才回退到第一条本身。
        """
        best_url, best_score = candidates[0], -1.0
        for url in candidates[:3]:
            html = self.fetcher.get(url, referer=referer, allow_404=True)
            if not html:
                continue
            detail = parse_detail(html, url, self.resolve)
            score = _similarity(stem_hint, detail.get("stem", ""))
            if score > best_score:
                best_url, best_score = url, score
            if score >= 0.85:
                break
        return best_url

    # ------------------------------------------------------------------
    def to_question(
        self,
        card: Dict[str, Any],
        detail: Optional[Dict[str, Any]],
    ) -> Optional[Question]:
        return build_question(
            source=self.name,
            qid=make_qid(self.name, self.detail_url(card)),
            url=self.detail_url(card),
            card=card,
            detail=detail,
        )


def _similarity(a: str, b: str) -> float:
    """极简字符级相似度（Jaccard on bigrams），够用来排序候选。"""
    if not a or not b:
        return 0.0

    def grams(s: str) -> set:
        s = re.sub(r"[\s，。．、（）()\[\]【】_]+", "", s)
        return {s[i:i + 2] for i in range(max(0, len(s) - 1))} or {s}

    ga, gb = grams(a), grams(b)
    inter = len(ga & gb)
    union = len(ga | gb) or 1
    return inter / union


# ---------------------------------------------------------------------------
# 第三条路线：学科栏目 → 完整解答（推荐的首选路线）
# ---------------------------------------------------------------------------
@register_spider
class Jiajiao1010SubjectSpider(BaseSpider):
    """走"学科栏目"路线的适配器。

    路线：`{column}/shiti_page_{N}` → `{column}/shiti_id_{H}`（**只两跳**）

    为什么这是最好的那条路？
      · **学科定向**：column 可配（czsx 初中数学 / gzsx 高中数学 / czwl 初中物理 …），
        不像全局 xiti_page 那样前几十页被同一学科占满，取数更有代表性；
      · **解析齐全**：详情页直接给【答案】+【解析】+【解法提示】，
        解析覆盖率远高于 xiti_id_ 页面（后者只有答案）；
      · **直达**：不需要经过 timu_id 相似题中转，语料干净、去重压力小。
    """

    name = "jiajiao1010-subject"

    @property
    def column(self) -> str:
        return str(self.cfg.get("crawler.column", "czsx") or "czsx").strip("/")

    @property
    def cursor_scope(self) -> str:
        # 同一个适配器要能按学科各跑各的，断点游标必须带上 column，
        # 否则换成 gzsx 会从 czsx 的页码接着跑。
        return f"{self.name}:{self.column}"

    # ------------------------------------------------------------------
    def index_url(self, page: int) -> str:
        return f"{self.base_url}/{self.column}/shiti_page_{int(page)}"

    def parse_index(self, html: str, page_url: str) -> List[Dict[str, Any]]:
        name, _stage = subject_from_code(self.column)
        return parse_column_cards(
            html,
            page_url,
            self.resolve,
            subject_hint=name,
            subject_code=self.column,
        )

    def max_index_page(self, html: str) -> Optional[int]:
        return parse_max_index_page(html, "shiti_page")

    # ------------------------------------------------------------------
    def detail_url(self, card: Dict[str, Any]) -> str:
        return card.get("url", "")

    def parse_detail(self, html: str, detail_url: str, card: Dict[str, Any]) -> Dict[str, Any]:
        return parse_detail(
            html,
            detail_url,
            self.resolve,
            fallback_subject=card.get("subject", ""),
        )

    def to_question(
        self,
        card: Dict[str, Any],
        detail: Optional[Dict[str, Any]],
    ) -> Optional[Question]:
        url = self.detail_url(card)
        if not url:
            return None
        q = build_question(
            source=self.name,
            qid=make_qid(self.name, url),
            url=url,
            card=card,
            detail=detail,
        )
        if q and not q.subject_code:
            q.subject_code = card.get("subject_code", "")
        return q
