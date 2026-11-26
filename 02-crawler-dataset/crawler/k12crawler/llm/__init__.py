"""模型子包：Backbone 注册 + 提示词 + 增强能力。"""

from .backbone import (  # noqa: F401
    BackboneError,
    ModelClient,
    build_client,
    parse_json_loose,
)
from . import prompts  # noqa: F401

__all__ = [
    "BackboneError",
    "ModelClient",
    "build_client",
    "parse_json_loose",
    "prompts",
]
