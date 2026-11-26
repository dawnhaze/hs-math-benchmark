"""存储层：SQLite 为主库 + JSON / JSONL 导出。

为什么用 SQLite？
  * 单文件、零部署，拷走就能用；
  * 支持事务与索引，百万量级题目的去重/检索毫无压力；
  * 配合 content_hash 唯一性检查，天然解决"重复爬同一道题"的问题。

同时维护一份追加式 JSONL 与按需 JSON 快照，方便下游 (pandas / LLM 微调管线) 直接消费。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..models import Question
from ..utils import ensure_dir, human_size, now_iso

log = logging.getLogger("k12.storage")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS questions (
    qid            TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    source_url     TEXT,
    subject        TEXT,
    subject_code   TEXT,
    grade          TEXT,
    qtype          TEXT,
    paper          TEXT,
    year           TEXT,
    stem           TEXT NOT NULL,
    stem_html      TEXT,
    options_json   TEXT,
    answer         TEXT,
    analysis       TEXT,
    answer_html    TEXT,
    images_json    TEXT,
    tags_json      TEXT,
    difficulty     REAL,
    parser         TEXT,
    content_hash   TEXT,
    extra_json     TEXT,
    crawled_at     TEXT,
    updated_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_q_subject  ON questions(subject);
CREATE INDEX IF NOT EXISTS idx_q_grade    ON questions(grade);
CREATE INDEX IF NOT EXISTS idx_q_qtype    ON questions(qtype);
CREATE INDEX IF NOT EXISTS idx_q_hash     ON questions(source, content_hash);
CREATE INDEX IF NOT EXISTS idx_q_crawled  ON questions(crawled_at);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    pages        INTEGER DEFAULT 0,
    fetched      INTEGER DEFAULT 0,
    inserted     INTEGER DEFAULT 0,
    duplicated   INTEGER DEFAULT 0,
    errors       INTEGER DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS seen_hashes (
    source       TEXT,
    content_hash TEXT,
    PRIMARY KEY (source, content_hash)
);
"""

_COLUMNS = [
    "qid", "source", "source_url", "subject", "subject_code", "grade", "qtype",
    "paper", "year", "stem", "stem_html", "options_json", "answer", "analysis",
    "answer_html", "images_json", "tags_json", "difficulty", "parser",
    "content_hash", "extra_json", "crawled_at", "updated_at",
]


class SQLiteStore:
    """线程安全的题目仓库。"""

    def __init__(self, db_path: str | Path, jsonl_path: Optional[str | Path] = None):
        self.db_path = Path(db_path)
        ensure_dir(self.db_path.parent)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        if self.jsonl_path:
            ensure_dir(self.jsonl_path.parent)

        self._lock = threading.RLock()
        self._local = threading.local()
        self._hash_cache: Optional[set] = None

        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        log.debug("SQLite 就绪：%s", self.db_path)

    # ------------------------------------------------------------------
    # 连接管理（每线程一个连接）
    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # 状态（断点续爬）
    # ------------------------------------------------------------------
    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        conn = self._conn()
        row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return default

    def next_page(self, source: str, start_page: int) -> int:
        """下一次应该从哪一页开始（断点续爬的核心）。"""
        val = self.get_state(f"{source}:next_page", None)
        return int(val) if val else int(start_page)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def _load_hash_cache(self) -> set:
        if self._hash_cache is None:
            conn = self._conn()
            rows = conn.execute("SELECT source, content_hash FROM seen_hashes").fetchall()
            self._hash_cache = {(r["source"], r["content_hash"]) for r in rows}
        return self._hash_cache

    @staticmethod
    def _row_of(q: Question) -> Tuple:
        return (
            q.qid, q.source, q.source_url, q.subject, q.subject_code, q.grade, q.qtype,
            q.paper, q.year, q.stem, q.stem_html,
            json.dumps(q.options, ensure_ascii=False),
            q.answer, q.analysis, q.answer_html,
            json.dumps(q.image_dicts, ensure_ascii=False),
            json.dumps(q.tags, ensure_ascii=False),
            q.difficulty, q.parser, q.content_hash,
            json.dumps(q.extra, ensure_ascii=False), q.crawled_at, now_iso(),
        )

    def save_many(self, questions: Iterable[Question]) -> Dict[str, int]:
        """批量入库。返回 {inserted, updated, duplicated}。"""
        stat = {"inserted": 0, "updated": 0, "duplicated": 0}
        rows: List[Tuple] = []
        new_hashes: List[Tuple[str, str]] = []
        accepted: List[Question] = []

        with self._lock:
            cache = self._load_hash_cache()
            for q in questions:
                key = (q.source, q.content_hash)
                if key in cache:
                    stat["duplicated"] += 1
                    continue
                cache.add(key)
                new_hashes.append(key)
                rows.append(self._row_of(q))
                accepted.append(q)

            if not rows:
                return stat

            conn = self._conn()
            placeholders = ",".join("?" for _ in _COLUMNS)
            cols = ",".join(_COLUMNS)
            existing = {
                r[0]
                for r in conn.execute(
                    "SELECT qid FROM questions WHERE qid IN (%s)"
                    % ",".join("?" for _ in accepted),
                    [q.qid for q in accepted],
                ).fetchall()
            }
            sql = f"INSERT INTO questions ({cols}) VALUES ({placeholders})"
            fallback_used = False
            try:
                conn.executemany(sql, rows)
                conn.executemany(
                    "INSERT OR IGNORE INTO seen_hashes(source, content_hash) VALUES (?, ?)",
                    new_hashes,
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:  # qid 冲突 -> 逐条降级
                log.warning("批量写入冲突，降级为逐条写入：%s", exc)
                conn.rollback()
                fallback_used = True
                stat["inserted"] = 0
                stat["duplicated"] = 0
                for q in accepted:
                    try:
                        conn.execute(sql, self._row_of(q))
                        stat["inserted"] += 1
                    except sqlite3.IntegrityError:
                        stat["duplicated"] += 1
                    conn.execute(
                        "INSERT OR IGNORE INTO seen_hashes(source, content_hash) VALUES (?, ?)",
                        (q.source, q.content_hash),
                    )
                conn.commit()

            if not fallback_used:
                stat["inserted"] = len(rows) - len(existing)
                stat["updated"] = len(existing)

        if accepted and self.jsonl_path:
            self._append_jsonl(accepted)
        return stat

    # ------------------------------------------------------------- 就地更正
    _UPDATABLE = (
        "subject", "subject_code", "grade", "qtype", "paper", "year",
        "stem", "stem_html", "options_json", "answer", "analysis",
        "answer_html", "images_json", "tags_json", "difficulty",
        "parser", "updated_at",
    )

    def update_question(self, qid: str, **fields: Any) -> bool:
        """就地更新单题字段，用于解析器升级后修复历史数据。

        content_hash / source_url 是不可变锚点（去重与溯源的依据），刻意排除。
        """
        cols = [k for k in fields if k in self._UPDATABLE]
        if not cols:
            return False
        sets = ", ".join(f"{c} = ?" for c in cols)
        args: List[Any] = [fields[c] for c in cols] + [qid]
        with self._lock:
            conn = self._conn()
            cur = conn.execute(f"UPDATE questions SET {sets} WHERE qid = ?", args)
            conn.commit()
            return cur.rowcount > 0

    def replace_question(self, q: Question) -> None:
        """整条覆盖写入（保留 qid / content_hash），用于解析器升级后的数据修复。"""
        with self._lock:
            conn = self._conn()
            placeholders = ",".join("?" for _ in _COLUMNS)
            cols = ",".join(_COLUMNS)
            conn.execute(
                f"INSERT OR REPLACE INTO questions ({cols}) VALUES ({placeholders})",
                self._row_of(q),
            )
            conn.execute(
                "INSERT OR IGNORE INTO seen_hashes(source, content_hash) VALUES (?, ?)",
                (q.source, q.content_hash),
            )
            conn.commit()

    def fetch_rows(self, source: str = "", only_empty: bool = False, limit: int = 0) -> List[Dict[str, Any]]:
        """取出待重新解析的行。only_empty 时只挑 answer/analysis 有缺的。"""
        sql = "SELECT * FROM questions WHERE 1=1"
        args: List[Any] = []
        if source:
            sql += " AND source = ?"
            args.append(source)
        if only_empty:
            sql += " AND (COALESCE(answer,'') = '' OR COALESCE(analysis,'') = '')"
        sql += " ORDER BY crawled_at DESC"
        if limit and limit > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [row_to_dict(r) for r in self._conn().execute(sql, args).fetchall()]

    def _append_jsonl(self, questions: Sequence[Question]) -> None:
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                for q in questions:
                    f.write(q.to_json() + "\n")
        except OSError as exc:
            log.warning("JSONL 写入失败：%s", exc)

    # ------------------------------------------------------------------
    # 读取 / 检索
    # ------------------------------------------------------------------
    def count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM questions").fetchone()[0]

    def search(
        self,
        keyword: str = "",
        subject: str = "",
        grade: str = "",
        qtype: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM questions WHERE 1=1"
        args: List[Any] = []
        if keyword:
            sql += " AND (stem LIKE ? OR answer LIKE ? OR analysis LIKE ? OR paper LIKE ?)"
            args += [f"%{keyword}%"] * 4
        if subject:
            sql += " AND subject = ?"
            args.append(subject)
        if grade:
            sql += " AND grade = ?"
            args.append(grade)
        if qtype:
            sql += " AND qtype = ?"
            args.append(qtype)
        sql += " ORDER BY crawled_at DESC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        rows = self._conn().execute(sql, args).fetchall()
        return [row_to_dict(r) for r in rows]

    def iter_all(self, batch: int = 500):
        conn = self._conn()
        cur = conn.execute("SELECT * FROM questions")
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            for r in rows:
                yield row_to_dict(r)

    def stats(self) -> Dict[str, Any]:
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        def _group(col: str, limit: int = 12) -> Dict[str, int]:
            rows = conn.execute(
                f"SELECT COALESCE(NULLIF({col},''),'未标注') AS k, COUNT(*) c "
                f"FROM questions GROUP BY k ORDER BY c DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return {r["k"]: r["c"] for r in rows}

        with_answer = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE answer IS NOT NULL AND answer <> ''"
        ).fetchone()[0]
        with_analysis = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE analysis IS NOT NULL AND analysis <> ''"
        ).fetchone()[0]
        with_images = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE images_json NOT LIKE '[]' AND images_json <> ''"
        ).fetchone()[0]
        last_run = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "total": total,
            "with_answer": with_answer,
            "with_answer_pct": round(with_answer / total * 100, 1) if total else 0.0,
            "with_analysis": with_analysis,
            "with_analysis_pct": round(with_analysis / total * 100, 1) if total else 0.0,
            "with_images": with_images,
            "by_subject": _group("subject"),
            "by_grade": _group("grade", 6),
            "by_qtype": _group("qtype", 8),
            "db_path": str(self.db_path),
            "db_size": human_size(self.db_path.stat().st_size) if self.db_path.exists() else "0B",
            "last_run": dict(last_run) if last_run else None,
            "next_page": self.get_state("jiajiao1010:next_page", None),
        }

    # ------------------------------------------------------------------
    # 运行日志
    # ------------------------------------------------------------------
    def start_run(self, source: str, note: str = "") -> int:
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO runs(source, started_at, note) VALUES (?, ?, ?)",
            (source, now_iso(), note),
        )
        conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"pages", "fetched", "inserted", "duplicated", "errors", "note"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                args.append(v)
        sets.append("finished_at=?")
        args.append(now_iso())
        args.append(run_id)
        conn = self._conn()
        conn.execute(f"UPDATE runs SET {','.join(sets)} WHERE id=?", args)
        conn.commit()

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_json(self, path: str | Path, limit: int = 0) -> Dict[str, Any]:
        path = Path(path)
        ensure_dir(path.parent)
        rows: List[Dict[str, Any]] = []
        for i, item in enumerate(self.iter_all()):
            if limit and i >= limit:
                break
            rows.append(item)
        payload = {
            "exported_at": now_iso(),
            "count": len(rows),
            "db": str(self.db_path),
            "questions": rows,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return {"path": str(path), "count": len(rows), "size": human_size(path.stat().st_size)}

    def export_jsonl(self, path: str | Path) -> Dict[str, Any]:
        path = Path(path)
        ensure_dir(path.parent)
        n = 0
        with open(path, "w", encoding="utf-8") as f:
            for item in self.iter_all():
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
                n += 1
        return {"path": str(path), "count": n, "size": human_size(path.stat().st_size)}


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for k in ("options_json", "images_json", "tags_json", "extra_json"):
        if k in d:
            raw = d.pop(k)
            try:
                d[k[:-5]] = json.loads(raw) if raw else ([] if k != "extra_json" else {})
            except (TypeError, json.JSONDecodeError):
                d[k[:-5]] = [] if k != "extra_json" else {}
    return d
