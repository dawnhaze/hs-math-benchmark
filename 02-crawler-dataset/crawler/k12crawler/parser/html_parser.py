"""解析层：把站点 HTML 变成规范化 Question 对象。

站点（1010jiajiao.com）的页面有两类：
  1) 索引页  xiti_page_{N}  —— 每页 10 张题目卡片，含 来源/题型/题干
  2) 详情页  xiti_id_{M}    —— 单题完整内容，含 答案/解析 + 学科面包屑

数学/物理公式在本站是 <img>。解析时统一替换为 {{img:n}} 占位符并登记图片，
后续可交给 VLM 转 LaTeX —— 这是"多模态取题"的接入口。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

from ..models import ImageRef, Question
from ..utils import (
    collapse_text,
    make_content_hash,
    normalize_text,
    placeholder,
    short_hash,
)

# ---------------------------------------------------------------------------
# 学科代码 -> 中文名（站点内部代码）
# ---------------------------------------------------------------------------
SUBJECT_CODE_MAP: Dict[str, str] = {
    "xxsx": "小学数学", "xxyy": "小学英语", "xxyw": "小学语文",
    "czsx": "初中数学", "czyy": "初中英语", "czyw": "初中语文",
    "czwl": "初中物理", "czhx": "初中化学", "czsw": "初中生物",
    "czdl": "初中地理", "czzz": "初中政治", "czls": "初中历史",
    "gzsx": "高中数学", "gzyy": "高中英语", "gzyw": "高中语文",
    "gzwl": "高中物理", "gzhx": "高中化学", "gzsw": "高中生物",
    "gzdl": "高中地理", "gzzz": "高中政治", "gzls": "高中历史",
}

_STAGE_BY_PREFIX = [("xx", "小学"), ("cz", "初中"), ("gz", "高中")]

# 块级标签：其前后需要保留换行，避免 get_text 把整段压成一行
BLOCK_TAGS = {
    "p", "div", "tr", "td", "th", "li", "ul", "ol", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    "blockquote", "center", "dt", "dd", "pre",
}

# 选项切分：要求字母按 A,B,C... 顺序出现，避免把正文误判成选项
_OPT_CANDIDATE_RE = re.compile(r"(?:^|[\s\u3000>）)】])([A-H])\s*[．.、,]\s*")
# 填空横线：至少 3 个连续下划线，或全角下划线。
# 注意：绝不能匹配单个 `_` —— 我们把 <sub> 转成了 `x_1` / `log_a`，
# 裸下划线一旦被吃，数学下标就全毁了（曾出现 `log______a(x______1…)`）。
_BLANK_RE = re.compile(r"(?:_{3,}|[　\s]*＿+[　\s]*)")
_YEAR_RE = re.compile(r"(19|20)\d{2}")
# 只清理题干前缀的【题目】；【答案】【解析】等标签必须保留，
# 它们是 split_answer_analysis 切分答案与解析的唯一锚点。
_LABEL_RE = re.compile(r"【题目】")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def subject_from_code(code: str) -> Tuple[str, str]:
    """返回 (学科全名, 学段)。"""
    code = (code or "").strip().lower()
    if code in SUBJECT_CODE_MAP:
        name = SUBJECT_CODE_MAP[code]
        for prefix, stage in _STAGE_BY_PREFIX:
            if code.startswith(prefix):
                return name, stage
        return name, ""
    return "", ""


def _stage_from_name(name: str) -> str:
    for kw, stage in (("小学", "小学"), ("初中", "初中"), ("高中", "高中"), ("高考", "高中"), ("中考", "初中")):
        if kw in (name or ""):
            return stage
    return ""


def _year_from_paper(paper: str) -> str:
    m = _YEAR_RE.search(paper or "")
    return m.group(0) if m else ""


def _classify_image(img: Tag) -> str:
    """粗判图片类型：小尺寸 = 行内公式，中等 = 插图，大图 = 图片题。"""
    def _px(v: Any) -> int:
        try:
            return int(re.sub(r"\D", "", str(v)) or 0)
        except ValueError:
            return 0

    w, h = _px(img.get("width")), _px(img.get("height"))
    if w and h and w <= 130 and h <= 60:
        return "formula"
    if w and h and (w >= 300 or h >= 300):
        return "figure"
    return "inline"


def _clean_label(text: str) -> str:
    return _LABEL_RE.sub("", text or "").strip()


# ---------------------------------------------------------------------------
# HTML 片段 -> (纯文本, 图片列表)
# ---------------------------------------------------------------------------

def fragment_to_text(
    node: Tag,
    resolve_url,
    start_index: int = 1,
) -> Tuple[str, List[ImageRef]]:
    """把一段 HTML 转成纯文本，并把 <img> 换成 {{img:n}} 占位符。

    同时把 <u>　　　</u> 这类下划线空位统一成 ______。
    """
    if node is None:
        return "", []

    # 深拷贝，避免污染原 DOM
    node = BeautifulSoup(str(node), "lxml")
    root = node.body or node

    # 空位统一
    for u in root.find_all(["u", "s"]):
        raw = u.get_text("", strip=False)
        if raw and not raw.replace("\u3000", "").replace("\xa0", "").strip():
            u.string = "______"

    # 上标 / 下标：本站用 <sup>2</sup> 表示幂次。直接 get_text 会压成 "x2"，
    # 幂次语义就没了。转成 ^2 / ^{12} / _n 形式，数学含义得以保留。
    for tag_name, marker in (("sup", "^"), ("sub", "_")):
        for el in root.find_all(tag_name):
            if el.find("img"):
                continue
            inner = el.get_text("", strip=True)
            if not inner:
                el.decompose()
                continue
            wrapped = inner if len(inner) == 1 else "{" + inner + "}"
            el.replace_with(NavigableString(marker + wrapped))

    images: List[ImageRef] = []
    counter = {"n": max(1, start_index)}
    for img in root.find_all("img"):
        src = resolve_url(img.get("src") or img.get("data-src") or "")
        if not src:
            img.decompose()
            continue
        idx = counter["n"]
        counter["n"] += 1
        images.append(
            ImageRef(
                idx=idx,
                url=src,
                kind=_classify_image(img),
                width=int(re.sub(r"\D", "", str(img.get("width"))) or 0) or None,
                height=int(re.sub(r"\D", "", str(img.get("height"))) or 0) or None,
                alt=(img.get("alt") or "").strip(),
            )
        )
        img.replace_with(NavigableString(placeholder(idx)))

    for tag in root.find_all(["script", "style"]):
        tag.decompose()
    for br in root.find_all("br"):
        br.replace_with(NavigableString("\n"))

    # 块级元素前后补换行：这样 get_text("") 既不会把行内 span 拆碎，
    # 又能保住段落/表格行的结构。
    for tag in root.find_all(BLOCK_TAGS):
        tag.insert_before(NavigableString("\n"))
        tag.append(NavigableString("\n"))

    text = root.get_text("")
    text = normalize_text(text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = _clean_label(text)
    return text, images


# ---------------------------------------------------------------------------
# 选项拆分
# ---------------------------------------------------------------------------

def split_options(text: str) -> Tuple[str, List[str]]:
    """从题干中拆出选项。返回 (题干, 选项列表)。"""
    if not text:
        return "", []

    matches = list(_OPT_CANDIDATE_RE.finditer(text))
    if len(matches) < 2:
        return text, []

    # 要求字母序列严格递增（A,B,C,D...）才认为是选项区
    seq: List[re.Match] = []
    expect = ord("A")
    for m in matches:
        if ord(m.group(1)) == expect:
            seq.append(m)
            expect += 1
        elif ord(m.group(1)) == ord("A"):
            seq = [m]
            expect = ord("B")
    if len(seq) < 2:
        return text, []

    stem = text[: seq[0].start()].strip()
    options: List[str] = []
    for i, m in enumerate(seq):
        end = seq[i + 1].start() if i + 1 < len(seq) else len(text)
        chunk = text[m.start():end].strip()
        chunk = re.sub(r"\s*\n\s*", " ", chunk)
        options.append(chunk)

    stem = _BLANK_RE.sub("______", stem)
    if not stem.strip():
        return text, []
    return stem, options


# ---------------------------------------------------------------------------
# 答案 / 解析 拆分
# ---------------------------------------------------------------------------

# 答案标签：出现即表示"紧跟其后的短文本就是答案"
_ANSWER_LABELS = ("【参考答案】", "【答案】", "答案：", "答案:", "参考答案：", "参考答案:")
# 解析标签：出现即表示"紧跟其后的内容是讲解过程"
# 注意：本站混用全角「：」与半角「:」，两种都要认，否则整段会被误当成答案。
_EXPLAIN_LABELS = (
    "【解法提示】", "【解析】", "【详解】", "【解题思路】", "【分析】", "【点评】",
    "思路分析：", "思路分析:", "试题点评：", "试题点评:",
    "解析：", "解析:", "详解：", "详解:", "解答：", "解答:", "分析：", "分析:", "点评：", "点评:",
    "解法：", "解法:", "解：", "解:",
)
# 长标签优先，避免「详解：」被「解：」抢先、「思路分析：」被「分析：」抢先。
_SECTION_RE = re.compile(
    "|".join(
        re.escape(x)
        for x in sorted(set(_ANSWER_LABELS) | set(_EXPLAIN_LABELS), key=len, reverse=True)
    )
)
# 页面没给独立答案时，从讲解过程里"捞"出最终结论。
_FINAL_ANSWER_RES = (
    # 填空题常有多个空（"A；1mm；2.11cm"），因此只在句末标点处收尾。
    re.compile(r"故答案为[:：]?\s*([^．。\n]{1,80})"),
    re.compile(r"答案为[:：]?\s*([^．。\n]{1,80})"),
    re.compile(r"故选[:：]?\s*([A-D][^．。\n]{0,24})"),
    re.compile(r"答案[:：]\s*([^．。\n]{1,80})"),
)


# 选择题答案常把解析一起塞进来："D 以偶函数的图象特征为依据…"。
# 只留字母当答案，其余归解析 —— 否则自动判分时模型答 "D" 会被判错。
_LEADING_OPTION_RE = re.compile(r"^\s*([A-D](?:[\s,，、]*[A-D])*)\s*")


def _split_trailing_explanation(answer: str) -> Tuple[str, str]:
    """把"字母 + 长解释"的答案拆成 (字母, 解释)。不像就原样返回。"""
    m = _LEADING_OPTION_RE.match(answer or "")
    if not m:
        return answer, ""
    rest = answer[m.end():].strip()
    if len(rest) < 12:
        return answer, ""
    letters = re.sub(r"[\s,，、]", "", m.group(1))
    return letters, rest


def split_answer_analysis(text: str) -> Tuple[str, str]:
    """把答案区文本拆成 (答案, 解析)。

    实采到三种页面形态，不能只"切一刀"：
      A. `【答案】B【解析】…故选B．`        —— 答案与解析分列
      B. `分析：…解答：解：…故答案为：…`   —— 无独立答案，结论藏在解答里
      C. `解：…分析：…`                    —— 解答在前、分析在后
    因此这里改成"按标签分段 -> 各归其位"：答案段进 answer，讲解段进 analysis；
    若没有答案段，则从讲解里抽取最终结论（故答案为/故选/答案为）作为 answer；
    整段连一个标签都没有时，答案区里装的本来就是答案，直接收进 answer。
    都没有就把 answer 留空 —— 宁可空，也不伪造。
    """
    if not text:
        return "", ""
    text = normalize_text(text)

    matches = list(_SECTION_RE.finditer(text))
    lead = text[: matches[0].start()].strip() if matches else text.strip()
    answer_parts: List[str] = []
    analysis_parts: List[str] = []
    pending = ""  # 只有标签、没有正文的段头（如 `解答：` 紧跟着 `解：`），并入下一段
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        label = m.group(0)
        content = text[m.end():end].strip()
        if label in _ANSWER_LABELS:
            if content:
                answer_parts.append(content)
            continue
        span = text[m.start():end].strip()
        if span == label:
            pending += span
            continue
        analysis_parts.append(pending + span)
        pending = ""

    explains_text = "\n".join(analysis_parts) or lead

    if answer_parts:
        answer = "\n".join(answer_parts)
    else:
        answer = ""
        for pat in _FINAL_ANSWER_RES:
            hit = pat.search(explains_text)
            if hit:
                answer = hit.group(1).strip(" ．。;；")
                break
        if not answer:
            if not matches:
                # 整段一个标签都没有：答案区里装的就是答案（如填空题的多空答案串）。
                bare = text.strip()
                answer = bare if len(bare) <= 1000 else ""
                if not answer:
                    analysis_parts = [bare] if bare else []
            else:
                cand = lead.strip()
                # 标签前若有很短的裸文本，才认作答案，避免把讲解误当答案。
                if cand and len(cand) <= 60 and "\n" not in cand:
                    answer = cand

    # 收尾：答案里若跟着一段解释，把字母留下、解释归解析（自动判分需要纯字母）。
    letters, tail = _split_trailing_explanation(answer)
    if tail:
        answer = letters
        analysis_parts.insert(0, tail)

    if analysis_parts:
        analysis = "\n".join(analysis_parts)
    elif lead and lead != answer:
        analysis = lead
    else:
        analysis = ""

    # 页面确实没给独立答案时会退化成"答案=解析"的重复，这里直接去重。
    if answer and analysis and normalize_text(answer) == normalize_text(analysis):
        answer = ""

    return _BLANK_RE.sub("______", answer).strip(), analysis.strip()


# ---------------------------------------------------------------------------
# 索引页解析
# ---------------------------------------------------------------------------

def parse_index_cards(html: str, page_url: str, resolve_url) -> List[Dict[str, Any]]:
    """解析 xiti_page_{N}，返回题目卡片列表（题干 + 元数据 + 详情链接）。"""
    soup = BeautifulSoup(html, "lxml")
    cards: List[Dict[str, Any]] = []

    for box in soup.find_all("div", class_="xiti"):
        bottom = box.find("p", class_="bottom")
        link = bottom.find("a") if bottom else None
        href = link.get("href", "") if link else ""
        if not href:
            continue
        url = resolve_url(href)

        # 元数据：科目 / 来源 / 题型（三个 <span class='source'> 按序对应）
        subject = paper = qtype = ""
        title = box.find("p", class_="title")
        if title:
            sources = title.find_all("span", class_="source")
            vals = [s.get_text(" ", strip=True) for s in sources]
            if len(vals) >= 3:
                subject, paper, qtype = vals[0], vals[1], vals[2]
            elif len(vals) == 2:
                subject, paper = vals[0], vals[1]
            elif len(vals) == 1:
                paper = vals[0]

        # 站点偶尔把学科代码塞进"来源"（实测 xiti_page_2400 段全是 "czyw"）。
        # 这属于脏数据：认出来就转成学科元数据，别让 paper 里躺着 "czyw"。
        if re.fullmatch(r"[a-z]{2,6}", paper or ""):
            code_name, _code_stage = subject_from_code(paper)
            if code_name:
                subject = subject or code_name
                paper = ""

        stem_node = box.find("div", class_="xtq")
        stem_html = str(stem_node) if stem_node else ""
        stem, images = fragment_to_text(stem_node, resolve_url) if stem_node else ("", [])
        stem, options = split_options(stem)

        if not stem:
            continue

        cards.append(
            {
                "url": url,
                "subject": subject,
                "paper": paper,
                "qtype": qtype,
                "stem": stem,
                "stem_html": stem_html,
                "options": options,
                "images": images,
                "page_url": page_url,
            }
        )
    return cards


def parse_max_index_page(html: str, pattern: str = "xiti_page") -> Optional[int]:
    """从索引页底部的分页条推断最大页码。

    全局索引用 `xiti_page_N`，学科栏目用 `shiti_page_N`，所以前缀可配。
    """
    nums = [int(n) for n in re.findall(rf"{re.escape(pattern)}_(\d+)", html)]
    return max(nums) if nums else None


def parse_column_cards(
    html: str,
    page_url: str,
    resolve_url,
    subject_hint: str = "",
    subject_code: str = "",
) -> List[Dict[str, Any]]:
    """解析学科栏目页（`{column}/shiti_page_{N}`），返回题目卡片列表。

    这套骨架与全局 `xiti_page_{N}` 不同，必须分开解析：
      · 题干在 `h1.q-tigan`（不是 `div.xtq` 的混排文本）；
      · 选项在 `ol.q-xz > li`（每条 = `span.sn` 序号 + `div.xx` 内容）；
      · 详情链接是 `a.daan`（"查看答案和解析>>"），不在 `p.bottom` 里；
      · 科目三要素仍是 `p.title` 里的三个 `span.source`。
    """
    soup = BeautifulSoup(html, "lxml")
    cards: List[Dict[str, Any]] = []

    for box in soup.find_all("div", class_="xiti"):
        link = box.find("a", class_="daan") or box.find("a", href=re.compile(r"shiti_id_"))
        href = link.get("href", "") if link else ""
        if not href:
            continue

        subject, paper, qtype = subject_hint, "", ""
        title = box.find("p", class_="title")
        if title:
            vals = [s.get_text(" ", strip=True) for s in title.find_all("span", class_="source")]
            if len(vals) >= 3:
                subject = vals[0] or subject
                paper, qtype = vals[1], vals[2]
            elif len(vals) == 2:
                subject = vals[0] or subject
                paper = vals[1]
            elif len(vals) == 1:
                # 只有一个 span 时，看它像不像题型（"单选题"），否则当来源。
                if re.search(r"题$", vals[0]):
                    qtype = vals[0]
                else:
                    paper = vals[0]

        if re.fullmatch(r"[a-z]{2,6}", paper or ""):
            code_name, _ = subject_from_code(paper)
            if code_name:
                subject = subject or code_name
                paper = ""

        node = box.find("h1", class_="q-tigan") or box.find("div", class_="xtq")
        if node is None:
            continue
        stem_html = str(node)
        stem, images = fragment_to_text(node, resolve_url)

        options: List[str] = []
        opt_list = box.find("ol", class_="q-xz")
        if opt_list:
            idx = len(images) + 1
            for li in opt_list.find_all("li"):
                text, imgs = fragment_to_text(li, resolve_url, start_index=idx)
                idx += len(imgs)
                images.extend(imgs)
                text = re.sub(r"\s*\n\s*", " ", text).strip()
                if text:
                    options.append(text)
            if not qtype:
                qtype = "选择题"

        if not stem:
            continue

        cards.append(
            {
                "url": resolve_url(href),
                "subject": subject,
                "subject_code": subject_code,
                "paper": paper,
                "qtype": qtype,
                "stem": stem,
                "stem_html": stem_html,
                "options": options,
                "images": images,
                "page_url": page_url,
            }
        )
    return cards


# ---------------------------------------------------------------------------
# 详情页解析
# ---------------------------------------------------------------------------

def parse_detail(
    html: str,
    detail_url: str,
    resolve_url,
    fallback_subject: str = "",
) -> Dict[str, Any]:
    """解析 xiti_id_{M} 详情页，返回 题干/答案/解析/学科 等字段。"""
    soup = BeautifulSoup(html, "lxml")
    out: Dict[str, Any] = {
        "subject": fallback_subject,
        "subject_code": "",
        "grade": _stage_from_name(fallback_subject),
        "stem": "",
        "stem_html": "",
        "options": [],
        "answer": "",
        "analysis": "",
        "answer_html": "",
        "images": [],
    }

    # --- 学科代码（页面里的 var subject='czyy'）---
    m = re.search(r"var\s+subject\s*=\s*['\"]([a-z]{2,6})['\"]", html)
    if m:
        code = m.group(1)
        name, stage = subject_from_code(code)
        out["subject_code"] = code
        if name:
            out["subject"] = name
            out["grade"] = stage

    # --- 面包屑：精英家教网 > 初中英语 > 题目详情 ---
    crumb = soup.find("div", class_="ndwz")
    if crumb:
        links = [a.get_text(strip=True) for a in crumb.find_all("a")]
        if len(links) >= 2:
            name = links[-1]
            if name and "首页" not in name:
                out["subject"] = name
                out["grade"] = _stage_from_name(name) or out["grade"]

    # --- 题干：第一个 div.timutext ---
    blocks = soup.find_all("div", class_="timutext")
    if blocks:
        stem_node = blocks[0]
        stem_html = str(stem_node)
        stem, images = fragment_to_text(stem_node, resolve_url)
        stem, options = split_options(stem)
        out["stem"] = stem
        out["stem_html"] = stem_html
        out["options"] = options
        out["images"] = images

    # --- 答案 / 解析：div#noanswer ---
    answer_node = soup.find("div", id="noanswer")
    if answer_node is None:
        answer_node = soup.find("div", class_="answer_inner")
    if answer_node is not None:
        out["answer_html"] = str(answer_node)
        ans_text, ans_images = fragment_to_text(
            answer_node, resolve_url, start_index=len(out["images"]) + 1
        )
        answer, analysis = split_answer_analysis(ans_text)
        out["answer"] = answer
        out["analysis"] = analysis
        out["images"].extend(ans_images)

    return out


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def build_question(
    *,
    source: str,
    qid: str,
    url: str,
    card: Optional[Dict[str, Any]] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Optional[Question]:
    """把卡片（列表页）与详情（详情页）合并成一道完整题目。"""
    card = card or {}
    detail = detail or {}

    stem = detail.get("stem") or card.get("stem") or ""
    if not stem:
        return None
    options = detail.get("options") or card.get("options") or []
    images = list(detail.get("images") or card.get("images") or [])

    subject = detail.get("subject") or card.get("subject") or ""
    grade = detail.get("grade") or _stage_from_name(subject)
    paper = card.get("paper") or ""
    qtype = card.get("qtype") or ""

    stem, options = split_options(stem) if not options else (stem, options)

    if not qtype:
        qtype = infer_qtype(stem, options, detail.get("answer", ""))

    # 去重：同一题干 + 选项，视为同一题
    content_hash = make_content_hash(stem, options)

    q = Question(
        qid=qid,
        source=source,
        source_url=url,
        subject=subject,
        subject_code=detail.get("subject_code", ""),
        grade=grade,
        qtype=qtype,
        paper=paper,
        year=_year_from_paper(paper),
        stem=stem,
        stem_html=card.get("stem_html") or detail.get("stem_html") or "",
        options=options,
        answer=detail.get("answer", ""),
        analysis=detail.get("analysis", ""),
        answer_html=detail.get("answer_html", ""),
        images=images,
        parser="html",
        content_hash=content_hash,
        extra={
            "index_page": card.get("page_url", ""),
            "has_detail": bool(detail),
        },
    )
    return q


def infer_qtype(stem: str, options: List[str], answer: str) -> str:
    """没抓到题型时，按内容启发式推断（LLM 增强开启时会覆盖更准的结果）。"""
    if options:
        return "选择题"
    if "______" in stem:
        return "填空题"
    if re.search(r"[（(]\s*\d+\s*[)）]", stem) or "解答" in stem or "证明" in stem:
        return "解答题"
    if re.search(r"判断|对错|√|×", stem):
        return "判断题"
    if not stem:
        return ""
    return "其他"


def make_qid(source: str, url: str) -> str:
    """由详情页 URL 派生稳定 qid；没有数字 ID 时退化为内容哈希。"""
    m = re.search(r"xiti_id_(\d+)", url) or re.search(r"timu_id_(\d+)", url)
    if m:
        return f"{source}:{m.group(1)}"
    m2 = re.search(r"shiti_id_([0-9a-f]+)", url)
    if m2:
        return f"{source}:s{m2.group(1)}"
    return f"{source}:h{short_hash(url)}"
