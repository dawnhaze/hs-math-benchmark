"""站点适配器子包。

导入各适配器模块即完成注册。
"""

from .base import (  # noqa: F401
    BaseSpider,
    available_spiders,
    get_spider,
    register_spider,
)
from . import jiajiao1010  # noqa: F401  (触发注册)

__all__ = [
    "BaseSpider",
    "available_spiders",
    "get_spider",
    "register_spider",
]
