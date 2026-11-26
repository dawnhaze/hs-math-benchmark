"""数据模型：统一表示一道 K12 试题。

所有站点适配器最终都要产出 Question 对象，再交给存储层落库。
字段设计刻意对齐"可训练/可直接消费"的要求：题干、选项、答案、解析、图片、
知识点标签、难度，一个都不少。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .utils import make_content_hash, now_iso, normalize_text


@dataclass
class ImageRef:
    """题干/答案中内嵌的图片（公式、插图、表格截图等）。"""

    idx: int                     # 占位符序号，对应文本里的 {{img:n}}
    url: str                     # 绝对地址
    kind: str = "inline"         # inline | figure | formula
    width: Optional[int] = None
    height: Optional[int] = None
    alt: str = ""
    latex: str = ""              # 由 VLM 转换得到的 LaTeX（可为空）

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Question:
    """一道试题的规范化表示。"""

    # --- 身份 ---
    qid: str                                  # 全局唯一 ID，形如 "jiajiao1010:20"
    source: str                               # 站点 key
    source_url: str = ""

    # --- 分类元数据 ---
    subject: str = ""                         # 学科（初中英语 / 高中数学 ...）
    subject_code: str = ""                    # 站点内部学科代码（czyy / gzsx ...）
    grade: str = ""                           # 学段 / 年级
    qtype: str = ""                           # 题型（选择题 / 填空题 ...）
    paper: str = ""                           # 来源试卷 / 考试名称
    year: str = ""                            # 年份（能推断时填充）

    # --- 核心内容 ---
    stem: str = ""                            # 题干（纯文本，含 {{img:n}} 占位符）
    stem_html: str = ""                       # 题干（原始 HTML，保留排版）
    options: List[str] = field(default_factory=list)   # 选项 ["A．xxx", ...]

    # --- 答案与解析 ---
    answer: str = ""
    analysis: str = ""                        # 解析 / 解题思路
    answer_html: str = ""

    # --- 扩展 ---
    images: List[ImageRef] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)      # 知识点标签
    difficulty: Optional[float] = None                 # 0~1，越难越大

    # --- 溯源与质量 ---
    parser: str = "html"                      # html | vlm | llm | text
    content_hash: str = ""
    crawled_at: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        self.stem = normalize_text(self.stem)
        self.answer = normalize_text(self.answer)
        self.analysis = normalize_text(self.analysis)
        self.options = [normalize_text(o) for o in (self.options or []) if o and o.strip()]
        if not self.crawled_at:
            self.crawled_at = now_iso()
        if not self.content_hash:
            self.content_hash = make_content_hash(self.stem, self.options)
        if isinstance(self.difficulty, str):
            try:
                self.difficulty = float(self.difficulty)
            except ValueError:
                self.difficulty = None

    # ------------------------------------------------------------------
    @property
    def image_dicts(self) -> List[Dict[str, Any]]:
        return [i.to_dict() for i in self.images]

    def flat_text(self) -> str:
        """把一道题压成单行纯文本，便于关键词检索与向量化。"""
        buf = [self.stem]
        if self.options:
            buf.append(" ".join(self.options))
        if self.answer:
            buf.append("答案：" + self.answer)
        return normalize_text(" ".join(buf)).replace("\n", " ")

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["images"] = self.image_dicts
        return d

    def to_json(self, **kw: Any) -> str:
        kw.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kw)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Question":
        """从 dict 还原题目。

        数据库行用的是 `xxx_json` 列名，而模型字段是 `xxx`。这里两种都认 ——
        否则从库里读回来的题会静默丢掉图片、选项、标签和扩展字段。
        """
        d = dict(d)

        def _json_value(*keys: str, fallback: Any) -> Any:
            for k in keys:
                if k not in d or d[k] is None:
                    continue
                v = d.pop(k)
                if isinstance(v, str):
                    v = v.strip()
                    if not v:
                        return fallback
                    try:
                        return json.loads(v)
                    except json.JSONDecodeError:
                        return fallback
                return v
            return fallback

        imgs = _json_value("images", "images_json", fallback=[])
        opts = _json_value("options", "options_json", fallback=[])
        tags = _json_value("tags", "tags_json", fallback=[])
        extra = _json_value("extra", "extra_json", fallback={})

        allowed = {f.name for f in dataclasses.fields(cls)}
        clean = {k: v for k, v in d.items() if k in allowed}
        q = cls(**clean)
        q.options = [normalize_text(o) for o in (opts or []) if o and str(o).strip()]
        q.tags = list(tags or [])
        q.extra = dict(extra or {})
        q.images = [ImageRef(**i) if isinstance(i, dict) else i for i in (imgs or [])]
        return q
