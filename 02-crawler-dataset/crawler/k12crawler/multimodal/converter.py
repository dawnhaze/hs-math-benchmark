"""多模态解析：PDF / 图像 / 文本 → 结构化题目。

对应拓展要求 1。三条入口共用同一套 Backbone 抽象，因此换 VLM 只改配置：

    PDF  ──┬─ 有文字层 ──> 文本链路 (LLM 结构化)
           └─ 扫描件  ──> 渲染成图 ──> VLM 直读
    图像 ──────────────> VLM 直读
    文本 ──────────────> LLM 结构化

另外提供「公式图片 → LaTeX」能力：本站题目的数学公式都是 <img>，
把它批量转成 LaTeX，题干才真正可训练、可检索。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..llm import BackboneError, ModelClient, build_client, parse_json_loose, prompts
from ..models import ImageRef, Question
from ..utils import ensure_dir, make_content_hash, short_hash

log = logging.getLogger("k12.multimodal")

ProgressFn = Callable[[str], None]


# ---------------------------------------------------------------------------
def _page_range(spec: str, total: int) -> List[int]:
    """解析 "1-10,15" 这样的页范围，返回 0-based 页码列表。"""
    if not spec:
        return list(range(total))
    pages: List[int] = []
    for part in re.split(r"[,，]", spec):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[-~]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            pages.extend(range(max(1, a), min(total, b) + 1))
        elif part.isdigit():
            p = int(part)
            if 1 <= p <= total:
                pages.append(p)
    return sorted({p - 1 for p in pages}) if pages else list(range(total))


def _payload_to_questions(
    payload: Any,
    *,
    source: str,
    origin: str,
    parser: str = "vlm",
    default_subject: str = "",
) -> List[Question]:
    """把模型返回的 JSON 转成 Question 列表。"""
    if isinstance(payload, dict):
        items = payload.get("questions") or payload.get("data") or []
        if isinstance(items, dict):
            items = [items]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    out: List[Question] = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        stem = str(it.get("stem") or it.get("question") or "").strip()
        if not stem:
            continue
        options = it.get("options") or []
        if isinstance(options, dict):
            options = [f"{k}．{v}" for k, v in options.items()]
        options = [str(o).strip() for o in options if str(o).strip()]

        tags = it.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in re.split(r"[,，、;；]", tags) if t.strip()]

        diff = it.get("difficulty")
        try:
            diff = float(diff) if diff is not None and diff != "" else None
        except (TypeError, ValueError):
            diff = None
        if diff is not None and diff > 1:  # 模型有时给百分制
            diff = diff / 100.0

        q = Question(
            qid="",
            source=source,
            source_url=origin,
            subject=str(it.get("subject") or default_subject or "").strip(),
            grade=_grade_of(str(it.get("subject") or default_subject or "")),
            qtype=str(it.get("qtype") or "").strip(),
            stem=stem,
            options=options,
            answer=str(it.get("answer") or "").strip(),
            analysis=str(it.get("analysis") or "").strip(),
            tags=tags,
            difficulty=diff,
            parser=parser,
            extra={
                "number": str(it.get("number") or i),
                "origin": origin,
                "backbone": "",
            },
        )
        if not q.qtype:
            from ..parser import infer_qtype

            q.qtype = infer_qtype(q.stem, q.options, q.answer)
        q.content_hash = make_content_hash(q.stem, q.options)
        q.qid = f"{source}:{short_hash(q.content_hash)}"
        out.append(q)
    return out


def _grade_of(subject: str) -> str:
    for kw in ("小学", "初中", "高中"):
        if kw in subject:
            return kw
    return ""


# ---------------------------------------------------------------------------
class QuestionConverter:
    """PDF / 图像 / 文本 → 题目。"""

    def __init__(self, cfg, backbone: str = "", client: Optional[ModelClient] = None):
        self.cfg = cfg
        mm = cfg.section("multimodal")
        self.backbone_name = backbone or mm.get("backbone") or cfg.get("models.default", "")
        self.client = client or build_client(cfg, self.backbone_name)
        self.dpi = int(mm.get("render_dpi", 200) or 200)
        self.text_first = bool(mm.get("text_first", True))
        self.max_images = int(mm.get("max_images_per_question", 12) or 12)
        self.image_exts = tuple(mm.get("image_extensions") or [])
        # 超过这个像素就当插图，不送 VLM 转公式（本站公式图都很小，通常 <150px）。
        self.figure_px = int(mm.get("figure_px", 320) or 320)

    # ------------------------------------------------------------------
    # 文本
    # ------------------------------------------------------------------
    def from_text(self, text: str, *, origin: str = "text", subject: str = "") -> List[Question]:
        text = (text or "").strip()
        if not text:
            return []
        prompt = prompts.render(prompts.TEXT_TO_QUESTIONS, content=text[:24000])
        raw = self.client.chat_text(prompt, system=prompts.SYSTEM_EXTRACTOR)
        qs = _payload_to_questions(
            parse_json_loose(raw), source="vlm-text", origin=origin, parser="llm",
            default_subject=subject,
        )
        self._stamp(qs)
        return qs

    # ------------------------------------------------------------------
    # 单张图像
    # ------------------------------------------------------------------
    def from_image(self, image: str | Path | bytes, *, origin: str = "", subject: str = "") -> List[Question]:
        origin = origin or (str(image) if not isinstance(image, bytes) else "image")
        raw = self.client.chat_image(
            image, prompts.IMAGE_TO_QUESTIONS, system=prompts.SYSTEM_EXTRACTOR
        )
        qs = _payload_to_questions(
            parse_json_loose(raw), source="vlm-image", origin=origin, parser="vlm",
            default_subject=subject,
        )
        self._stamp(qs)
        return qs

    # ------------------------------------------------------------------
    # 目录 / 文件（自动按扩展名分派）
    # ------------------------------------------------------------------
    def from_path(
        self,
        path: str | Path,
        *,
        page_range: str = "",
        progress: Optional[ProgressFn] = None,
    ) -> List[Question]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"输入不存在：{p}")
        if p.is_dir():
            out: List[Question] = []
            files = sorted(
                f for f in p.rglob("*")
                if f.is_file() and (
                    f.suffix.lower() == ".pdf" or f.suffix.lower() in self.image_exts
                    or f.suffix.lower() in (".txt", ".md", ".json")
                )
            )
            for i, f in enumerate(files, 1):
                if progress:
                    progress(f"[{i}/{len(files)}] {f.name}")
                try:
                    out.extend(self.from_path(f))
                except Exception as exc:  # noqa: BLE001
                    log.warning("处理失败 %s：%s", f, exc)
            return out

        suffix = p.suffix.lower()
        if suffix == ".pdf":
            return self.from_pdf(p, page_range=page_range, progress=progress)
        if suffix in self.image_exts:
            if progress:
                progress(f"识别图像 {p.name}")
            return self.from_image(p, origin=str(p))
        if suffix in (".txt", ".md"):
            if progress:
                progress(f"解析文本 {p.name}")
            return self.from_text(p.read_text(encoding="utf-8", errors="replace"), origin=str(p))
        raise ValueError(f"不支持的文件类型：{suffix}")

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------
    def from_pdf(
        self,
        pdf_path: str | Path,
        *,
        page_range: str = "",
        progress: Optional[ProgressFn] = None,
    ) -> List[Question]:
        try:
            try:
                import pymupdf as fitz  # PyMuPDF >= 1.24 推荐写法
            except ImportError:  # pragma: no cover
                import fitz  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise BackboneError("缺少 PyMuPDF，请执行：pip install PyMuPDF") from exc

        pdf_path = Path(pdf_path)
        doc = fitz.open(str(pdf_path))
        try:
            total = doc.page_count
            pages = _page_range(page_range or self.cfg.get("multimodal.page_range", ""), total)
            out: List[Question] = []
            for n, pno in enumerate(pages, 1):
                page = doc.load_page(pno)
                text = (page.get_text("text") or "").strip()
                has_text = len(text) >= 60  # 太短的"文字层"基本是页眉页脚
                if progress:
                    mode = "文本层" if (has_text and self.text_first) else "VLM 直读"
                    progress(f"[{n}/{len(pages)}] 第 {pno + 1}/{total} 页 · {mode}")

                try:
                    if has_text and self.text_first:
                        qs = self.from_text(text, origin=f"{pdf_path.name}#p{pno + 1}")
                    else:
                        pix = page.get_pixmap(dpi=self.dpi)
                        qs = self.from_image(
                            pix.tobytes("png"), origin=f"{pdf_path.name}#p{pno + 1}"
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning("第 %d 页解析失败：%s", pno + 1, exc)
                    continue
                for q in qs:
                    q.extra.setdefault("pdf_page", pno + 1)
                out.extend(qs)
            return out
        finally:
            doc.close()

    # ------------------------------------------------------------------
    # 公式图片 → LaTeX
    # ------------------------------------------------------------------
    def formula_to_latex(self, image: str | Path | bytes) -> str:
        try:
            raw = self.client.chat_image(
                image, prompts.FORMULA_TO_LATEX, system=prompts.SYSTEM_FORMULA
            )
        except BackboneError as exc:
            log.warning("公式识别失败：%s", exc)
            return ""
        raw = raw.strip().strip("$").strip()
        raw = re.sub(r"^\$\$?|\$\$?$", "", raw).strip()
        if raw.upper().startswith("UNKNOWN") or len(raw) > 400:
            return ""
        return raw

    def convert_question_formulas(self, q: Question, fetch_bytes) -> Question:
        """把一道题里的公式图片转成 LaTeX，并回填到题干。

        关键点：本站的 `<img>` 标签**不带** width/height（只有 style），
        HTML 阶段根本分不清"行内公式"和"插图"。所以判别挪到这里 ——
        图片本来就要下载，顺手量一下尺寸：小图按公式转 LaTeX，大图直接跳过，
        既判得准，也省掉一次无谓的 VLM 调用。
        """
        if not q.images:
            return q
        latex_map: Dict[int, str] = {}
        for img in q.images[: self.max_images]:
            if img.kind == "figure":
                continue
            data = fetch_bytes(img.url)
            if not data:
                continue

            size = self._measure(data)
            if size:
                img.width, img.height = size
                if max(size) > self.figure_px:
                    img.kind = "figure"   # 实测就是插图，不必劳烦 VLM
                    continue
                img.kind = "formula"

            latex = self.formula_to_latex(data)
            if latex:
                img.latex = latex
                latex_map[img.idx] = latex
        if latex_map:
            from ..utils import resolve_placeholders

            q.stem = resolve_placeholders(q.stem, latex_map)
            q.answer = resolve_placeholders(q.answer, latex_map)
            q.analysis = resolve_placeholders(q.analysis, latex_map)
        return q

    @staticmethod
    def _measure(data: bytes) -> Optional[Tuple[int, int]]:
        """量出图片像素尺寸；缺 Pillow 或格式不支持时返回 None。"""
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                return int(im.width), int(im.height)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    def _stamp(self, questions: Iterable[Question]) -> None:
        for q in questions:
            q.extra["backbone"] = self.client.name
            q.extra["backend_model"] = self.client.model

    # ------------------------------------------------------------------
    def info(self) -> Dict[str, Any]:
        return {
            "name": self.client.name,
            "backbone": self.client.name,
            "provider": self.client.provider,
            "model": self.client.model,
            "base_url": self.client.base_url,
            "vision": self.client.vision,
            "dpi": self.dpi,
            "text_first": self.text_first,
        }
