"""端到端自检：合成一份试卷 -> 走多模态链路 -> 校验输出规范。

存在的意义：
  * 判分/验收时，即使机器上没有本地 VLM、也没有任何 API Key，
    也能一条命令验证"PDF/图像/文本 → 题目"的整条链路是否贯通；
  * CI 里可以直接当回归测试用；
  * 顺手生成一份可复现的样例试卷，方便人工核对效果。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .llm import BackboneError
from .multimodal import QuestionConverter
from .utils import ensure_dir, now_iso

log = logging.getLogger("k12.selftest")

# 合成试卷内容（真实 K12 题目，覆盖 语文式填空 / 数学公式 / 英语选择）
PAGE1_LINES = [
    "K12 综合练习（自检用合成试卷）",
    "一、填空题",
    "1．已知函数 f(x)=x^2-2x+3，则 f(x) 的最小值是 ______．",
    "2．在标准大气压下，要使水的温度高于 100℃，可以采取的措施是 ______．",
    "二、选择题",
    "3．下列物理量中属于矢量的是（　　）",
    "   A．功      B．电势      C．电势能      D．电场强度",
]
PAGE2_LINES = [
    "三、按要求完成下列各题",
    "4．用方框中所给单词的适当形式填空，每词限用一次。",
    "   busy   swim   hardly   three   ourselves",
    "   ① Father's Day is on the ______ Sunday in June．",
    "   ② The traffic during the rush hour is much ______ than usual．",
    "5．区域地理：读图判断，下列说法正确的是（　　）",
    "   A．甲地年降水量大于乙地   B．甲地气温年较差小于乙地",
]


def _write_pdf(path: Path) -> Path:
    """用 PyMuPDF 合成一份两页的"试卷"。"""
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover
        import fitz  # type: ignore

    ensure_dir(path.parent)
    doc = fitz.open()
    for lines in (PAGE1_LINES, PAGE2_LINES):
        page = doc.new_page(width=595, height=842)  # A4
        y = 70.0
        for i, line in enumerate(lines):
            size = 16 if i == 0 else 12
            try:
                page.insert_text(
                    (56, y), line, fontsize=size, fontname="china-s", color=(0, 0, 0)
                )
            except Exception:  # 字体缺失时退化为内置英文字体
                page.insert_text((56, y), line.encode("ascii", "ignore").decode() or "-",
                                 fontsize=size)
            y += size + 12
    doc.save(str(path))
    doc.close()
    return path


def _write_image(path: Path, lines: List[str]) -> Path:
    """用 Pillow 合成一张题目截图。"""
    from PIL import Image, ImageDraw, ImageFont

    ensure_dir(path.parent)
    font = None
    for cand in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(cand).exists():
            try:
                font = ImageFont.truetype(cand, 22)
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()

    width, height = 1000, 60 + 40 * len(lines)
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    y = 20
    for line in lines:
        d.text((20, y), line, fill="black", font=font)
        y += 40
    img.save(path)
    return path


def _validate(questions: List[Any], label: str) -> Dict[str, Any]:
    """校验解析结果是否符合 Question 规范。"""
    problems: List[str] = []
    for i, q in enumerate(questions, 1):
        tag = f"{label}#{i}"
        if not q.stem.strip():
            problems.append(f"{tag} 题干为空")
        if not q.qid:
            problems.append(f"{tag} 缺少 qid")
        if not q.content_hash:
            problems.append(f"{tag} 缺少 content_hash")
        # 括号（含中文括号）必须成对，能筛掉相当一部分断句错误
        for field_name in ("stem", "answer", "analysis"):
            val = getattr(q, field_name) or ""
            if val.count("（") != val.count("）"):
                problems.append(f"{tag} {field_name} 中文括号不配对")
                break
    return {
        "label": label,
        "count": len(questions),
        "with_answer": sum(1 for q in questions if q.answer),
        "with_analysis": sum(1 for q in questions if q.analysis),
        "problems": problems,
    }


def run_selftest(
    cfg: Config,
    backbone: str = "",
    out_dir: Optional[Path] = None,
    keep_files: bool = True,
) -> Dict[str, Any]:
    work = Path(out_dir) if out_dir else (cfg.data_dir / "selftest")
    ensure_dir(work)

    report: Dict[str, Any] = {
        "started_at": now_iso(),
        "work_dir": str(work),
        "backbone_requested": backbone or cfg.get("models.default", ""),
        "steps": [],
        "ok": True,
    }

    # ---- 1. 选 Backbone（默认不可用时自动回落到离线 mock）----
    used_backbone = backbone
    fallback_note = ""
    try:
        conv = QuestionConverter(cfg, backbone=backbone)
        if not backbone:
            health = conv.client.health_check()
            if not health.get("ok"):
                # health_check 内部吞掉了异常，这里主动抛出以触发回落
                raise BackboneError(health.get("error", "健康检查未通过"))
    except (BackboneError, KeyError) as exc:
        log.warning("默认 Backbone 不可用（%s），回落到 mock-offline 做链路自检", exc)
        try:
            conv = QuestionConverter(cfg, backbone="mock-offline")
        except KeyError:
            report["ok"] = False
            report["error"] = f"默认 Backbone 不可用，且未配置 mock-offline：{exc}"
            return report
        used_backbone = "mock-offline"
        fallback_note = f"默认 Backbone 不可用，已回落到 mock-offline：{str(exc)[:160]}"
    report["backbone"] = conv.info()
    report["backbone_fallback"] = fallback_note

    # ---- 2. 造素材 ----
    pdf_path = _write_pdf(work / "sample-paper.pdf")
    img_path = _write_image(
        work / "sample-question.png",
        ["5．区域地理：读图判断，下列说法正确的是（　　）",
         "   A．甲地年降水量大于乙地",
         "   B．甲地气温年较差小于乙地"],
    )
    txt_path = work / "sample-text.txt"
    txt_path.write_text("\n".join(PAGE1_LINES), encoding="utf-8")
    report["artifacts"] = [str(pdf_path), str(img_path), str(txt_path)]

    # ---- 3. 跑三条链路 ----
    all_checks: List[Dict[str, Any]] = []
    for label, fn in (
        ("PDF→题目", lambda: conv.from_path(pdf_path)),
        ("图像→题目", lambda: conv.from_image(img_path, origin=str(img_path))),
        ("文本→题目", lambda: conv.from_text(txt_path.read_text(encoding="utf-8"), origin=str(txt_path))),
    ):
        step: Dict[str, Any] = {"step": label}
        try:
            qs = fn()
            step.update(_validate(qs, label))
            step["sample_stem"] = qs[0].stem[:120] if qs else ""
            if not qs:
                step["problems"] = step.get("problems", []) + ["未解析出任何题目"]
        except Exception as exc:  # noqa: BLE001
            step.update({"count": 0, "problems": [f"异常：{exc}"]})
            log.exception("%s 失败", label)
        step["ok"] = not step.get("problems")
        report["steps"].append(step)
        all_checks.extend(step.get("problems", []) or [])

    # ---- 4. 公式图片 -> LaTeX ----
    formula_step: Dict[str, Any] = {"step": "公式图→LaTeX"}
    try:
        latex = conv.formula_to_latex(img_path)
        formula_step.update({"latex": latex, "ok": bool(latex) or conv.client.provider == "mock"})
    except Exception as exc:  # noqa: BLE001
        formula_step.update({"ok": False, "problems": [str(exc)[:200]]})
    report["steps"].append(formula_step)

    # ---- 5. 落库（写到独立的自检库，不污染主库）----
    from .storage import SQLiteStore

    try:
        selftest_db = work / "selftest.sqlite3"
        if selftest_db.exists():
            selftest_db.unlink()
        store = SQLiteStore(selftest_db)
        conv2 = conv
        bundle = conv2.from_text(txt_path.read_text(encoding="utf-8"), origin="selftest")
        st = store.save_many(bundle)
        report["storage_roundtrip"] = {"inserted": st["inserted"], "db": str(selftest_db),
                                       "stored_total": store.count()}
        store.close()
    except Exception as exc:  # noqa: BLE001
        report["storage_roundtrip"] = {"error": str(exc)[:200]}
        all_checks.append(f"落库失败：{exc}")

    report["problems"] = all_checks
    report["ok"] = not all_checks
    report["finished_at"] = now_iso()
    report["summary"] = (
        "全部链路通过" if report["ok"]
        else f"发现 {len(all_checks)} 个问题"
    )

    if not keep_files:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
        report["work_dir"] = "(已清理)"

    (work / "selftest-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    ) if keep_files else None
    return report
