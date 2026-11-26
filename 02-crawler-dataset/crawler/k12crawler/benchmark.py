"""Benchmark 数据集构建：标注规范 + 可判定性过滤 + 分层抽样 + 统计导出。

把"爬回来的原始题目"变成"能拿去评测的 Benchmark 条目"。

范围（按第一次汇报的修改意见收敛）：
  · 只做**单项选择题**（四选一）——课程要求里"选择题即可"，且客观题能全自动判分；
  · 评测指标只有**解题准确率**，不做过程得分、不做人工复核。

三条标注轴（规则化、可复现）：

  1. 知识板块  board      —— 9 个板块，加权关键词投票
  2. 难度      difficulty —— 基础 / 中档 / 压轴，先算连续分值，再按分位切三层
  3. 答案类型  answer_type—— 选项字母 / 其他（用于质检，不参与评测）

难度是"规则初标"：作用是把数据分层，让评测能按难度切片。
真正的 item-level 难度留到期中用多模型作答正确率校准（答对率越低 → 越难）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .utils import ensure_dir

# ---------------------------------------------------------------------------
# 零、数据清洗：GBK 混编导致的经典乱码
#   本站个别页面把 △ 退化成 D、∠ 退化成 Ð（如 "在不等边DABC中…则ÐA的取值范围"）。
#   这不是解析错误，而是源页面编码混编，必须在建模前修回来。
# ---------------------------------------------------------------------------
_MOJIBAKE = (
    (re.compile(r"(?<![A-Za-z])DABC(?![A-Za-z])"), "△ABC"),
    (re.compile(r"(?<![A-Za-z])DAB(?![A-Za-z])"), "△AB"),
    (re.compile(r"Ð"), "∠"),
    (re.compile(r"°"), "°"),
)


def repair_mojibake(text: str) -> str:
    if not text:
        return text
    for pat, rep in _MOJIBAKE:
        text = pat.sub(rep, text)
    return text


# ---------------------------------------------------------------------------
# 一、知识板块：加权关键词投票
# ---------------------------------------------------------------------------
BOARD_SPEC: List[Tuple[str, str, List[Tuple[str, int]]]] = [
    (
        "导数及其应用",
        "导数运算、切线、单调性、极值与最值、恒成立与零点",
        [("导数", 4), ("导函数", 4), ("f′", 4), ("f'(", 3), ("求导", 4),
         ("切线", 3), ("极大值", 3), ("极小值", 3), ("单调递增区间", 4),
         ("恒成立", 3), ("极值", 3)],
    ),
    (
        "解析几何",
        "直线与圆、椭圆、双曲线、抛物线、离心率与位置关系",
        [("椭圆", 5), ("双曲线", 5), ("抛物线", 5), ("离心率", 5), ("准线", 4),
         ("渐近线", 5), ("焦点", 4), ("垂直平分线", 4), ("圆的方程", 4),
         ("直线与圆", 5), ("弦长", 3), ("中点弦", 4), ("轨迹方程", 3),
         ("斜率", 2), ("截距", 2), ("直线", 2), ("圆心", 3), ("半径", 1)],
    ),
    (
        "立体几何",
        "空间几何体、线面关系、角与距离、体积表面积、三视图",
        [("棱柱", 5), ("棱锥", 5), ("棱台", 5), ("圆台", 5), ("正方体", 5),
         ("长方体", 5), ("二面角", 5), ("线面角", 5), ("三视图", 5), ("外接球", 5),
         ("异面直线", 4), ("母线", 4), ("侧面积", 3), ("体积", 2),
         ("表面积", 3), ("射影", 4), ("多面体", 4), ("圆锥", 4),
         ("截面", 3), ("空间四边形", 4), ("棱", 2), ("平面", 1)],
    ),
    (
        "概率与统计",
        "随机事件概率、随机变量分布、期望方差、统计推断",
        [("概率", 3), ("随机变量", 5), ("分布列", 5), ("期望", 3), ("方差", 3),
         ("正态分布", 5), ("频率分布", 5), ("独立性检验", 5), ("回归方程", 5),
         ("抽样", 3), ("中位数", 2), ("众数", 2), ("平均数", 2), ("标准差", 3),
         ("相关关系", 4), ("相关系数", 4), ("散点图", 5),
         ("预报变量", 5), ("解释变量", 5)],
    ),
    (
        "计数原理",
        "分类加法与分步乘法、排列组合、二项式定理",
        [("排列", 3), ("组合", 2), ("二项式", 5), ("展开式", 4), ("取法", 4),
         ("种数", 4), ("不同的排法", 5), ("不同的分配", 5), ("不同的选法", 5),
         ("不同的取法", 5), ("任取", 3), ("方案", 2), ("编组", 4),
         ("排法", 3), ("多少种", 3), ("共有多少", 2)],
    ),
    (
        "数列",
        "等差等比数列、通项与求和、递推关系",
        [("数列", 5), ("等差数列", 5), ("等比数列", 5), ("通项", 4),
         ("前n项和", 5), ("递推", 4), ("公差", 4), ("公比", 4)],
    ),
    (
        "三角函数",
        "三角函数图象与性质、恒等变换、解三角形",
        [("三角函数", 5), ("正弦", 4), ("余弦", 4), ("正切", 4),
         ("解三角形", 5), ("弧度", 3), ("最小正周期", 5), ("图象变换", 4),
         ("△ABC", 4), ("ΔABC", 4), ("∠A", 3), ("三角形", 2), ("最大边", 3),
         ("sin", 2), ("cos", 2), ("tan", 2), ("锐角", 2), ("钝角", 2)],
    ),
    (
        "集合与常用逻辑",
        "集合运算、命题与量词、充要条件",
        [("集合", 5), ("子集", 5), ("交集", 5), ("并集", 5), ("补集", 5),
         ("充要条件", 5), ("充分不必要", 5), ("必要不充分", 5),
         ("全称量词", 5), ("存在量词", 5), ("命题", 2), ("∁", 4)],
    ),
    (
        "函数",
        "函数概念与性质、指对幂函数、函数图象与零点",
        [("函数", 3), ("定义域", 5), ("值域", 5), ("单调性", 3), ("奇函数", 5),
         ("偶函数", 5), ("周期性", 4), ("指数函数", 5), ("对数函数", 5),
         ("幂函数", 5), ("零点", 3), ("f(x)", 2), ("图象", 2),
         ("反函数", 4), ("大小关系", 2)],
    ),
    (
        "其他",
        "平面向量、复数、不等式、算法、数论与推理证明",
        [("向量", 3), ("复数", 5), ("虚部", 5), ("不等式", 3), ("程序框图", 5),
         ("算法", 3), ("条件语句", 5), ("循环语句", 5), ("数学归纳法", 5),
         ("反证法", 3), ("虚数单位", 5), ("合情推理", 4), ("归纳推理", 4),
         ("类比推理", 4), ("最大公约数", 5), ("最小公倍数", 5),
         ("平形四边形", 4), ("平行四边形", 3), ("数集", 2)],
    ),
]

BOARD_NAMES = [b[0] for b in BOARD_SPEC]

# 难度先验：板块本身有难度基线（集合 < 函数 < 立体/解析几何 < 导数）
BOARD_PRIOR = {
    "集合与常用逻辑": 0,
    "其他": 1,
    "函数": 1,
    "计数原理": 2,
    "数列": 2,
    "三角函数": 2,
    "概率与统计": 2,
    "立体几何": 3,
    "解析几何": 3,
    "导数及其应用": 4,
}

# ---------------------------------------------------------------------------
# 二、题型：本次只收单项选择题
# ---------------------------------------------------------------------------
_QTYPE_CHOICE = ("单选", "多选", "选择")


def is_choice(raw_qtype: str, options: List[str]) -> bool:
    raw = (raw_qtype or "").strip()
    if any(k in raw for k in _QTYPE_CHOICE):
        return True
    return len(options) >= 4 and not raw


# ---------------------------------------------------------------------------
# 三、难度打分（连续分值 -> 分位切层）
# ---------------------------------------------------------------------------
_HARD_KW = ("证明", "讨论", "恒成立", "取值范围", "存在", "任意",
            "最小值", "最大值", "最值", "单调区间", "零点个数", "至少有", "至多有")


def score_difficulty(
    stem: str,
    options: List[str],
    analysis: str,
    n_images: int,
    board: str,
) -> int:
    """难度连续分值。每个加分项都可解释，便于答辩时逐条说明。"""
    s = BOARD_PRIOR.get(board, 1)
    n = len(stem or "")
    if n > 60:
        s += 1
    if n > 120:
        s += 2
    if n > 220:
        s += 2
    s += min(3, sum(1 for k in _HARD_KW if k in (stem or "")))
    if n_images:
        s += 1
    if options:
        avg = sum(len(o) for o in options) / len(options)
        if avg > 20:
            s += 1
        if avg > 40:
            s += 2
    if analysis:
        s += min(2, len(analysis) // 300)
    return s


def _quantile(sorted_vals: List[int], q: float) -> int:
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def assign_difficulty(scores: List[int]) -> Tuple[int, int]:
    """按分位切三层，返回 (p40, p80) 两个切点。

    用分位而不是固定阈值，是为了让"基础/中档/压轴"的比例稳定在 4:4:2，
    不会因为某次抓取的题目偏简单就整体塌成"基础"。
    """
    vals = sorted(scores)
    return _quantile(vals, 0.4), _quantile(vals, 0.8)


def label_of(score: int, p40: int, p80: int) -> str:
    if score <= p40:
        return "基础"
    if score <= p80:
        return "中档"
    return "压轴"


# ---------------------------------------------------------------------------
# 四、答案类型（质检用）
# ---------------------------------------------------------------------------
def classify_answer(answer: str) -> str:
    a = (answer or "").strip()
    if re.fullmatch(r"[A-D]{1,4}", re.sub(r"[\s,，、]", "", a)):
        return "选项字母"
    if re.fullmatch(r"-?\d+(\.\d+)?", a):
        return "数值"
    if len(a) <= 40:
        return "表达式"
    return "文本"


# ---------------------------------------------------------------------------
# 五、板块分类
# ---------------------------------------------------------------------------
def classify_board(stem: str, options: Iterable[str], analysis: str = "") -> Tuple[str, Dict[str, int]]:
    """加权关键词投票，返回 (板块, 各板块得分)。同分时按 BOARD_NAMES 顺序靠前者优先。"""
    text = " ".join([stem or "", " ".join(options or []), (analysis or "")[:300]])
    scores: Dict[str, int] = {}
    for name, _desc, kws in BOARD_SPEC:
        hits = sum(w for kw, w in kws if kw in text)
        if hits:
            scores[name] = hits
    if not scores:
        return "未分类", {}
    best = max(scores.items(), key=lambda kv: (kv[1], -BOARD_NAMES.index(kv[0])))
    return best[0], scores


# ---------------------------------------------------------------------------
# 六、可判定性过滤（能不能拿去自动判分）
# ---------------------------------------------------------------------------
_OFFTOPIC = ("阿伏加德罗", "摩尔质量", "化学方程式", "电磁感应", "电流强度")


def judge_ready(stem: str, options: List[str], answer: str) -> Tuple[bool, str]:
    stem = (stem or "").strip()
    answer = (answer or "").strip()
    if any(k in stem for k in _OFFTOPIC):
        return False, "非数学内容"
    if len(stem) < 8:
        return False, "题干过短"
    if not answer:
        return False, "缺答案"
    letters = re.sub(r"[\s,，、]", "", answer)
    if not re.fullmatch(r"[A-D]", letters):
        return False, "答案不是单个选项字母"
    if len(options) < 4:
        return False, "选项不足四个"
    if letters not in "ABCD"[: len(options)]:
        return False, "答案超出选项范围"
    if stem.count("{{img:") > 4:
        return False, "题干图片过多"
    return True, ""


# ---------------------------------------------------------------------------
# 七、构建
# ---------------------------------------------------------------------------
def _load_rows(db_path: Path, subject_code: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM questions WHERE subject_code = ? ORDER BY qid", (subject_code,)
        ).fetchall()
    finally:
        conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for key, fallback in (("options", []), ("images", []), ("tags", []), ("extra", {})):
            raw = d.pop(f"{key}_json", None)
            try:
                d[key] = json.loads(raw) if raw else fallback
            except json.JSONDecodeError:
                d[key] = fallback
        for f in ("stem", "answer", "analysis"):
            d[f] = repair_mojibake(d.get(f) or "")
        d["options"] = [repair_mojibake(o) for o in d["options"]]
        out.append(d)
    return out


def _stratified_cap(rows: List[Dict[str, Any]], target: int) -> List[Dict[str, Any]]:
    """按板块分层抽样，板块内按 基础/中档/压轴 轮转取，保证难度不塌缩。"""
    if target <= 0 or len(rows) <= target:
        return rows
    by_board: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_board[r["board"]].append(r)

    total = len(rows)
    quota = {b: max(1, round(len(v) / total * target)) for b, v in by_board.items()}
    while sum(quota.values()) > target:
        b = max(quota, key=lambda k: quota[k])
        quota[b] -= 1
    while sum(quota.values()) < target:
        b = max(by_board, key=lambda k: len(by_board[k]) - quota[k])
        if len(by_board[b]) <= quota[b]:
            break
        quota[b] += 1

    picked: List[Dict[str, Any]] = []
    for b, items in by_board.items():
        by_diff: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for it in items:
            by_diff[it["difficulty"]].append(it)
        order = ["基础", "中档", "压轴"]
        idx = {k: 0 for k in order}
        got = 0
        while got < quota[b]:
            progressed = False
            for d in order:
                pool = by_diff.get(d, [])
                if idx[d] < len(pool):
                    picked.append(pool[idx[d]])
                    idx[d] += 1
                    got += 1
                    progressed = True
                    if got >= quota[b]:
                        break
            if not progressed:
                break
    return picked


def build(
    db_path: str | Path,
    out_dir: str | Path,
    subject_code: str = "gzsx",
    target: int = 1400,
) -> Dict[str, Any]:
    """构建 Benchmark 数据集，写出 jsonl + stats.json + spec.json。"""
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    ensure_dir(out_dir)

    raw_rows = _load_rows(db_path, subject_code)
    rejected: Counter = Counter()
    kept: List[Dict[str, Any]] = []

    for row in raw_rows:
        options = row.get("options") or []
        if not is_choice(row.get("qtype", ""), options):
            rejected["非选择题"] += 1
            continue
        ok, reason = judge_ready(row.get("stem", ""), options, row.get("answer", ""))
        if not ok:
            rejected[reason] += 1
            continue
        board, _ = classify_board(row["stem"], options, row.get("analysis", ""))
        row["board"] = board
        row["answer_type"] = classify_answer(row["answer"])
        row["_score"] = score_difficulty(
            row["stem"], options, row.get("analysis", ""), len(row.get("images") or []), board
        )
        kept.append(row)

    # 题干级去重：content_hash 已做过一遍（题干+选项），但选项排版差异会让
    # 同一道题漏网。评测算分时同一题重复出现会抬高分数，这里按题干预归一化再收一次。
    seen_stems = set()
    deduped: List[Dict[str, Any]] = []
    for r in kept:
        key = re.sub(r"\s+", "", r["stem"] or "")
        if key in seen_stems:
            rejected["题干重复"] += 1
            continue
        seen_stems.add(key)
        deduped.append(r)
    kept = deduped

    # 难度分层：按分值排名切 4:4:2。
    # 用排名而不是固定阈值，是因为分值大量并列（整数分），阈值切会把比例带偏。
    order = sorted(range(len(kept)), key=lambda i: (kept[i]["_score"], len(kept[i]["stem"] or "")))
    n = len(order)
    for rank, i in enumerate(order):
        kept[i]["difficulty"] = "基础" if rank < 0.4 * n else ("中档" if rank < 0.8 * n else "压轴")
    p40 = kept[order[min(n - 1, int(0.4 * n))]]["_score"] if n else 0
    p80 = kept[order[min(n - 1, int(0.8 * n))]]["_score"] if n else 0

    sampled = _stratified_cap(kept, target)
    sampled.sort(key=lambda r: (BOARD_NAMES.index(r["board"]) if r["board"] in BOARD_NAMES else 99,
                                ["基础", "中档", "压轴"].index(r["difficulty"]), r["qid"]))

    jsonl_path = out_dir / "benchmark.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in sampled:
            item = {
                "id": r["qid"],
                "source": r["source"],
                "source_url": r["source_url"],
                "subject": r["subject"],
                "board": r["board"],
                "qtype": "选择题",
                "difficulty": r["difficulty"],
                "difficulty_score": r["_score"],
                "answer_type": r["answer_type"],
                "stem": r["stem"],
                "options": r["options"],
                "answer": re.sub(r"[\s,，、]", "", r["answer"]),
                "analysis": r["analysis"],
                "images": [{"idx": i.get("idx"), "url": i.get("url"), "latex": i.get("latex", "")}
                           for i in r["images"]],
                "has_formula_image": "{{img:" in (r["stem"] or ""),
                "subset": "core" if "{{img:" not in (r["stem"] or "") else "extended",
                "stem_len": len(r["stem"] or ""),
            }
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _crosstab(a: str, b: str) -> Dict[str, Dict[str, int]]:
        table: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in sampled:
            table[r[a]][r[b]] += 1
        return {k: dict(v) for k, v in sorted(table.items(), key=lambda kv: -sum(kv[1].values()))}

    stats = {
        "subject_code": subject_code,
        "raw_total": len(raw_rows),
        "kept": len(kept),
        "sampled": len(sampled),
        "target": target,
        "rejected": dict(rejected),
        "difficulty_cuts": {"p40": p40, "p80": p80},
        "board": dict(Counter(r["board"] for r in sampled).most_common()),
        "difficulty": dict(Counter(r["difficulty"] for r in sampled).most_common()),
        "answer_type": dict(Counter(r["answer_type"] for r in sampled).most_common()),
        "subset": dict(Counter("core" if "{{img:" not in (r["stem"] or "") else "extended"
                               for r in sampled).most_common()),
        "board_x_difficulty": _crosstab("board", "difficulty"),
        "with_analysis": sum(1 for r in sampled if (r["analysis"] or "").strip()),
        "jsonl": str(jsonl_path),
    }

    (out_dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "spec.json").write_text(
        json.dumps(
            {
                "boards": [{"name": n, "desc": d, "keywords": [k for k, _ in kws]}
                           for n, d, kws in BOARD_SPEC],
                "difficulties": ["基础", "中档", "压轴"],
                "difficulty_rule": "连续分值按分位切分：≤P40 基础，≤P80 中档，其余压轴",
                "answer_types": ["选项字母", "数值", "表达式", "文本"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return stats
