---
name: k12-crawler
description: |
  K12 试题数据爬虫。自动从精英家教网(1010jiajiao.com)增量采集小学/初中/高中
  全学科试题（题干、选项、答案、解析、公式图片），存入 SQLite + JSONL；
  支持无人值守定时采集；并可用本地/云端 VLM 把 PDF、图像、文本解析成结构化题目。
  当用户提到「爬题」「采集试题」「K12 题库」「试卷解析」「把 PDF 卷子变成题目」
  「图片转题目」「题库检索/导出」「批量 OCR 取题」时使用本 Skill。
  触发词：爬题、试题爬虫、K12题库、试卷解析、PDF转题目、图片转题目、题库导出、
  试题采集、k12 crawler。
---

# K12 试题数据爬虫

## 这个 Skill 能做什么

| 能力 | 命令 | 说明 |
|---|---|---|
| 增量采集试题 | `k12crawler crawl` | 从上次断点续爬，边抓边入库 |
| 换采集路线/学科 | `k12crawler crawl --source … --column …` | 三条路线任选，学科栏目路线可定向某学科 |
| 无人值守采集 | `k12crawler daemon` | 常驻进程，按间隔自动跑，**无需人工触发** |
| 查看题库状况 | `k12crawler status` | 总量 / 学科 / 题型 / 答案覆盖率 / 断点 |
| 检索题目 | `k12crawler search` | 按关键词、学科、学段、题型过滤 |
| 导出数据 | `k12crawler export` | 导出 JSON / JSONL 供下游消费 |
| 用新解析器修数据 | `k12crawler reparse` | 解析规则升级后回填历史数据，不必清库重爬 |
| 多模态取题 | `k12crawler parse` | PDF / 图像 / 文本 → 结构化题目（走 VLM） |
| 公式转 LaTeX | `k12crawler formulas` | 把题目里的公式图片转成 LaTeX 回写题干 |
| 切换 VLM 骨干 | `k12crawler backbones` | 列出 / 自检所有可配置 Backbone |
| 端到端自检 | `k12crawler selftest` | 无需 VLM 也能验证整条链路 |
| 环境体检 | `k12crawler doctor` | 依赖、目录、数据库、站点、模型连通性 |

## 三条采集路线（`--source`）

| 路线 | 命令 | 跳数 | 适用 |
|---|---|---|---|
| **学科栏目**（推荐） | `--source jiajiao1010-subject --column czsx` | 2 | 学科定向；详情页给【答案】+【解析】+【解法提示】 |
| 平铺习题索引 | `--source jiajiao1010` | 2 | 量最大最快；有【答案】但**无【解析】** |
| 试卷路线 | `--source jiajiao1010-paper` | 4 | 有完整解答过程；但列的是"相似题"，去重压力大 |

`--column` 可选值：`xxsx/xxyw/xxyy`（小学）、`czsx/czyw/czyy/czwl/czhx/czsw/czzz/czls/czdl`（初中）、
`gzsx/gzyw/gzyy/gzwl/gzhx/gzsw/gzzz/gzls/gzdl`（高中）。

## 前置：确定 Python 解释器

本项目依赖已装在项目自带环境里，**必须用下面这个解释器**，不要用系统 python：

```bash
PY="C:/Users/HP/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
```

> 如果该环境不存在，先执行：
> `python -m venv C:/Users/HP/.workbuddy/binaries/python/envs/default`
> 然后 `pip install -r requirements.txt`

工作目录固定为：

```bash
cd C:/Users/HP/WorkBuddy/2026-10-15-19-52-30/k12-question-crawler
```

## 核心约定（Agent 必读）

1. **所有命令都加 `--json`**，输出是严格可解析的 JSON，一行一个对象。
   Agent 直接 `json.loads` 即可，不需要正则抠屏。
2. **退出码**：`0` 成功 / `1` 运行失败 / `2` 参数或配置问题。
3. **不需要任何交互输入**，所有参数都能通过命令行或 `config/config.yaml` 给定。
4. 采集是**幂等**的：重复抓同一道题会被 content_hash 去重，不会污染数据。
5. 采集随时可以 Ctrl+C 中断，已入库的数据不丢，下次自动续爬。

---

## 常用工作流

### 1) 采集一批题目（最常用）

```bash
# 推荐：学科栏目路线，定向初中数学，带完整解析
"$PY" -m k12crawler crawl --source jiajiao1010-subject --column czsx \
    --pages 30 --max-questions 300 --json
```

换学科只改 `--column`，例如高中数学 `gzsx`、初中物理 `czwl`。

返回：

```json
{
  "run_id": 4, "pages_crawled": 30, "pages_attempted": 58, "pages_skipped": 28,
  "probes": 12, "cards_seen": 300, "inserted": 300, "duplicated": 0,
  "errors": 0, "next_page": 61, "stopped_by": "max_questions",
  "stored_total": 640, "elapsed_sec": 214.7,
  "fetch_stats": {"requests": 358, "ok": 358, "retries": 0}
}
```

关键字段：`inserted`（本次新增）、`stored_total`（库内总量）、`next_page`（下次的起点）。

> 每条路线的断点是**各自独立**的（游标 key 含 source 名），互不干扰。

### 2) 启动无人值守采集（后台常驻）

```bash
"$PY" -m k12crawler daemon --interval 30
```

- 每 30 分钟自动增量采集一轮，进程常驻，**建好之后不需要任何人工触发**。
- 想验证调度是否正常，用有限轮次：`--rounds 2`（跑两轮后自动退出）。
- 也可用系统级调度替代（Windows 任务计划 / cron / systemd）定时调用 `crawl`，
  参考 README 的「部署」章节。

### 3) 看题库现状

```bash
"$PY" -m k12crawler status --json
```

### 4) 找题 / 导出

```bash
"$PY" -m k12crawler search "二次函数" --grade 初中 --limit 5 --json
"$PY" -m k12crawler export --out-json data/export.json --out-jsonl data/export.jsonl --limit 500 --json
```

> 导出路径参数是 `--out-json` / `--out-jsonl`（不是 `--json`）——
> `--json` 是全局开关，表示"以 JSON 输出结果"，两者刻意区分以免冲突。

### 4b) 解析器升级后回填历史数据

```bash
"$PY" -m k12crawler reparse --source jiajiao1010-subject --dry-run --json   # 先看会改什么
"$PY" -m k12crawler reparse --only-empty --json                            # 只救答案/解析为空的题
```

- 按 `source_url` 重新取页、重跑解析，只把**真正变化**的题写回；
- `qid` 与 `content_hash` 保持不变，不影响去重与溯源；
- 返回里带 `fields`（各字段被改了多少条）与 `samples`（前 5 条差异），便于审计。

### 5) 把一份 PDF 试卷变成结构化题目（拓展要求 1）

```bash
"$PY" -m k12crawler parse ./试卷.pdf --backbone qwen3-vl-8b --out out.json --save --json
```

- `--backbone` 可换成任意已配置的 VLM（这就是拓展要求 2 的切换入口）。
- `--save` 会把解析结果一并写入题库。
- 支持：`.pdf` / `.png` `.jpg` 等图片 / `.txt` `.md` / 整个目录。
- `--formulas` 会额外把题目里的公式图转成 LaTeX。

### 6) 公式图片 → LaTeX

```bash
"$PY" -m k12crawler formulas --limit 20 --backbone qwen3-vl-8b --json
```

### 7) 切换 / 检查 VLM 骨干

```bash
"$PY" -m k12crawler backbones --json                # 列出全部
"$PY" -m k12crawler backbones --check --json        # 逐个连通性自检
```

### 8) 没有本地 VLM 时验证链路

```bash
"$PY" -m k12crawler selftest --json
```

自动合成试卷 → 跑通 PDF/图像/文本/公式/落库 五条链路 → 校验输出规范。
默认 Backbone 不可用时会自动回落到 `mock-offline`，并如实标注。

---

## 配置要点（`config/config.yaml`）

| 想改什么 | 改哪里 |
|---|---|
| 采集路线 | `crawler.source`（或命令加 `--source`） |
| 采集学科 | `crawler.column`（或命令加 `--column`） |
| 采集规模 | `crawler.max_pages` / `max_questions` |
| 采集频率 | `schedule.interval_minutes` |
| 采集时段 | `schedule.window: "01:00-06:00"` |
| 是否抓答案 | `crawler.fetch_detail` |
| 请求礼雾 | `crawler.delay` / `concurrency` / `respect_robots` |
| 换 VLM | `models.default`，或给命令加 `--backbone` |
| 接私有模型 | 在 `models.backbones` 里加一条，`base_url` 指向你的端点 |
| 登录态站点 | `crawler.account.username/password/cookie` |

## 输出数据字段

每条题目包含：
`qid, source, source_url, subject, subject_code, grade, qtype, paper, year,
stem, stem_html, options[], answer, analysis, images[], tags[], difficulty,
parser, content_hash, crawled_at`

题干中的内嵌公式以 `{{img:n}}` 占位，对应 `images[n].url`；
经 `formulas` 命令或 `parse --formulas` 处理后，占位符会被替换成 LaTeX。

## 注意事项

- **合规**：默认遵守 robots.txt、请求间隔 0.6s、并发 4、UA 轮换。请勿把 `delay`
  调到 0，也不要并发超过 8，避免给对方站点造成压力。
- 本 Skill 抓取的是公开页面，答案字段来自页面公开内容，仅供学习研究使用。
- 遇到「无法访问站点」：先跑 `k12crawler doctor`，它会告诉你是依赖、网络还是配置问题。
