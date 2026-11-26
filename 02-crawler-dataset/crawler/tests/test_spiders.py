"""适配器层测试：注册表、断点作用域、路线差异（全部离线）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from k12crawler.config import load_config  # noqa: E402
from k12crawler.fetcher import Fetcher  # noqa: E402
from k12crawler.spiders import available_spiders, get_spider  # noqa: E402


def test_three_routes_registered():
    names = set(available_spiders())
    assert {"jiajiao1010", "jiajiao1010-paper", "jiajiao1010-subject"} <= names


def test_index_routes_cursor_scope_is_name():
    cfg = load_config()
    f = Fetcher(cfg)
    assert get_spider("jiajiao1010", cfg, f).cursor_scope == "jiajiao1010"
    assert get_spider("jiajiao1010-paper", cfg, f).cursor_scope == "jiajiao1010-paper"


def test_subject_cursor_scope_includes_column():
    """换学科必须各自断点，否则换成 gzsx 会从 czsx 的页码接着跑。"""
    cfg = load_config()
    f = Fetcher(cfg)
    for column in ("czsx", "gzsx", "czwl"):
        cfg.as_dict()["crawler"]["column"] = column
        sp = get_spider("jiajiao1010-subject", cfg, f)
        assert sp.cursor_scope == f"jiajiao1010-subject:{column}"
        assert sp.index_url(3).endswith(f"/{column}/shiti_page_3")


def test_subject_route_urls():
    cfg = load_config()
    cfg.as_dict()["crawler"]["column"] = "czsx"
    sp = get_spider("jiajiao1010-subject", cfg, Fetcher(cfg))
    assert sp.index_url(1) == "http://www.1010jiajiao.com/czsx/shiti_page_1"
    assert sp.expands is False, "学科栏目路线是两跳直达，不需要额外下钻"
