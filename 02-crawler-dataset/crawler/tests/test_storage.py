"""存储层单元测试：写入、去重、状态、统计、导出。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from k12crawler.models import ImageRef, Question  # noqa: E402
from k12crawler.storage import SQLiteStore  # noqa: E402


def _q(n: int, stem: str = "") -> Question:
    return Question(
        qid=f"t:{n}",
        source="t",
        source_url=f"http://x/{n}",
        subject="初中数学",
        grade="初中",
        qtype="选择题",
        stem=stem or f"第 {n} 题：下列说法正确的是（　）",
        options=["A．甲", "B．乙"],
        answer="A" if n % 2 else "B",
        images=[ImageRef(idx=1, url="http://x/a.png", kind="formula")] if n == 1 else [],
    )


def test_save_and_count(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3", tmp_path / "t.jsonl")
    st = store.save_many([_q(1), _q(2), _q(3)])
    assert st["inserted"] == 3
    assert store.count() == 3
    store.close()


def test_dedup_by_content_hash(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3")
    store.save_many([_q(1, "同一道题")])
    st2 = store.save_many([_q(2, "同一道题")])   # 不同 qid，同样题干
    assert st2["inserted"] == 0
    assert st2["duplicated"] == 1
    assert store.count() == 1
    store.close()


def test_upsert_same_qid(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3")
    store.save_many([_q(1)])
    st = store.save_many([_q(1)])  # 同 qid 同内容 -> 视为重复
    assert st["inserted"] == 0
    assert store.count() == 1
    store.close()


def test_state_checkpoint(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3")
    assert store.next_page("s", 1) == 1
    store.set_state("s:next_page", 42)
    assert store.next_page("s", 1) == 42
    assert store.get_state("missing", "def") == "def"
    store.close()


def test_jsonl_append(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3", tmp_path / "t.jsonl")
    store.save_many([_q(1), _q(2)])
    lines = (tmp_path / "t.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["qid"] == "t:1"
    store.close()


def test_search_and_stats(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3")
    store.save_many([_q(i) for i in range(1, 6)])
    rows = store.search("下列说法")
    assert len(rows) == 5
    assert rows[0]["options"] == ["A．甲", "B．乙"]   # JSON 字段已反序列化
    s = store.stats()
    assert s["total"] == 5
    assert s["with_answer"] == 5
    assert s["by_grade"]["初中"] == 5
    store.close()


def test_export(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3")
    store.save_many([_q(1), _q(2)])
    r1 = store.export_json(tmp_path / "out.json")
    r2 = store.export_jsonl(tmp_path / "out.jsonl")
    assert r1["count"] == 2 and r2["count"] == 2
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["questions"][0]["qid"] == "t:1"
    store.close()


def test_run_log(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3")
    rid = store.start_run("t", "test")
    store.finish_run(rid, pages=1, inserted=2, note="done")
    last = store.stats()["last_run"]
    assert last["inserted"] == 2 and last["note"] == "done"
    store.close()


def test_model_roundtrip():
    q = _q(1)
    d = q.to_dict()
    q2 = Question.from_dict(d)
    assert q2.qid == q.qid
    assert q2.images and q2.images[0].url == "http://x/a.png"
    assert q2.to_dict() == d


def test_from_dict_accepts_raw_db_row(tmp_path):
    """原始数据库行用的是 `xxx_json` 列名，图片/选项也不能被静默丢掉。

    这正是 formulas 命令此前"处理 N 题、更新 0 题"的根因。
    """
    store = SQLiteStore(tmp_path / "t.sqlite3")
    store.save_many([_q(1)])
    raw = dict(store._conn().execute("SELECT * FROM questions").fetchone())  # noqa: SLF001
    assert "images_json" in raw and "images" not in raw
    q = Question.from_dict(raw)
    store.close()
    assert q.options == ["A．甲", "B．乙"]
    assert q.images and q.images[0].url == "http://x/a.png"
    assert q.images[0].kind == "formula"


def test_replace_and_update_question(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3")
    store.save_many([_q(1)])

    q = Question.from_dict(store.fetch_rows()[0])
    q.answer = "订正后的答案"
    q.images = [ImageRef(idx=1, url="http://x/b.png", kind="formula", latex="x^2")]
    store.replace_question(q)

    row = store.fetch_rows()[0]
    assert row["answer"] == "订正后的答案"
    assert row["images"][0]["latex"] == "x^2"

    assert store.update_question(q.qid, analysis="新解析")
    assert store.fetch_rows()[0]["analysis"] == "新解析"
    # content_hash 是不可变锚点，update_question 不接受
    assert not store.update_question(q.qid, content_hash="hacked")
    store.close()


def test_fetch_rows_only_empty(tmp_path):
    store = SQLiteStore(tmp_path / "t.sqlite3")
    store.save_many([_q(1), _q(2)])
    store.update_question("t:1", analysis="已有解析")
    assert [r["qid"] for r in store.fetch_rows(only_empty=True)] == ["t:2"]
    assert len(store.fetch_rows()) == 2
    store.close()
