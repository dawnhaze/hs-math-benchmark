"""工具函数与端到端自检的测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from k12crawler.config import load_config  # noqa: E402
from k12crawler.utils import (  # noqa: E402
    collapse_text,
    make_content_hash,
    normalize_text,
    resolve_placeholders,
    short_hash,
)


def test_normalize_text():
    assert normalize_text("  a\u00a0\u00a0 b  ") == "a b"
    assert normalize_text("a\n\n\n\nb") == "a\n\nb"
    assert normalize_text("x\u200by") == "xy"           # 零宽字符清除
    assert normalize_text("中文　全角") == "中文 全角"


def test_collapse_text():
    assert collapse_text("a\n b\t\tc") == "a b c"


def test_content_hash_stable_and_distinct():
    a = make_content_hash("题干", ["A．1", "B．2"])
    b = make_content_hash("题干 ", ["A．1", "B．2"])   # 空白差异不影响
    c = make_content_hash("题干", ["A．1", "B．3"])
    assert a == b
    assert a != c
    assert len(a) == 32


def test_short_hash():
    assert len(short_hash("abc", 8)) == 8


def test_placeholder_protocol():
    text = "读图判断 {{img:1}} 正确的是 {{img:2}}"
    out = resolve_placeholders(text, {1: "x^{2}", 2: ""})
    assert "$x^{2}$" in out
    assert "{{img:2}}" in out        # 缺失的 LaTeX 保留原占位符，不丢信息


# ---------------------------------------------------------------------------
def test_selftest_end_to_end(tmp_path):
    """自检必须完全离线可跑：默认 Backbone 不通时自动回落 mock。"""
    cfg = load_config()
    cfg.as_dict()["models"]["default"] = "mock-offline"   # 测试环境强制离线
    from k12crawler.selftest import run_selftest

    report = run_selftest(cfg, backbone="mock-offline", out_dir=tmp_path)
    assert report["ok"], report.get("problems")
    steps = {s["step"]: s for s in report["steps"]}
    assert steps["PDF→题目"]["count"] >= 2
    assert steps["图像→题目"]["count"] >= 1
    assert steps["文本→题目"]["count"] >= 1
    assert steps["公式图→LaTeX"]["ok"] is True
    # 素材必须真的生成了
    for p in report["artifacts"]:
        assert Path(p).exists()
    # 落库回归
    assert report["storage_roundtrip"]["stored_total"] >= 1
