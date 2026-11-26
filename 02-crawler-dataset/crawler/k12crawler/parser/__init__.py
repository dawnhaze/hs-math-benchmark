"""解析子包。"""

from .html_parser import (  # noqa: F401
    SUBJECT_CODE_MAP,
    build_question,
    fragment_to_text,
    infer_qtype,
    make_qid,
    parse_column_cards,
    parse_detail,
    parse_index_cards,
    parse_max_index_page,
    split_answer_analysis,
    split_options,
    subject_from_code,
)

__all__ = [
    "build_question",
    "fragment_to_text",
    "infer_qtype",
    "make_qid",
    "parse_column_cards",
    "parse_detail",
    "parse_index_cards",
    "parse_max_index_page",
    "split_answer_analysis",
    "split_options",
    "subject_from_code",
    "SUBJECT_CODE_MAP",
]
