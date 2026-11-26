"""Backbone 抽象、提示词渲染、多模态解析的离线测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from k12crawler.config import Config, DEFAULTS, load_config  # noqa: E402
from k12crawler.llm import ModelClient, build_client, parse_json_loose, prompts  # noqa: E402
from k12crawler.multimodal import _page_range  # noqa: E402
from k12crawler.multimodal.converter import _payload_to_questions  # noqa: E402


# ---------------------------------------------------------------------------
# 提示词渲染：花括号不能把模板搞崩（这是踩过的真实坑）
# ---------------------------------------------------------------------------
def test_prompt_render_keeps_json_braces():
    out = prompts.render(prompts.TEXT_TO_QUESTIONS, content="1+1=2")
    assert "1+1=2" in out
    assert '"questions"' in out          # JSON schema 必须原样保留
    assert "<<" not in out               # 占位符必须被替换干净


def test_prompt_render_classify():
    out = prompts.render(prompts.CLASSIFY_QUESTION, stem="求导", options="[]")
    assert "求导" in out and '"grade"' in out


# ---------------------------------------------------------------------------
# JSON 宽松解析
# ---------------------------------------------------------------------------
def test_parse_json_loose_variants():
    assert parse_json_loose('{"a": 1}')["a"] == 1
    assert parse_json_loose('```json\n{"a": 2}\n```')["a"] == 2
    assert parse_json_loose('好的，结果是：{"a": 3} 完毕')["a"] == 3
    assert parse_json_loose('[{"a": 4},]') [0]["a"] == 4     # 尾随逗号


def test_parse_json_loose_raises_on_garbage():
    with pytest.raises(ValueError):
        parse_json_loose("完全没有 JSON 的一段话")


# ---------------------------------------------------------------------------
# Backbone 解析与 mock 行为
# ---------------------------------------------------------------------------
def test_backbone_resolution_from_config():
    cfg = load_config()
    names = cfg.backbone_names()
    assert "qwen3-9b" in names          # 基准 Backbone 必须在
    assert cfg.get("models.default") == "qwen3-9b"
    spec = cfg.backbone("qwen3-9b")
    assert spec["model"] == "qwen3:9b"
    assert spec["vision"] is True


def test_backbone_unknown_raises():
    cfg = load_config()
    with pytest.raises(KeyError):
        cfg.backbone("no-such-backbone")


def test_api_key_env_missing_raises(monkeypatch):
    cfg = load_config()
    client = build_client(cfg, "gpt-4o-mini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from k12crawler.llm import BackboneError

    with pytest.raises(BackboneError):
        _ = client.api_key


def test_mock_backbone_produces_questions():
    cfg = load_config()
    client = build_client(cfg, "mock-offline")
    raw = client.chat_text(prompts.render(prompts.TEXT_TO_QUESTIONS, content="任意文本"))
    payload = parse_json_loose(raw)
    assert payload["questions"] and payload["questions"][0]["stem"]


def test_mock_backbone_formula_and_enrich():
    cfg = load_config()
    client = build_client(cfg, "mock-offline")
    latex = client.chat_image(b"fake", prompts.FORMULA_TO_LATEX, system=prompts.SYSTEM_FORMULA)
    assert latex.strip() and "$" not in latex
    enriched = parse_json_loose(client.chat_text(prompts.render(prompts.ENRICH_QUESTION, question="{}")))
    assert "clean_stem" in enriched


def test_mock_health_check_ok():
    cfg = load_config()
    h = build_client(cfg, "mock-offline").health_check()
    assert h["ok"] is True


def test_vision_disabled_backbone_rejects_image():
    """纯文本 Backbone 处理图像时必须给出可读的中文错误。"""
    spec = {"name": "txt", "provider": "mock", "model": "m", "base_url": "x",
            "api_key": "k", "vision": False}
    from k12crawler.llm import BackboneError

    with pytest.raises(BackboneError) as ei:
        ModelClient(spec).chat_image(b"x", "hi")
    assert "视觉" in str(ei.value)


# ---------------------------------------------------------------------------
# 多模态辅助函数
# ---------------------------------------------------------------------------
def test_page_range_parsing():
    assert _page_range("", 10) == list(range(10))
    assert _page_range("1-3", 10) == [0, 1, 2]
    assert _page_range("2,5", 10) == [1, 4]
    assert _page_range("8-99", 10) == [7, 8, 9]      # 越界自动收敛


def test_payload_to_questions_shapes():
    payload = {
        "questions": [
            {"stem": "题干 A", "options": ["A．1", "B．2"], "answer": "A",
             "qtype": "选择题", "subject": "初中数学", "tags": "二次函数,最值",
             "difficulty": 80},
        ]
    }
    qs = _payload_to_questions(payload, source="vlm", origin="t.pdf")
    assert len(qs) == 1
    q = qs[0]
    assert q.stem == "题干 A"
    assert q.tags == ["二次函数", "最值"]      # 字符串标签会被切开
    assert q.difficulty == pytest.approx(0.8)  # 百分制自动换算
    assert q.qid.startswith("vlm:") and q.content_hash


def test_payload_to_questions_skips_empty_stem():
    qs = _payload_to_questions({"questions": [{"stem": "  "}, {"stem": "ok"}]},
                               source="vlm", origin="x")
    assert len(qs) == 1


def test_payload_to_questions_options_dict():
    qs = _payload_to_questions(
        {"questions": [{"stem": "s", "options": {"A": "甲", "B": "乙"}}]},
        source="vlm", origin="x",
    )
    assert qs[0].options == ["A．甲", "B．乙"]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def test_config_defaults_merge():
    cfg = Config(DEFAULTS)
    assert cfg.get("crawler.concurrency") == 4
    assert cfg.get("crawler.empty_tolerance") == 3
    # 合成默认值缺失，Config 不应炸
    assert cfg.get("nothing.here", "d") == "d"


def test_config_paths_are_absolute():
    cfg = load_config()
    assert cfg.path_of("storage.sqlite_path").is_absolute()
    assert cfg.data_dir.is_absolute()
