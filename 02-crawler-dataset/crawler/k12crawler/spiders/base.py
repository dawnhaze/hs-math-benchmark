"""站点适配器基类 + 注册表。

新增一个题目来源只需要：
  1. 继承 BaseSpider，实现 index_url / parse_index / build_question
  2. 用 @register_spider 注册
  3. 在 config.yaml 里把 crawler.source 改成新名字
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from ..models import Question


class BaseSpider(ABC):
    """所有站点适配器的契约。"""

    name: str = "base"
    base_url: str = ""

    #: 索引页与题目页之间是否需要额外下钻一层。
    #: 为 True 时，pipeline 会把索引页的每张卡片先交给 expand()，由适配器
    #: 展开成真正的题目卡片，再去抓详情页。
    expands: bool = False

    def __init__(self, cfg, fetcher):
        self.cfg = cfg
        self.fetcher = fetcher
        self.base_url = self.base_url or cfg.get("crawler.base_url", "")

    # ------------------------------------------------------------- 身份
    @property
    def cursor_scope(self) -> str:
        """断点游标的作用域，也是 runs 表里的来源标识。

        默认就是适配器名。但若同一个适配器能"分频道"跑（例如学科栏目路线
        的 czsx / gzsx 共用一个类），子类必须把频道并进来 —— 否则换一个
        学科会接着上一个学科的页码跑，那是错的。
        """
        return self.name

    # ------------------------------------------------------------- 索引层
    @abstractmethod
    def index_url(self, page: int) -> str:
        """返回第 page 页索引的 URL。"""

    @abstractmethod
    def parse_index(self, html: str, page_url: str) -> List[Dict[str, Any]]:
        """解析索引页，返回题目卡片列表。"""

    def max_index_page(self, html: str) -> Optional[int]:
        """从索引页推断最大页码（用于自动判断"还有没有得挖"）。"""
        return None

    # ------------------------------------------------------------- 下钻层
    def expand(
        self,
        html: str,
        url: str,
        card: Dict[str, Any],
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """把一张索引卡片展开成多张题目卡片。

        默认实现返回空列表（表示不需要下钻）。需要多级遍历的站点在此
        自行决定要不要再发请求 —— 站点自身的遍历复杂度，留在适配器里。
        `limit` 是 pipeline 给出的建议上限（>0 时生效），用于控制单卡展开规模。
        """
        return []

    # ------------------------------------------------------------- 详情层
    def detail_url(self, card: Dict[str, Any]) -> str:
        return card.get("url", "")

    @abstractmethod
    def parse_detail(self, html: str, detail_url: str, card: Dict[str, Any]) -> Dict[str, Any]:
        """解析详情页，返回补充字段（答案/解析等）。"""

    # ------------------------------------------------------------- 组装
    @abstractmethod
    def to_question(
        self,
        card: Dict[str, Any],
        detail: Optional[Dict[str, Any]],
    ) -> Optional[Question]:
        """合并卡片与详情，产出 Question。"""

    # ------------------------------------------------------------- 工具
    def resolve(self, url: str) -> str:
        return self.fetcher.absolute(url)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r}>"


# ---------------------------------------------------------------------------
_SPIDERS: Dict[str, type] = {}


def register_spider(cls: type) -> type:
    name = getattr(cls, "name", None)
    if not name or name == "base":
        raise ValueError(f"{cls.__name__} 缺少有效的 name 属性")
    _SPIDERS[name] = cls
    return cls


def get_spider(name: str, cfg, fetcher) -> BaseSpider:
    if name not in _SPIDERS:
        raise KeyError(f"未知站点适配器 '{name}'。可用：{', '.join(available_spiders())}")
    return _SPIDERS[name](cfg, fetcher)


def available_spiders() -> List[str]:
    return sorted(_SPIDERS)
