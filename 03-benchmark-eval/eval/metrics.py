"""评测指标：准确率 + 95% 置信区间 + 多维切片。

为什么只报准确率也要给区间：
  1,000 道题上 1 个百分点的差距 ≈ 10 道题，模型间差 2 个点完全可能是抽样噪声。
  汇报时给出 Wilson 区间，比单点数字更站得住。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 主口径 6 板块（导数独立），与数据集 board 字段一致
BOARDS = ["函数", "导数及其应用", "数列", "立体几何", "解析几何", "概率与统计"]
DIFFICULTIES = ["基础", "中档", "压轴"]


def wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)


def bucket(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    correct = sum(1 for r in rows if r.get("correct"))
    lo, hi = wilson_ci(correct, n)
    invalid = sum(1 for r in rows if not r.get("pred"))
    errors = sum(1 for r in rows if r.get("error"))
    lat = [r["latency"] for r in rows if r.get("latency")]
    return {
        "n": n,
        "correct": correct,
        "accuracy": round(correct / n, 4) if n else 0.0,
        "ci95": [round(lo, 4), round(hi, 4)],
        "invalid": invalid,
        "invalid_rate": round(invalid / n, 4) if n else 0.0,
        "errors": errors,
        "error_rate": round(errors / n, 4) if n else 0.0,
        "avg_latency": round(sum(lat) / len(lat), 3) if lat else 0.0,
    }


def _group(rows: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[str(r.get(key) or "未知")].append(r)
    return {k: bucket(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


def summarize(rows: List[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"overall": bucket(rows)}
    for key in ("board", "sub_board", "difficulty", "subset", "source", "split", "year_bucket"):
        out[key] = _group(rows, key)

    # 板块 × 难度 矩阵
    matrix: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for b in BOARDS:
        matrix[b] = {}
        for d in DIFFICULTIES:
            sub = [r for r in rows if r.get("board") == b and r.get("difficulty") == d]
            if sub:
                matrix[b][d] = bucket(sub)
    out["board_x_difficulty"] = matrix

    # 宏观平均：六个板块等权，避免"函数题多"把总分带偏
    board_accs = [v["accuracy"] for k, v in out["board"].items() if k in BOARDS and v["n"] > 0]
    out["macro_board_accuracy"] = round(sum(board_accs) / len(board_accs), 4) if board_accs else 0.0
    out["answer_extraction"] = dict(Counter(r.get("extract_method") or "未抽取" for r in rows))
    gold = Counter(r.get("gold") for r in rows if r.get("gold"))
    if gold:
        letter, n = gold.most_common(1)[0]
        out["chance_baseline"] = {
            "random": 0.25,
            "most_common_letter": letter,
            "most_common_rate": round(n / len(rows), 4),
            "delta_over_random": round(n / len(rows) - 0.25, 4),
        }
    # token 与轮次行为（审计 P1-8 / P1-2）
    pt = sum(int(r.get("prompt_tokens") or 0) for r in rows)
    ct = sum(int(r.get("completion_tokens") or 0) for r in rows)
    out["usage"] = {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
        "per_item_tokens": round((pt + ct) / len(rows), 1) if rows else 0,
        "avg_calls": round(sum(int(r.get("calls") or 1) for r in rows) / len(rows), 2) if rows else 0,
        "truncated": sum(1 for r in rows if r.get("truncated")),
    }
    multi = [r for r in rows if int(r.get("rounds") or 1) > 1]
    if multi:
        changed = [r for r in multi if len({a for a in (r.get("stage_answers") or []) if a}) > 1]
        gained = sum(1 for r in changed if r.get("correct") and (r.get("stage_answers") or [""])[0] != r.get("gold"))
        lost = sum(1 for r in changed if not r.get("correct") and (r.get("stage_answers") or [""])[0] == r.get("gold"))
        out["stage_behavior"] = {
            "items": len(multi),
            "changed": len(changed),
            "changed_rate": round(len(changed) / len(multi), 4),
            "changed_to_correct": gained,
            "changed_to_wrong": lost,
        }
    out["option_distribution"] = dict(Counter(r.get("pred") or "?" for r in rows))
    if meta:
        out["meta"] = meta
    return out


def format_markdown(summary: Dict[str, Any], title: str = "评测结果") -> str:
    m = summary.get("meta") or {}
    o = summary["overall"]
    lines = [f"# {title}\n"]
    lines.append(f"- 模型：`{m.get('model', '')}`　后端：`{m.get('provider', '')}`")
    lines.append(f"- 数据集：`{m.get('dataset', '')}`　样本数：{o['n']}")
    mode_cn = {"normal": "常规测试", "deep": "深度思考测试"}.get(m.get("mode"), m.get("mode"))
    lines.append(f"- 测试方式：**{mode_cn}**（固定提示词 `{m.get('prompt', '')}`，共 {m.get('deep_rounds', 1)} 轮）")
    lines.append(f"- 解码：temperature={m.get('temperature')}，max_tokens={m.get('max_tokens')}")
    lines.append("")
    lines.append("## 总体\n")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 准确率 | **{o['accuracy']:.2%}** |")
    lines.append(f"| 95% 置信区间 | [{o['ci95'][0]:.2%}, {o['ci95'][1]:.2%}] |")
    lines.append(f"| 六板块宏平均 | {summary.get('macro_board_accuracy', 0):.2%} |")
    lines.append(f"| 未抽取到答案 | {o['invalid']}（{o['invalid_rate']:.2%}） |")
    lines.append(f"| 调用失败 | {o['errors']}（{o['error_rate']:.2%}） |")
    lines.append(f"| 平均单题耗时 | {o['avg_latency']:.2f}s |")
    cb = summary.get("chance_baseline") or {}
    if cb:
        lines.append(f"| 盲猜基线 | 随机 25.0%；全选 {cb['most_common_letter']} 为 {cb['most_common_rate']:.2%} |")
    u = summary.get("usage") or {}
    if u:
        lines.append(f"| Token 消耗 | 输入 {u['prompt_tokens']} / 输出 {u['completion_tokens']} / 每题 {u['per_item_tokens']} |")
        lines.append(f"| 平均调用次数 | {u['avg_calls']}（截断 {u['truncated']} 条） |")
    sb = summary.get("stage_behavior")
    if sb:
        lines.append(f"| 多轮改判 | {sb['changed']}/{sb['items']}（{sb['changed_rate']:.1%}），改对 {sb['changed_to_correct']}、改错 {sb['changed_to_wrong']} |")
    lines.append("")

    def table(name: str, key: str, order: Optional[Iterable[str]] = None) -> None:
        data = summary.get(key) or {}
        if not data:
            return
        lines.append(f"## {name}\n")
        lines.append("| 切片 | 题数 | 准确率 | 95% CI | 未抽取 |")
        lines.append("|---|---|---|---|---|")
        keys = list(order) if order else list(data.keys())
        for k in keys:
            if k not in data:
                continue
            v = data[k]
            lines.append(
                f"| {k} | {v['n']} | {v['accuracy']:.2%} | [{v['ci95'][0]:.2%}, {v['ci95'][1]:.2%}] | {v['invalid']} |"
            )
        lines.append("")

    table("按知识板块（主口径）", "board", BOARDS)
    table("按细分板块", "sub_board")
    table("按难度", "difficulty", DIFFICULTIES)
    table("按子集", "subset")
    table("按来源", "source")
    table("按年份", "year_bucket")

    mat = summary.get("board_x_difficulty") or {}
    if mat:
        lines.append("## 板块 × 难度矩阵\n")
        lines.append("| 板块 | 基础 | 中档 | 压轴 |")
        lines.append("|---|---|---|---|")
        for b in BOARDS:
            cells = []
            for d in DIFFICULTIES:
                v = mat.get(b, {}).get(d)
                cells.append(f"{v['accuracy']:.1%} (n={v['n']})" if v else "—")
            lines.append(f"| {b} | " + " | ".join(cells) + " |")
        lines.append("")

    ext = summary.get("answer_extraction") or {}
    if ext:
        lines.append("## 答案抽取方式\n")
        lines.append("| 方式 | 次数 |")
        lines.append("|---|---|")
        for k, v in sorted(ext.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {k} | {v} |")
        lines.append("")
    return "\n".join(lines)
