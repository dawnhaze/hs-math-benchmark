"""多个模型的评测结果合并成一张对比表（汇报里的主表）。

用法：
  python compare.py --results results --out results/leaderboard.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

BOARDS = ["函数", "导数及其应用", "数列", "立体几何", "解析几何", "概率与统计"]
DIFFS = ["基础", "中档", "压轴"]


def load_summaries(directory: Path) -> List[Dict[str, Any]]:
    out = []
    for p in sorted(directory.glob("summary_*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--out", default="results/leaderboard.md")
    args = ap.parse_args()

    summaries = load_summaries(Path(args.results))
    if not summaries:
        print("没有找到 summary_*.json")
        return 1
    rows = []
    for s in summaries:
        m = s.get("meta") or {}
        rows.append(
            {
                "model": (m.get("model") or "unknown") + (f" [{m.get('mode')}]" if m.get("mode") else ""),
                "provider": m.get("provider"),
                "mock": bool(m.get("mock")),
                "n": s["overall"]["n"],
                "acc": s["overall"]["accuracy"],
                "ci": s["overall"]["ci95"],
                "macro": s.get("macro_board_accuracy", 0),
                "invalid": s["overall"]["invalid_rate"],
                "board": {k: v["accuracy"] for k, v in (s.get("board") or {}).items()},
                "diff": {k: v["accuracy"] for k, v in (s.get("difficulty") or {}).items()},
            }
        )
    rows.sort(key=lambda r: -r["acc"])

    lines = ["# 模型对比（同一数据集、同一 Prompt）\n"]
    lines.append("| 排名 | 模型（测试方式） | 后端 | 题数 | 准确率 | 95% CI | 六板块宏平均 | 未抽取率 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        mark = "（mock，仅链路自检）" if r["mock"] else ""
        lines.append(
            f"| {i} | `{r['model']}`{mark} | {r['provider']} | {r['n']} | **{r['acc']:.2%}** | "
            f"[{r['ci'][0]:.2%}, {r['ci'][1]:.2%}] | {r['macro']:.2%} | {r['invalid']:.2%} |"
        )
    lines.append("")

    lines.append("## 分板块准确率\n")
    lines.append("| 模型 | " + " | ".join(BOARDS) + " |")
    lines.append("|---" * (len(BOARDS) + 1) + "|")
    for r in rows:
        cells = [f"{r['board'].get(b, 0):.1%}" if b in r["board"] else "—" for b in BOARDS]
        lines.append(f"| `{r['model']}` | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## 分难度准确率\n")
    lines.append("| 模型 | " + " | ".join(DIFFS) + " |")
    lines.append("|---" * (len(DIFFS) + 1) + "|")
    for r in rows:
        cells = [f"{r['diff'].get(d, 0):.1%}" if d in r["diff"] else "—" for d in DIFFS]
        lines.append(f"| `{r['model']}` | " + " | ".join(cells) + " |")
    lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:14]))
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
