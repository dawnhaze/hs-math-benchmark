"""评测脚本自测：答案抽取、指标计算、结果导出字段。

运行：
  python -m pytest tests -q
（没有 pytest 时：python tests/test_eval.py）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import metrics  # noqa: E402
import prompts  # noqa: E402

CASES = [
    ("答案：B", "B", "答案标记"),
    ("推理……\n答案: C", "C", "答案标记"),
    ("所以 \\boxed{D}", "D", "boxed"),
    ("故选 A。", "A", "故选"),
    ("B", "B", "独立字母"),
    ("我觉得是 A 或 B，最后选 B", "B", "兜底扫描"),
    ("", "", "空输出"),
    # 审计 P0-3：正文里的选项字母不能被当成答案
    ("设 A={1,2}，B={3}，求 A∩B 的元素个数。由定义得 B 中元素不在 A 内，故答案为 0。", "", "未抽取"),
    ("比较 A、B、C、D 四个选项可知，", "", "未抽取"),
    ("A、B、C、D 四个选项都有可能，取决于参数。", "", "未抽取"),
    ("综上，选 C", "C", "故选"),
]


def test_extract() -> None:
    for text, want, _tag in CASES:
        got, _ = prompts.extract_answer(text)
        assert got == want, f"{text!r} -> {got}, want {want}"


def test_extract_option_text() -> None:
    opts = ["3 对", "4 对", "8 对", "16 对"]
    got, method = prompts.extract_answer("符合条件的共有 8 对", opts)
    assert got == "C" and method == "选项原文匹配", (got, method)


def test_bucket() -> None:
    rows = [
        {"correct": True, "pred": "A", "latency": 1.0},
        {"correct": False, "pred": "B", "latency": 1.0},
        {"correct": False, "pred": "", "latency": 1.0},
    ]
    b = metrics.bucket(rows)
    assert b["n"] == 3 and b["correct"] == 1
    assert abs(b["accuracy"] - 1 / 3) < 1e-3
    assert b["invalid"] == 1
    lo, hi = b["ci95"]
    assert lo < b["accuracy"] < hi


def test_matrix() -> None:
    rows = [
        {"board": "函数", "difficulty": "基础", "correct": True, "pred": "A"},
        {"board": "函数", "difficulty": "基础", "correct": False, "pred": "B"},
        {"board": "数列", "difficulty": "压轴", "correct": True, "pred": "C"},
    ]
    s = metrics.summarize(rows)
    assert s["overall"]["n"] == 3
    assert s["board_x_difficulty"]["函数"]["基础"]["accuracy"] == 0.5
    assert s["board_x_difficulty"]["数列"]["压轴"]["accuracy"] == 1.0
    assert "函数" in s["board"]


def test_two_modes() -> None:
    item = {"stem": "已知函数 f(x)=x^2-2x，求最小值", "options": ["-1", "0", "1", "2"], "answer": "A"}
    sys_n, user_n = prompts.build_messages(item, "normal")
    sys_d, user_d = prompts.build_messages(item, "deep")
    assert "两种" not in sys_n and "两种" in sys_d
    assert "答案：X" in sys_n and "答案：X" in sys_d
    # 复核轮不得暴露上一轮答案（审计 P1-2：否则是锚定而不是独立复核）
    sys_v, user_v = prompts.build_verify_messages(item, "C")
    assert "上一轮结论" not in user_v
    assert "C" not in user_v.replace("A.", "").replace("B.", "").replace("C.", "").replace("D.", "")
    assert prompts.default_max_tokens("deep") > prompts.default_max_tokens("normal")


def test_prompt_hides_metadata() -> None:
    item = {"stem": "已知 x>0，求最小值", "options": ["1", "2", "3", "4"], "answer": "B"}
    system, user = prompts.build_messages(item)
    assert "答案" in system
    assert "B" not in user.split("A.")[0]
    assert "A. 1" in user and "D. 4" in user


def _run_with(script):
    """用脚本化的假 provider 跑 run_one，验证截断与轮次失败处理。"""
    import evaluate
    from providers import Completion

    class Stub:
        name = "stub"

        def __init__(self, seq):
            self.seq = list(seq)
            self.i = 0

        def complete(self, system, user, *, temperature, max_tokens):
            item = self.seq[min(self.i, len(self.seq) - 1)]
            self.i += 1
            return Completion(**item)

    item = {"id": "t", "stem": "求 1+1", "options": ["1", "2", "3", "4"], "answer": "B",
            "board": "函数", "difficulty": "基础", "subset": "core", "source": "s", "split": "test"}
    cfg = {"provider": {"mock_accuracy": 0.6}, "run": {"seed": 0}}
    return evaluate.run_one(item, Stub(script), cfg, mode="deep", rounds=2,
                            temperature=0.0, max_tokens=64, retries=1)


def test_truncated_not_counted() -> None:
    row = _run_with([
        {"text": "比较 A、B、C、D 四个选项可知", "finish_reason": "length"},
        {"text": "复核后仍无法确定", "finish_reason": "stop"},
    ])
    assert row["pred"] == "" and row["truncated"] is True


def test_final_stage_failure_falls_back() -> None:
    row = _run_with([
        {"text": "计算得 2。\n答案：B", "finish_reason": "stop"},
        {"text": "", "error": "HTTP 500", "finish_reason": ""},
    ])
    assert row["pred"] == "B" and row["correct"] is True
    assert row["fallback_stage"] == 1 and row["error"] == ""


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
