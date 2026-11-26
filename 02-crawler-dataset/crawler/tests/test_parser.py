"""解析层单元测试（离线，不依赖网络）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from k12crawler.parser import (  # noqa: E402
    build_question,
    infer_qtype,
    make_qid,
    parse_column_cards,
    parse_detail,
    parse_index_cards,
    parse_max_index_page,
    split_answer_analysis,
    split_options,
)

# ---------------------------------------------------------------------------
INDEX_HTML = """
<html><body>
<div class='xiti'>
  <p class='title'>
    <strong>科目：</strong><span class='source'></span>
    <strong>来源：</strong><span class='source'>2011年山西省高中阶段教育学校招生统一考试</span>
    <strong>题型：</strong><span class='source'></span>
  </p>
  <div class="xtq"><div><div>
    <p><span>– Hi,Tom. Could you help me cut up the meat for dumplings.</span></p>
    <p><span>– OK,Mom.But where is the ________?</span></p>
    <p><span>A. kinfe    B. fork    C. spoon</span></p>
  </div></div></div>
  <p class='bottom'><a href='http://www.1010jiajiao.com/xiti_id_20' class='daan'>试题详情</a></p>
</div>
<div class='xiti'>
  <p class='title'>
    <strong>科目：</strong><span class='source'></span>
    <strong>来源：</strong><span class='source'>2010-2011学年度泰安市八年级期末</span>
    <strong>题型：</strong><span class='source'></span>
  </p>
  <div class="xtq"><div><p><span>2．下列说法正确的是（　　）</span></p>
  <p><span>A．甲</span><span>　</span><span>B．乙</span></p></div></div>
  <p class='bottom'><a href='http://www.1010jiajiao.com/xiti_id_21' class='daan'>试题详情</a></p>
</div>
<a href='http://www.1010jiajiao.com/xiti_page_5'>5</a>
<a href='http://www.1010jiajiao.com/xiti_page_97155'>97155</a>
</body></html>
"""

DETAIL_HTML = """
<html><body>
<div class="ndwz"><a href="http://www.1010jiajiao.com">精英家教网</a> &gt;
  <a href='http://www.1010jiajiao.com/czyy/index.php'>初中英语</a> &gt; <strong>题目详情</strong></div>
<div class='timutext'>
  <p><span>用方框中所给的单词填空。</span></p>
  <p><span>1．Father&rsquo;s day is on the __________Sunday in June</span></p>
  <p><span>2．下图为等值线图，读图判断 <img src="//thumb.zyjl.cn/a/B.001.png" width="30" height="20" /></span></p>
</div>
<div class='timutext'>
  <div class="answer_tab">
    <div class="answer_inner" id="noanswer">
      <p><span class="exm-an-main exm-an-con">【答案】</span><span>1．third 2．见解析</span></p>
      <p><span>【解析】</span><span>根据句意与语法可确定答案。</span></p>
    </div>
  </div>
</div>
<script>var subject='czyy';</script>
</body></html>
"""


# ---------------------------------------------------------------------------
def test_split_options_ordered_letters():
    stem, opts = split_options("下列正确的是（　） A．甲 B．乙 C．丙 D．丁")
    assert "下列正确的是" in stem
    assert len(opts) == 4
    assert opts[0].startswith("A")


def test_split_options_ignores_scattered_letters():
    # 字母不按 A,B,C 顺序出现时不应被误判为选项
    text = "已知 B．点是中点，C 选项错误，A 与 D 无关"
    stem, opts = split_options(text)
    assert opts == []
    assert stem == text


def test_split_options_no_options():
    stem, opts = split_options("计算 1+1 的值。")
    assert opts == []
    assert stem == "计算 1+1 的值。"


def test_split_answer_analysis():
    ans, ana = split_answer_analysis("【答案】B 【解析】因为电场强度是矢量。")
    assert ans == "B"
    assert ana.startswith("【解析】")

    ans2, ana2 = split_answer_analysis("1、third 2、busier")
    assert ana2 == ""
    assert "third" in ans2


def test_split_answer_analysis_paper_shapes():
    """试卷路线（shiti 页）的三种真实形态，必须各归其位。"""
    # B 形态：分析在前、解答在后，无独立答案 —— 结论要从解答里捞出来
    ans, ana = split_answer_analysis(
        "分析：刻度尺的正确使用方法：（1）看：使用前先看量程．"
        "解答：解：B刻度尺的刻度线一边没有紧靠被测物体；"
        "A刻度尺两条相邻刻度线之间的距离是1mm；故答案为：A；1mm．"
    )
    assert ans == "A；1mm", ans
    assert ana.startswith("分析："), ana
    assert "解答：" in ana

    # C 形态：解答在前、分析在后 —— 两者都属讲解，不得互相污染
    ans3, ana3 = split_answer_analysis(
        "解：（1）法拉第发现电磁感应现象，导致发电机发明；"
        "分析：（1）解答本题应掌握电磁感应的物理学史．"
    )
    assert ana3.startswith("解："), ana3
    assert "分析：" in ana3

    # 答案与解析完全重合时，答案留空，绝不复制粘贴
    ans4, ana4 = split_answer_analysis("分析：只有一段讲解内容，没有独立结论。")
    assert ans4 == ""
    assert ana4.startswith("分析：")

    # 整段无标签：答案区里装的就是答案（填空题多空答案串），不受长度限制
    ans5, ana5 = split_answer_analysis(
        "（1）地中海气候（2）雨水补给 冬 113 110 （3）50 6 "
        "（4）D 地形平坦开阔；河流交汇处，水源丰富，交通便利 （5）沿等高线延伸，坡度缓，易于施工"
    )
    assert ans5.startswith("（1）地中海气候"), ans5
    assert ana5 == ""


def test_infer_qtype():
    assert infer_qtype("下列说法正确的是", ["A．甲", "B．乙"], "") == "选择题"
    assert infer_qtype("水的沸点是 ______", [], "") == "填空题"
    assert infer_qtype("(1)求函数解析式", [], "") == "解答题"


def test_make_qid():
    assert make_qid("s", "http://x.com/xiti_id_20") == "s:20"
    from k12crawler.parser import make_qid as mk

    assert mk("s", "http://x.com/shiti_id_abc123def").startswith("s:sabc123def")
    assert mk("s", "http://x.com/unknown").startswith("s:h")


def test_parse_index_cards():
    cards = parse_index_cards(INDEX_HTML, "http://x/xiti_page_1", lambda u: u)
    assert len(cards) == 2
    assert cards[0]["url"].endswith("xiti_id_20")
    assert "2011年山西省" in cards[0]["paper"]
    assert cards[0]["options"], "选项应被拆出"
    assert len(cards[0]["options"]) == 3


def test_choice_answer_keeps_only_letter():
    """选择题答案常把解析一起塞进来，必须只留字母，否则自动判分全错。"""
    ans, ana = split_answer_analysis(
        "D 以偶函数的图象特征为依据进行判断．∵偶函数y＝f(x)的图象关于y轴对称"
    )
    assert ans == "D", ans
    assert ana.startswith("以偶函数"), ana


def test_subscript_not_mangled_into_blank():
    """单个下划线是数学下标（log_a / x_1），不能被填空正则吃掉。"""
    ans, _ = split_answer_analysis("log_a(x_1 x_2)=4")
    assert "log_a" in ans and "x_1" in ans, ans
    assert "______" not in ans, ans

    # 真正的填空横线仍要归一成 ______
    ans2, _ = split_answer_analysis("答案：_________")
    assert "______" in ans2, ans2


def test_parse_max_index_page():
    assert parse_max_index_page(INDEX_HTML) == 97155
    assert parse_max_index_page("<html></html>") is None


# ---------------------------------------------------------------------------
# 学科栏目路线（/czsx/shiti_page_N）——另一套骨架，必须单独解析
# ---------------------------------------------------------------------------
COLUMN_HTML = """
<html><body>
<div class='xiti'>
  <p class='title'>
    <strong>科目：</strong><span class='source'></span>
    <strong>来源：</strong><span class='source'></span>
    <strong>题型：</strong><span class='source'>单选题</span>
  </p>
  <div class="xtq"><div>
    <h1 class="q-tigan">计算（-x+2y）<sup>2</sup>的结果是</h1><br />
    <ol class="q-xz">
      <li><span class='sn'>A.</span><div class='xx'>-x<sup>2</sup>+4xy+y<sup>2</sup></div></li>
      <li><span class='sn'>B.</span><div class='xx'>x<sup>12</sup>-4xy+4y<sup>2</sup></div></li>
    </ol>
  </div></div>
  <p class='bottom'></p>
  <a href='http://www.1010jiajiao.com/czsx/shiti_id_abc123' class='daan'>查看答案和解析&gt;&gt;</a>
</div>
</body></html>
"""


def test_parse_column_cards():
    cards = parse_column_cards(
        COLUMN_HTML,
        "http://x/czsx/shiti_page_1",
        lambda u: u,
        subject_hint="初中数学",
        subject_code="czsx",
    )
    assert len(cards) == 1
    c = cards[0]
    assert c["url"].endswith("shiti_id_abc123"), c["url"]
    assert c["subject"] == "初中数学"
    assert c["subject_code"] == "czsx"
    assert c["qtype"] == "单选题"
    # <sup> 必须转成 ^，否则 "（-x+2y）2" 的幂次语义就丢了
    assert c["stem"] == "计算（-x+2y）^2的结果是", c["stem"]
    assert c["options"] == ["A. -x^2+4xy+y^2", "B. x^{12}-4xy+4y^2"], c["options"]


def test_parse_max_index_page_prefixes():
    assert parse_max_index_page("<a href='xiti_page_10'>10</a>") == 10
    assert parse_max_index_page("<a href='shiti_page_366461'>x</a>", "shiti_page") == 366461
    assert parse_max_index_page("<html></html>", "shiti_page") is None


def test_dirty_paper_becomes_subject():
    """站点有时把学科代码写进"来源"（如 czyw），应转成学科而不是塞进 paper。"""
    html = INDEX_HTML.replace(
        "<strong>来源：</strong><span class='source'>2011年山西省高中阶段教育学校招生统一考试</span>",
        "<strong>来源：</strong><span class='source'>czyw</span>",
    )
    cards = parse_index_cards(html, "http://x/xiti_page_2400", lambda u: u)
    assert cards, "应解析出卡片"
    assert cards[0]["paper"] == "", cards[0]["paper"]
    assert cards[0]["subject"] == "初中语文", cards[0]["subject"]


def test_parse_detail_fields_and_placeholders():
    d = parse_detail(DETAIL_HTML, "http://x.com/xiti_id_20", lambda u: "https:" + u)
    assert d["subject"] == "初中英语"
    assert d["subject_code"] == "czyy"
    assert d["grade"] == "初中"
    assert "Father" in d["stem"]
    assert d["answer"].startswith("1．third")
    assert d["analysis"].startswith("【解析】")
    # 公式图片应变成占位符并登记
    assert "{{img:1}}" in d["stem"]
    assert d["images"] and d["images"][0].url.startswith("https://thumb.zyjl.cn")


def test_build_question_merges_card_and_detail():
    cards = parse_index_cards(INDEX_HTML, "http://x/xiti_page_1", lambda u: u)
    card = cards[0]
    detail = parse_detail(DETAIL_HTML, "http://x.com/xiti_id_20", lambda u: u,
                          fallback_subject=card["subject"])
    q = build_question(source="s", qid="s:20", url=card["url"], card=card, detail=detail)
    assert q is not None
    assert q.qid == "s:20"
    assert q.content_hash
    assert q.answer
    assert q.qtype  # 由 infer_qtype 兜底
    assert q.extra["has_detail"] is True


def test_build_question_without_detail_still_works():
    cards = parse_index_cards(INDEX_HTML, "http://x/xiti_page_1", lambda u: u)
    q = build_question(source="s", qid="s:20", url=cards[0]["url"], card=cards[0], detail={})
    assert q is not None and q.stem
    assert q.answer == ""
