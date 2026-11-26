"""人工抽检结果核对：算难度一致率、Cohen's kappa 与可解率。

用法：
  # 1) 人工填 review_sample.csv 的两列：人工难度、是否可解
  # 2) 运行
  python check_review.py --csv data/benchmark_v3/review_sample.csv

输出：
  review_report.md / review_report.json
  控制台给出：已填条数、一致率、kappa、不可解条数与编号。
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

LABELS = ["基础", "中档", "压轴"]


def kappa(pairs: list) -> float:
    n = len(pairs)
    if not n:
        return 0.0
    po = sum(1 for a, b in pairs if a == b) / n
    ca = Counter(a for a, _ in pairs)
    cb = Counter(b for _, b in pairs)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    return 0.0 if pe == 1 else (po - pe) / (1 - pe)


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    cands = [here / "dataset" / "review_sample.csv", here.parent / "dataset" / "review_sample.csv",
             Path("data/benchmark_v3/review_sample.csv")]
    default_csv = next((str(c) for c in cands if c.exists()), "data/benchmark_v3/review_sample.csv")
    ap.add_argument("--csv", default=default_csv)
    out_dir = Path(default_csv).resolve().parent
    ap.add_argument("--out-md", default=str(out_dir / "review_report.md"))
    ap.add_argument("--out-json", default=str(out_dir / "review_report.json"))
    args = ap.parse_args()

    rows = list(csv.DictReader(Path(args.csv).open(encoding="utf-8-sig")))
    pairs = []
    unsolvable, invalid_label = [], []
    for r in rows:
        human = (r.get("人工难度(基础/中档/压轴)") or "").strip()
        machine = (r.get("difficulty_rule") or "").strip()
        ok = (r.get("是否可解(是/否)") or "").strip()
        if human in LABELS:
            pairs.append((machine, human))
        elif human:
            invalid_label.append((r["id"], human))
        if ok == "否":
            unsolvable.append(r["id"])

    filled = len(pairs)
    n = len(rows)
    po = sum(1 for a, b in pairs if a == b) / filled if filled else 0.0
    k = kappa(pairs)
    confusion = Counter(f"{a}->{b}" for a, b in pairs)

    report = {
        "sample_size": n,
        "difficulty_filled": filled,
        "filled_rate": round(filled / n, 4) if n else 0,
        "agreement": round(po, 4),
        "cohen_kappa": round(k, 4),
        "unsolvable": len(unsolvable),
        "unsolvable_ids": unsolvable[:20],
        "invalid_labels": invalid_label[:10],
        "confusion": dict(confusion),
    }
    Path(args.out_json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 人工抽检结果",
        "",
        f"- 抽样 {n} 条，已填难度 {filled} 条（{report['filled_rate']:.1%}）",
        f"- 规则难度与人工判定一致率：**{po:.1%}**",
        f"- Cohen's kappa：**{k:.3f}**（<0.4 较弱，0.4–0.6 中等，>0.6 较好）",
        f"- 人工判定不可解：{len(unsolvable)} 条",
        "",
        "## 混淆情况（规则 -> 人工）",
        "",
        "| 规则 → 人工 | 条数 |",
        "|---|---|",
    ]
    for key, cnt in sorted(confusion.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {key} | {cnt} |")
    if unsolvable:
        lines += ["", "## 不可解条目", ""] + [f"- `{i}`" for i in unsolvable]
    if len(pairs) < n * 0.5:
        lines += ["", "> 提示：已填不足一半，统计意义有限，建议至少标 100 条再引用 kappa。"]
    Path(args.out_md).write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
