"""固定提示词与答案解析。

两种测试方式，**提示词写死**，不通过配置拼接，保证同一模型两次评测可比：

  normal  —— 常规测试：一次性作答，允许简要说明，最后一行给答案。
  deep    —— 深度思考测试：先按固定要求做多方法求解与验算，
              再进入复核轮（固定复核提示词），独立重算、纠错，最后给答案。

复核轮数由 `--deep-rounds` 控制（默认 2 = 首答 + 1 轮复核）。
每轮都必须单独输出一行「答案：X」，判分取最后一轮的答案。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 一、固定提示词
# ---------------------------------------------------------------------------
NORMAL_SYSTEM = (
    "你是一名高中数学解题助手。题目为四选一单项选择题，只有一个正确选项。"
    "请独立解题，不要依赖记忆中的原题答案。"
    "最后必须单独输出一行「答案：X」，X 为 A、B、C、D 中的一个。"
)

NORMAL_USER = """{stem}

{options}

请作答。可以简要说明理由，最后单独输出一行「答案：X」。"""

DEEP_SYSTEM = (
    "你是一名严谨的高中数学解题专家。你要对题目做深入、反复的思考，不要凭直觉作答：\n"
    "1. 先把已知条件、所求目标、隐含限制逐条列清楚，标出容易看错的地方；\n"
    "2. 至少用两种不同方法求解（例如代数推导与几何直观、通法与特殊值代入），比较两条路径的结果；\n"
    "3. 把候选答案回代原题或取特殊值验算，检查定义域、取值范围、边界情况；\n"
    "4. 若两种方法结论不一致，必须定位是哪一步出错并重算，不能含糊带过；\n"
    "5. 最后用一句话确认结论，并单独输出一行「答案：X」，X 为 A、B、C、D 中的一个。"
)

DEEP_USER = """{stem}

{options}

请按上述要求深入思考后作答，最后单独输出一行「答案：X」。"""

VERIFY_SYSTEM = (
    "你是一名复核老师。请把这道题当成全新的题，独立从头求解一遍，不要参考任何既有结论。"
    "重点检查：条件是否漏读、符号与端点是否处理正确、特殊情况是否成立。"
    "最后单独输出一行「答案：X」，X 为 A、B、C、D 中的一个。"
)

# 注意：复核轮**不**把上一轮答案写进提示词。
# 审计发现旧版把「上一轮结论：X」喂回去，模型倾向沿用，提升主要来自锚定而非独立验证。
VERIFY_USER = """题目：
{stem}

选项：
{options}

请独立求解并自查，然后给出你确认的最终答案。
最后单独输出一行「答案：X」。"""

# 兼容旧参数名：zero_shot ≈ normal，cot ≈ normal（带简短推理）
TEMPLATES = {"zero_shot": NORMAL_USER, "cot": NORMAL_USER}
MODES = ("normal", "deep")


def format_options(options: List[str]) -> str:
    return "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(options))


def build_messages(item: Dict, mode: str = "normal") -> Tuple[str, str]:
    """返回首轮 (system, user)。mode 只接受 normal / deep。"""
    opts = format_options(item["options"])
    stem = item["stem"].strip()
    if mode == "deep":
        return DEEP_SYSTEM, DEEP_USER.format(stem=stem, options=opts)
    return NORMAL_SYSTEM, NORMAL_USER.format(stem=stem, options=opts)


def build_verify_messages(item: Dict, previous_answer: str) -> Tuple[str, str]:
    """复核轮：给定上一轮结论，要求独立重算。"""
    opts = format_options(item["options"])
    stem = item["stem"].strip()
    return VERIFY_SYSTEM, VERIFY_USER.format(
        stem=stem, options=opts, previous=previous_answer or "（上一轮未给出明确选项）"
    )


def default_max_tokens(mode: str) -> int:
    return 1024 if mode == "deep" else 256


# ---------------------------------------------------------------------------
# 二、答案抽取：从自由文本里捞出唯一选项
# ---------------------------------------------------------------------------
_PATTERNS = [
    (re.compile(r"答案\s*(?:是|为)?\s*[:：]?\s*[*_`]*\s*([A-D])(?![A-Za-z])"), "答案标记"),
    (re.compile(r"\\boxed\{\s*([A-D])\s*\}"), "boxed"),
    (re.compile(r"(?:正确选项|应选|故选|选择|选)\s*[:：]?\s*([A-D])(?![A-Za-z])"), "故选"),
    (re.compile(r"^\s*([A-D])\s*[.、．]?\s*$", re.M), "独立字母"),
]


def extract_answer(text: str, options: Optional[List[str]] = None) -> Tuple[str, str]:
    """返回 (选项字母, 抽取方式)；抽不到返回 ("", "未抽取")。"""
    if not text:
        return "", "空输出"
    t = text.strip()
    for pat, tag in _PATTERNS:
        hits = pat.findall(t)
        if hits:
            return hits[-1].upper(), tag
    # 只接受"末行明确指认"的情形：整行很短且只有一个选项字母。
    # 不再全文扫 A–D：本数据集大量题干用 A、B、C 命名集合/点/事件，
    # 全文扫描会把正文里的字母当答案（审计 P0-3），拒答与截断会被伪装成有答案。
    tail_lines = [ln.strip() for ln in t.splitlines() if ln.strip()][-2:]
    for line in reversed(tail_lines):
        if len(line) <= 24:
            m = re.fullmatch(r"[（(]?\s*(?:选|是|为)?\s*([A-D])\s*[）)]?\s*[。.]?", line)
            if m:
                return m.group(1).upper(), "末行字母"
            m2 = re.fullmatch(r"([A-D])\s*[。.]?\s*(?:项)?", line)
            if m2:
                return m2.group(1).upper(), "末行字母"
    if options:
        norm = re.sub(r"\s+", "", t)
        keys = sorted(
            ((re.sub(r"\s+", "", o), i) for i, o in enumerate(options)),
            key=lambda kv: -len(kv[0]),
        )
        for key, i in keys:
            if len(key) >= 2 and key in norm:
                return chr(65 + i), "选项原文匹配"
    return "", "未抽取"
