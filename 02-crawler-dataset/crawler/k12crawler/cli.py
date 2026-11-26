"""命令行入口：k12crawler

Agent-Friendly 设计三条铁律：
  1. 所有命令都支持 --json，输出严格可解析的 JSON（Agent 直接消费，不用正则抠屏）；
  2. 退出码有语义：0 成功 / 1 失败 / 2 参数或配置问题；
  3. 不需要交互式输入，一切参数都能从命令行或配置文件给定。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

# 保证中文在 Windows 管道里不乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

from . import __version__
from .config import Config, EXAMPLE_CONFIG_PATH, load_config
from .fetcher import Fetcher
from .llm import BackboneError, build_client
from .multimodal import QuestionConverter
from .pipeline import CrawlPipeline
from .scheduler import run_daemon
from .spiders import available_spiders, get_spider
from .storage import SQLiteStore
from .utils import ensure_dir, human_size, now_stamp

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="K12 试题数据爬虫 —— 自动采集 / 多模态解析 / Agent-Friendly",
)

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


# ---------------------------------------------------------------------------
class Ctx:
    def __init__(self, config_path: Optional[str], as_json: bool, verbose: bool):
        self.config_path = config_path
        self.as_json = as_json
        self.verbose = verbose
        self._cfg: Optional[Config] = None

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            self._cfg = load_config(self.config_path)
        return self._cfg

    @property
    def console(self):
        from rich.console import Console

        return Console(highlight=False, soft_wrap=False)


def _setup_logging(cfg: Config, verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else getattr(logging, str(cfg.get("project.log_level", "INFO")), logging.INFO)
    log_dir = ensure_dir(cfg.log_dir)
    handlers: List[logging.Handler] = [logging.FileHandler(log_dir / "crawler.log", encoding="utf-8")]
    if not quiet:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def _out(ctx: typer.Context, payload: Any, renderer=None) -> None:
    """统一输出：JSON 模式直出，否则交给 rich 渲染。"""
    obj: Ctx = ctx.obj
    if obj.as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
    elif renderer is not None:
        renderer(payload)
    else:
        from rich import print_json

        print_json(json.dumps(payload, ensure_ascii=False, default=str))


def _fail(message: str, code: int = EXIT_FAIL) -> None:
    typer.secho(f"✖ {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
@app.callback()
def main(
    ctx: typer.Context,
    config: Optional[str] = typer.Option(None, "--config", "-c", help="配置文件路径，默认 config/config.yaml"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出（Agent 友好）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出调试日志"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="不打印日志到终端"),
) -> None:
    ctx.obj = Ctx(config, as_json, verbose)
    try:
        cfg = ctx.obj.cfg
    except Exception as exc:  # noqa: BLE001
        _fail(f"配置加载失败：{exc}", EXIT_USAGE)
        return
    _setup_logging(cfg, verbose, quiet)


# ---------------------------------------------------------------------------
@app.command("version")
def cmd_version(ctx: typer.Context) -> None:
    """显示版本与运行环境。"""
    from .binary_info import runtime_info

    _out(ctx, {"version": __version__, **runtime_info()})


@app.command("init")
def cmd_init(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 config.yaml"),
) -> None:
    """初始化：生成配置文件、数据目录与数据库。"""
    from .config import DEFAULT_CONFIG_PATH

    cfg_path = Path(ctx.obj.config_path) if ctx.obj.config_path else DEFAULT_CONFIG_PATH
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path

    created = []
    if not cfg_path.exists() or force:
        ensure_dir(cfg_path.parent)
        cfg_path.write_text(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(str(cfg_path))

    cfg = load_config(str(cfg_path))
    for p in (
        cfg.data_dir,
        cfg.log_dir,
        cfg.path_of("storage.json_snapshot_dir", "data/json"),
        cfg.path_of("storage.raw_html_dir", "data/raw"),
    ):
        ensure_dir(p)

    store = SQLiteStore(
        cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"),
        cfg.path_of("storage.jsonl_path", "data/questions.jsonl"),
    )
    store.close()

    payload = {
        "ok": True,
        "config": str(cfg_path),
        "created": created,
        "data_dir": str(cfg.data_dir),
        "db": str(cfg.path_of("storage.sqlite_path", "data/k12.sqlite3")),
        "backbones": cfg.backbone_names(),
        "sources": available_spiders(),
    }
    _out(
        ctx,
        payload,
        lambda d: ctx.obj.console.print(
            f"[green]✔[/] 初始化完成\n  配置: {d['config']}\n  数据: {d['data_dir']}\n"
            f"  数据库: {d['db']}\n  Backbone: {', '.join(d['backbones'])}\n"
            f"  数据源: {', '.join(d['sources'])}"
        ),
    )


@app.command("sources")
def cmd_sources(ctx: typer.Context) -> None:
    """列出可用的站点适配器。"""
    names = available_spiders()
    _out(ctx, {"sources": names}, lambda d: ctx.obj.console.print("\n".join(["数据源：", *[f"  · {n}" for n in d["sources"]]])))


@app.command("backbones")
def cmd_backbones(
    ctx: typer.Context,
    check: bool = typer.Option(False, "--check", help="逐个做连通性自检（会真的调用模型）"),
) -> None:
    """列出可配置的 VLM/LLM Backbone（拓展要求 2 的入口）。"""
    cfg = ctx.obj.cfg
    default = cfg.get("models.default", "")
    rows: List[Dict[str, Any]] = []
    for name in cfg.backbone_names():
        spec = cfg.backbone(name)
        row = {
            "name": name,
            "default": name == default,
            "provider": spec.get("provider"),
            "model": spec.get("model"),
            "base_url": spec.get("base_url"),
            "vision": bool(spec.get("vision", True)),
            "note": spec.get("note", ""),
        }
        if check:
            try:
                row["health"] = build_client(cfg, name).health_check()
            except BackboneError as exc:
                row["health"] = {"ok": False, "error": str(exc)}
        rows.append(row)

    def render(d: Dict[str, Any]) -> None:
        from rich.table import Table

        t = Table(title=f"Backbone 列表（默认：{d['default']}）", show_lines=False)
        t.add_column("名称", style="cyan", no_wrap=True)
        t.add_column("默认", justify="center")
        t.add_column("provider")
        t.add_column("model")
        t.add_column("视觉", justify="center")
        if d["checked"]:
            t.add_column("连通", justify="center")
        t.add_column("备注", overflow="fold")
        for r in d["backbones"]:
            cells = [
                r["name"], "★" if r["default"] else "", str(r["provider"]), str(r["model"]),
                "✔" if r["vision"] else "—",
            ]
            if d["checked"]:
                h = r.get("health") or {}
                cells.append("[green]OK[/]" if h.get("ok") else "[red]FAIL[/]")
            cells.append(r.get("note", ""))
            t.add_row(*cells)
        ctx.obj.console.print(t)

    _out(ctx, {"default": default, "checked": check, "backbones": rows}, render)


# ---------------------------------------------------------------------------
@app.command("crawl")
def cmd_crawl(
    ctx: typer.Context,
    pages: Optional[int] = typer.Option(None, "--pages", "-p", help="本次最多翻多少索引页（0=不限）"),
    start_page: Optional[int] = typer.Option(None, "--start-page", help="从第几页开始（默认接着上次）"),
    max_questions: Optional[int] = typer.Option(None, "--max-questions", "-n", help="本次最多入库多少题"),
    detail: Optional[bool] = typer.Option(None, "--detail/--no-detail", help="是否抓详情页拿答案与解析"),
    reset: bool = typer.Option(False, "--reset", help="忽略断点，从头开始"),
    source: Optional[str] = typer.Option(None, "--source", help="站点适配器名称"),
    column: Optional[str] = typer.Option(
        None, "--column", help="学科栏目（仅 jiajiao1010-subject 路线用，如 czsx / gzsx / czwl）"
    ),
) -> None:
    """跑一轮采集（跑完即退出）。"""
    cfg = ctx.obj.cfg
    if source:
        cfg.as_dict()["crawler"]["source"] = source
    if column:
        cfg.as_dict()["crawler"]["column"] = column
    quiet = ctx.obj.as_json
    _setup_logging(cfg, ctx.obj.verbose, quiet)

    pipe = CrawlPipeline(cfg)

    def on_progress(event: str, payload: Dict[str, Any]) -> None:
        if ctx.obj.as_json or quiet:
            return
        if event == "page":
            ctx.obj.console.print(
                f"  → 第 [cyan]{payload.get('page')}[/] 页 · {payload.get('cards')} 题"
            )
        elif event == "probe":
            ctx.obj.console.print(
                f"  [yellow]⤳ 死区探矿[/] 第 {payload.get('from_page')} → "
                f"{payload.get('to_page')} 页（连续空 {payload.get('empty_streak')} 页）"
            )

    try:
        result = pipe.run(
            pages=pages,
            start_page=start_page,
            max_questions=max_questions,
            fetch_detail=detail,
            reset_cursor=reset,
            progress=on_progress,
        )
    except KeyboardInterrupt:
        _fail("已被用户中断（已采数据均已入库，下次运行会自动续爬）", EXIT_FAIL)
        return
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("k12.cli").exception("采集失败")
        _fail(f"采集失败：{exc}", EXIT_FAIL)
        return

    def render(d: Dict[str, Any]) -> None:
        c = ctx.obj.console
        c.print()
        c.print(
            f"[bold green]✔ 采集完成[/]  用时 {d['elapsed_sec']}s  ·  原因：{d['stopped_by']}\n"
            f"   有效索引页 {d['pages_crawled']}（尝试 {d['pages_attempted']}，跳过死区 {d['pages_skipped']}，探矿跳跃 {d['probes']}）\n"
            f"   题卡片 {d['cards_seen']}  [green]新增 {d['inserted']}[/]  重复 {d['duplicated']}  错误 {d['errors']}\n"
            f"   库内累计 [bold]{d['stored_total']}[/] 题   下一页游标：{d['next_page']}"
            + (f"   分页上限：{d['max_index_page']}" if d.get("max_index_page") else "")
        )
        st = d.get("fetch_stats", {})
        c.print(f"   请求 {st.get('requests')}  成功 {st.get('ok')}  重试 {st.get('retries')}  流量 {human_size(st.get('bytes', 0))}")

    _out(ctx, result, render)
    raise typer.Exit(EXIT_OK)


@app.command("daemon")
def cmd_daemon(
    ctx: typer.Context,
    rounds: Optional[int] = typer.Option(None, "--rounds", help="跑 N 轮后退出（0=永久运行）"),
    interval: Optional[int] = typer.Option(None, "--interval", help="轮询间隔（分钟），覆盖配置"),
    pages: Optional[int] = typer.Option(None, "--pages", help="每轮最多翻多少索引页，覆盖配置"),
    max_questions: Optional[int] = typer.Option(None, "--max-questions", "-n", help="每轮最多入库多少题，覆盖配置"),
) -> None:
    """常驻守护进程：按配置自动增量采集，无人值守。"""
    cfg = ctx.obj.cfg
    if interval:
        cfg.as_dict()["schedule"]["interval_minutes"] = int(interval)
    if rounds is not None:
        cfg.as_dict()["schedule"]["max_rounds"] = int(rounds)
    if pages is not None:
        cfg.as_dict()["crawler"]["max_pages"] = int(pages)
    if max_questions is not None:
        cfg.as_dict()["crawler"]["max_questions"] = int(max_questions)
    if not cfg.get("schedule.enabled", True):
        _fail("配置中 schedule.enabled = false，守护进程已关闭。请先启用。", EXIT_USAGE)

    def on_round(result: Dict[str, Any]) -> None:
        if not ctx.obj.as_json:
            ctx.obj.console.print(
                f"[dim]{time.strftime('%H:%M:%S')}[/] 第 {result.get('round')} 轮 · "
                f"新增 {result.get('inserted')} · 库内 {result.get('stored_total')}"
            )

    code = run_daemon(cfg, on_round=on_round)
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
@app.command("status")
def cmd_status(ctx: typer.Context) -> None:
    """查看数据仓库状态：总量、学科分布、题型分布、断点。"""
    cfg = ctx.obj.cfg
    store = SQLiteStore(cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"))
    stats = store.stats()
    store.close()

    def render(d: Dict[str, Any]) -> None:
        from rich.table import Table

        c = ctx.obj.console
        c.print(
            f"[bold]题库存量[/]  {d['total']} 题 · {d['db_size']}  ·  {d['db_path']}\n"
            f"  含答案 {d['with_answer']} ({d['with_answer_pct']}%)  ·  "
            f"含解析 {d['with_analysis']} ({d['with_analysis_pct']}%)  ·  含图 {d['with_images']}\n"
            f"  下一页游标：{d['next_page']}"
        )
        for key, title in (("by_subject", "学科分布"), ("by_grade", "学段分布"), ("by_qtype", "题型分布")):
            t = Table(title=title, show_header=True, header_style="bold cyan")
            t.add_column("分类")
            t.add_column("数量", justify="right")
            for k, v in (d.get(key) or {}).items():
                t.add_row(str(k), str(v))
            c.print(t)
        if d.get("last_run"):
            c.print(f"最近一次运行：{d['last_run']}")

    _out(ctx, stats, render)


@app.command("search")
def cmd_search(
    ctx: typer.Context,
    keyword: str = typer.Argument("", help="关键词（题干/答案/解析/来源）"),
    subject: str = typer.Option("", "--subject", help="按学科过滤"),
    grade: str = typer.Option("", "--grade", help="按学段过滤"),
    qtype: str = typer.Option("", "--qtype", help="按题型过滤"),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """检索题库。"""
    cfg = ctx.obj.cfg
    store = SQLiteStore(cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"))
    rows = store.search(keyword, subject, grade, qtype, limit=limit)
    store.close()

    def render(d: Dict[str, Any]) -> None:
        for i, q in enumerate(d["results"], 1):
            ctx.obj.console.print(
                f"[cyan]{i}. [{q['qid']}][/] {q.get('subject') or '?'} · {q.get('qtype') or '?'} · {q.get('paper') or ''}"
            )
            ctx.obj.console.print("   " + (q.get("stem") or "")[:160].replace("\n", " "))
            if q.get("options"):
                ctx.obj.console.print("   " + " | ".join(o[:40] for o in q["options"][:4]))
            if q.get("answer"):
                ctx.obj.console.print(f"   [green]答案[/]: {q['answer'][:100]}")
            ctx.obj.console.print()

    _out(ctx, {"count": len(rows), "results": rows}, render)


@app.command("show")
def cmd_show(ctx: typer.Context, qid: str = typer.Argument(..., help="题目 ID，如 jiajiao1010:20")) -> None:
    """查看单题完整内容。"""
    cfg = ctx.obj.cfg
    store = SQLiteStore(cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"))
    rows = store.search("", "", "", "", limit=1) if not qid else _fetch_by_qid(store, qid)
    store.close()
    if not rows:
        _fail(f"未找到题目 {qid}", EXIT_FAIL)
    _out(ctx, rows[0])


def _fetch_by_qid(store: SQLiteStore, qid: str) -> List[Dict[str, Any]]:
    conn = store._conn()  # noqa: SLF001
    row = conn.execute("SELECT * FROM questions WHERE qid=?", (qid,)).fetchone()
    if not row:
        return []
    from .storage import row_to_dict

    return [row_to_dict(row)]


@app.command("export")
def cmd_export(
    ctx: typer.Context,
    out_json: Optional[str] = typer.Option(None, "--out-json", help="导出 JSON 的路径"),
    out_jsonl: Optional[str] = typer.Option(None, "--out-jsonl", help="导出 JSONL 的路径"),
    limit: int = typer.Option(0, "--limit", help="只导出前 N 题（0=全部）"),
) -> None:
    """把题库导出成 JSON / JSONL。

    注意：这里刻意用 --out-json / --out-jsonl 而不是 --json，
    以免与全局的 --json（JSON 输出）冲突。
    """
    cfg = ctx.obj.cfg
    store = SQLiteStore(cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"))
    results: List[Dict[str, Any]] = []
    if out_json:
        results.append({"format": "json", **store.export_json(out_json, limit=limit)})
    else:
        default_json = cfg.path_of("storage.json_snapshot_dir", "data/json") / f"export-{now_stamp()}.json"
        results.append({"format": "json", **store.export_json(default_json, limit=limit)})
    if out_jsonl:
        results.append({"format": "jsonl", **store.export_jsonl(out_jsonl)})
    store.close()
    _out(ctx, {"exports": results}, lambda d: ctx.obj.console.print(
        "\n".join(f"[green]✔[/] {e['format']:5s} {e['count']:>6} 条 -> {e['path']} ({e['size']})" for e in d["exports"])
    ))


# ---------------------------------------------------------------------------
@app.command("parse")
def cmd_parse(
    ctx: typer.Context,
    input_path: str = typer.Argument(..., help="PDF / 图片 / 文本 / 目录 路径"),
    backbone: str = typer.Option("", "--backbone", "-b", help="使用哪个 VLM Backbone（默认取 models.default）"),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="结果 JSON 输出路径"),
    page_range: str = typer.Option("", "--pages-range", help='PDF 页范围，如 "1-10"'),
    save: bool = typer.Option(False, "--save", help="同时写入题库"),
    formulas: bool = typer.Option(False, "--formulas", help="额外把公式图片转成 LaTeX（仅对含图片的输入有意义）"),
) -> None:
    """多模态解析：把 PDF / 图像 / 文本转成结构化题目（拓展要求 1）。"""
    cfg = ctx.obj.cfg
    converter = QuestionConverter(cfg, backbone=backbone)

    if not ctx.obj.as_json:
        info = converter.info()
        ctx.obj.console.print(
            f"[bold]多模态解析[/]  输入：{input_path}\n"
            f"  Backbone：{info['backbone']} ({info['model']} @ {info['base_url']})  视觉：{'✔' if info['vision'] else '—'}"
        )

    def on_progress(msg: str) -> None:
        if not ctx.obj.as_json:
            ctx.obj.console.print(f"  [dim]{msg}[/]")

    try:
        questions = converter.from_path(input_path, page_range=page_range, progress=on_progress)
    except FileNotFoundError as exc:
        _fail(str(exc), EXIT_USAGE)
        return
    except BackboneError as exc:
        _fail(str(exc), EXIT_FAIL)
        return
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("k12.cli").exception("解析失败")
        _fail(f"解析失败：{exc}", EXIT_FAIL)
        return

    if formulas and questions:
        fetcher = Fetcher(cfg)

        def fetch_bytes(url: str) -> Optional[bytes]:
            return fetcher.get_bytes(url)

        for i, q in enumerate(questions, 1):
            if q.images:
                on_progress(f"[{i}/{len(questions)}] 公式转 LaTeX（{len(q.images)} 张）")
                converter.convert_question_formulas(q, fetch_bytes)

    saved = {"inserted": 0, "duplicated": 0}
    if save and questions:
        store = SQLiteStore(
            cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"),
            cfg.path_of("storage.jsonl_path", "data/questions.jsonl"),
        )
        saved = store.save_many(questions)
        store.close()

    payload = {
        "ok": True,
        "input": str(input_path),
        "backbone": converter.info(),
        "count": len(questions),
        "saved": saved,
        "questions": [q.to_dict() for q in questions],
    }
    if out:
        p = Path(out)
        ensure_dir(p.parent)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["out"] = str(p)

    def render(d: Dict[str, Any]) -> None:
        c = ctx.obj.console
        c.print(f"\n[bold green]✔ 解析完成[/]  共 {d['count']} 道题" + (f" · 已入库 {d['saved']['inserted']}" if save else ""))
        if d.get("out"):
            c.print(f"  结果已写出：{d['out']}")
        for i, q in enumerate(d["questions"][:5], 1):
            c.print(f"\n[cyan]{i}.[/] {q['subject'] or '?'} · {q['qtype']}")
            c.print("   " + q["stem"][:180].replace("\n", " "))
            if q.get("options"):
                c.print("   " + " | ".join(o[:36] for o in q["options"][:4]))
            if q.get("answer"):
                c.print(f"   [green]答案[/]: {q['answer'][:120]}")
        if d["count"] > 5:
            c.print(f"\n[dim]… 其余 {d['count'] - 5} 道见 JSON 输出[/]")

    _out(ctx, payload, render)


@app.command("formulas")
def cmd_formulas(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", "-n", help="处理多少道含图的题"),
    backbone: str = typer.Option("", "--backbone", "-b", help="用于识别的 VLM Backbone"),
    qid: str = typer.Option("", "--qid", help="只处理指定题目"),
    source: str = typer.Option("", "--source", help="只处理某个站点"),
) -> None:
    """把已入库题目中的公式图片转成 LaTeX 并回写题干。

    注意：本站的 <img> 不带尺寸信息，HTML 阶段无法区分公式与插图，
    因此这里按"有图且题干还没转成 LaTeX"来选候选，真正的判别在下载后按尺寸完成。
    """
    cfg = ctx.obj.cfg
    store = SQLiteStore(
        cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"),
        cfg.path_of("storage.jsonl_path", "data/questions.jsonl"),
    )
    converter = QuestionConverter(cfg, backbone=backbone)
    fetcher = Fetcher(cfg)

    if qid:
        rows = _fetch_by_qid(store, qid)
    else:
        sql = (
            "SELECT * FROM questions WHERE images_json NOT IN ('', '[]') "
            "AND stem NOT LIKE '%$%'"
        )
        args: List[Any] = []
        if source:
            sql += " AND source = ?"
            args.append(source)
        sql += " LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in store._conn().execute(sql, args).fetchall()]  # noqa: SLF001
    from .models import Question

    fixed = 0
    for row in rows:
        q = Question.from_dict(row)
        before = (q.stem, q.answer, q.analysis)
        converter.convert_question_formulas(q, fetcher.get_bytes)
        # 公式占位符也可能落在答案/解析里，因此三处都要比。
        if (q.stem, q.answer, q.analysis) != before:
            store.replace_question(q)
            fixed += 1
            if not ctx.obj.as_json:
                ctx.obj.console.print(f"  ✔ {q.qid}  {before[0][:60]}… → {q.stem[:60]}…")
    store.close()
    _out(ctx, {"processed": len(rows), "updated": fixed, "backbone": converter.info()},
         lambda d: ctx.obj.console.print(f"[green]✔[/] 处理 {d['processed']} 题，更新 {d['updated']} 题"))


# ---------------------------------------------------------------------------
@app.command("reparse")
def cmd_reparse(
    ctx: typer.Context,
    source: str = typer.Option("", "--source", help="只重解析某个站点（默认全部）"),
    limit: int = typer.Option(0, "--limit", "-n", help="最多处理 N 题（0=全部）"),
    only_empty: bool = typer.Option(False, "--only-empty", help="只救答案/解析为空的题"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只报告差异，不写库"),
) -> None:
    """用当前解析器重新解析已入库题目并回写。

    解析规则升级后不必清库重爬：这里按 source_url 重新取页、重跑解析，
    只把发生变化的题写回（qid / content_hash 保持不变）。
    """
    from urllib.parse import urljoin

    from .models import Question
    from .parser import parse_detail

    cfg = ctx.obj.cfg
    store = SQLiteStore(
        cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"),
        cfg.path_of("storage.jsonl_path", "data/questions.jsonl"),
    )
    fetcher = Fetcher(cfg)
    rows = store.fetch_rows(source=source, only_empty=only_empty, limit=limit)

    changed = unchanged = failed = 0
    samples: List[Dict[str, Any]] = []
    field_tally: Dict[str, int] = {}

    def _same_json(a: Any, b: Any) -> bool:
        return json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(
            b, ensure_ascii=False, sort_keys=True
        )

    for row in rows:
        url = row.get("source_url") or ""
        html = fetcher.get(url, allow_404=True) if url else None
        detail = parse_detail(html, url, lambda u, base=url: urljoin(base, u)) if html else None
        if not detail or not detail.get("stem"):
            failed += 1
            continue

        q = Question.from_dict(row)
        q.stem = detail.get("stem") or q.stem
        q.options = detail.get("options") or q.options
        q.answer = detail.get("answer") or ""
        q.analysis = detail.get("analysis") or ""
        if detail.get("images"):
            q.images = detail["images"]
        q.subject = detail.get("subject") or q.subject
        q.subject_code = detail.get("subject_code") or q.subject_code
        q.grade = detail.get("grade") or q.grade

        # 逐字段比对：题干、选项、图片都可能被解析器升级改变，不能只看答案。
        diffs = [
            name
            for name, old, new in (
                ("stem", row.get("stem") or "", q.stem),
                ("answer", row.get("answer") or "", q.answer),
                ("analysis", row.get("analysis") or "", q.analysis),
                ("options", row.get("options") or [], q.options),
                ("images", row.get("images") or [], q.image_dicts),
            )
            if not _same_json(old, new)
        ]
        if not diffs:
            unchanged += 1
            continue

        changed += 1
        for name in diffs:
            field_tally[name] = field_tally.get(name, 0) + 1
        if len(samples) < 5:
            samples.append(
                {
                    "qid": row["qid"],
                    "fields": diffs,
                    "stem_before": (row.get("stem") or "")[:60],
                    "stem_after": q.stem[:60],
                    "analysis_before": (row.get("analysis") or "")[:60],
                    "analysis_after": q.analysis[:60],
                }
            )
        if dry_run:
            continue

        store.replace_question(q)
        if not ctx.obj.as_json:
            ctx.obj.console.print(f"  ✔ {row['qid']}  变更字段 {diffs}")

    store.close()
    summary = {
        "scanned": len(rows),
        "changed": changed,
        "unchanged": unchanged,
        "failed": failed,
        "dry_run": dry_run,
        "fields": field_tally,
        "samples": samples,
    }
    _out(
        ctx,
        summary,
        lambda d: ctx.obj.console.print(
            f"[green]✔[/] 重解析完成：扫描 {d['scanned']} · 变更 {d['changed']} · "
            f"未变 {d['unchanged']} · 失败 {d['failed']}"
            + ("（dry-run，未写库）" if d["dry_run"] else "")
        ),
    )


# ---------------------------------------------------------------------------
@app.command("benchmark")
def cmd_benchmark(
    ctx: typer.Context,
    column: str = typer.Option("gzsx", "--column", help="按学科代码取题（gzsx=高中数学）"),
    target: int = typer.Option(1400, "--target", "-t", help="目标条数（0=全量不抽样）"),
    out: str = typer.Option("data/benchmark", "--out", help="输出目录"),
) -> None:
    """把已入库题目按标注规范构造成 Benchmark 数据集。"""
    from .benchmark import build

    cfg = ctx.obj.cfg
    db_path = cfg.path_of("storage.sqlite_path", "data/k12.sqlite3")
    out_dir = Path(out)
    if not out_dir.is_absolute():
        out_dir = Path.cwd() / out_dir

    try:
        stats = build(db_path, out_dir, subject_code=column, target=target)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("k12.cli").exception("Benchmark 构建失败")
        _fail(f"Benchmark 构建失败：{exc}", EXIT_FAIL)
        return

    def render(d: Dict[str, Any]) -> None:
        c = ctx.obj.console
        c.print()
        c.print(
            f"[bold green]✔ Benchmark 构建完成[/]  "
            f"原始 {d['raw_total']} → 可用 {d['kept']} → 入选 [bold]{d['sampled']}[/]"
        )
        if d.get("rejected"):
            c.print(f"   过滤：{d['rejected']}")
        c.print("\n[bold]板块分布[/]")
        for k, v in d["board"].items():
            c.print(f"   {k:<14} {v:>5}  {v * 100 // max(1, d['sampled']):>3}%")
        c.print("\n[bold]难度分布[/]  " + "  ".join(f"{k} {v}" for k, v in d["difficulty"].items()))
        cuts = d.get("difficulty_cuts", {})
        c.print(f"   [dim]切点：P40={cuts.get('p40')}  P80={cuts.get('p80')}[/]")
        c.print("[bold]答案类型[/]  " + "  ".join(f"{k} {v}" for k, v in d["answer_type"].items()))
        c.print("[bold]子集[/]      " + "  ".join(f"{k} {v}" for k, v in d.get("subset", {}).items()))
        c.print(f"   含解析：{d.get('with_analysis', 0)} / {d['sampled']}")
        c.print(f"\n   数据文件：{d['jsonl']}")

    _out(ctx, stats, render)


# ---------------------------------------------------------------------------
@app.command("doctor")
def cmd_doctor(ctx: typer.Context, check_models: bool = typer.Option(False, "--check-models", help="同时自检模型连通性")) -> None:
    """环境体检：依赖、目录、数据库、站点连通性、模型可用性。"""
    cfg = ctx.obj.cfg
    report: Dict[str, Any] = {"ok": True, "checks": []}

    def add(name: str, ok: bool, detail: str) -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            report["ok"] = False

    # 依赖（PyMuPDF 优先用新模块名 pymupdf，旧名 fitz 会打弃用告警）
    for mod, pkg in (("requests", "requests"), ("bs4", "beautifulsoup4"), ("yaml", "PyYAML"),
                     ("typer", "typer"), ("rich", "rich"), ("apscheduler", "APScheduler"),
                     ("openai", "openai"), ("pymupdf", "PyMuPDF"), ("PIL", "pillow")):
        try:
            __import__(mod)
            add(f"依赖 {pkg}", True, "已安装")
        except ImportError:
            try:
                __import__("fitz" if mod == "pymupdf" else mod)
                add(f"依赖 {pkg}", True, "已安装（旧模块名）")
            except ImportError:
                add(f"依赖 {pkg}", False, f"缺失，请 pip install {pkg}")

    # 目录与数据库
    for p in (cfg.data_dir, cfg.log_dir):
        try:
            ensure_dir(p)
            add(f"目录 {p.name}", True, str(p))
        except OSError as exc:
            add(f"目录 {p.name}", False, str(exc))

    try:
        store = SQLiteStore(cfg.path_of("storage.sqlite_path", "data/k12.sqlite3"))
        n = store.count()
        store.close()
        add("SQLite", True, f"{cfg.path_of('storage.sqlite_path', '')} 现有 {n} 题")
    except Exception as exc:  # noqa: BLE001
        add("SQLite", False, str(exc))

    # 站点
    fetcher = Fetcher(cfg)
    spider = get_spider(cfg.get("crawler.source", "jiajiao1010"), cfg, fetcher)
    html = fetcher.get(spider.index_url(cfg.get("crawler.start_page", 1)), allow_404=True)
    if html:
        cards = spider.parse_index(html, spider.index_url(1))
        add("站点连通", True, f"{spider.base_url} 首页解析出 {len(cards)} 张题卡片")
    else:
        add("站点连通", False, f"无法访问 {spider.base_url}")

    # 模型
    if check_models:
        for name in cfg.backbone_names():
            try:
                h = build_client(cfg, name).health_check()
                add(f"Backbone {name}", bool(h.get("ok")), h.get("reply") or h.get("error", ""))
            except Exception as exc:  # noqa: BLE001
                add(f"Backbone {name}", False, str(exc)[:160])

    def render(d: Dict[str, Any]) -> None:
        c = ctx.obj.console
        for chk in d["checks"]:
            c.print(("[green]✔[/] " if chk["ok"] else "[red]✖[/] ") + f"{chk['name']:<24} {chk['detail']}")
        c.print("\n" + ("[bold green]体检通过[/]" if d["ok"] else "[bold yellow]存在问题，见上方 ✖ 项[/]"))

    _out(ctx, report, render)
    if not report["ok"]:
        raise typer.Exit(EXIT_FAIL)


@app.command("selftest")
def cmd_selftest(
    ctx: typer.Context,
    backbone: str = typer.Option("", "--backbone", "-b", help="指定 Backbone；留空则用默认，不可用时自动回落到离线自检"),
    out_dir: Optional[str] = typer.Option(None, "--out-dir", help="素材与报告的输出目录"),
) -> None:
    """端到端自检：合成试卷 -> 多模态解析 -> 校验输出规范（无需 VLM 也能跑通）。"""
    from .selftest import run_selftest

    cfg = ctx.obj.cfg
    report = run_selftest(cfg, backbone=backbone, out_dir=Path(out_dir) if out_dir else None)

    def render(d: Dict[str, Any]) -> None:
        c = ctx.obj.console
        info = d.get("backbone", {})
        c.print(
            f"[bold]端到端自检[/]  Backbone：{info.get('name')} "
            f"({info.get('model')} @ {info.get('base_url')})"
        )
        if d.get("backbone_fallback"):
            c.print(f"  [yellow]⚠ {d['backbone_fallback']}[/]")
        for s in d["steps"]:
            mark = "[green]✔[/]" if s.get("ok") else "[red]✖[/]"
            c.print(f"  {mark} {s['step']:<12} 解析 {s.get('count', '-')} 题  {(s.get('sample_stem') or s.get('latex') or '')[:70]}")
            for p in s.get("problems", []) or []:
                c.print(f"      [red]· {p}[/]")
        sr = d.get("storage_roundtrip", {})
        if sr.get("inserted") is not None:
            c.print(f"  [green]✔[/] 落库回归     写入 {sr['inserted']} 题 -> {sr['db']}")
        c.print(f"\n  素材目录：{d['work_dir']}")
        c.print("[bold green]✔ " + d["summary"] + "[/]" if d["ok"] else "[bold red]✖ " + d["summary"] + "[/]")

    _out(ctx, report, render)
    if not report["ok"]:
        raise typer.Exit(EXIT_FAIL)


# ---------------------------------------------------------------------------
# Agent 体验修正：让全局开关也能写在子命令之后
#   click 默认只接受「k12crawler --json status」这种"开关在前"的写法，
#   但 Agent 与人类都更习惯「k12crawler status --json」。这里在进入
#   typer 之前把可安全搬移的全局开关挪到前面。
#   注意：只有"纯开关"才能搬；带值的 -c/--config 不在此列。
# ---------------------------------------------------------------------------
_GLOBAL_FLAGS = {"--json", "-v", "--verbose", "-q", "--quiet"}


def normalize_argv(argv: List[str]) -> List[str]:
    """把写在子命令后面的全局开关挪到最前面。"""
    if len(argv) <= 1:
        return argv
    moved: List[str] = []
    rest: List[str] = []
    for arg in argv[1:]:
        (moved if arg in _GLOBAL_FLAGS else rest).append(arg)
    return [argv[0], *moved, *rest]


def main(argv: Optional[List[str]] = None) -> None:
    """真正的命令行入口（供 `python -m k12crawler` 与 console_script 使用）。"""
    argv = list(sys.argv if argv is None else argv)
    sys.argv = normalize_argv(argv)
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
