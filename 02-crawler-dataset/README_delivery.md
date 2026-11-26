# 第二次汇报：爬虫与 1,400 条数据集

> 本目录保留第二次汇报的原始交付：采集程序 + 当时的数据集。
> 第三次汇报在此基础上扩到 4,000 条并新增评测脚本，见 `../03-benchmark-eval/`。

# 高中数学 Benchmark · 爬虫与数据集交付包

本包包含两部分：**采集程序**（可独立运行、可无人值守）与**已经采好的数据集**。
数据集为高中数学单选题，共 **1,400 条**，全部答案可自动判定。

---

## 一、目录结构

```
交付_爬虫与数据集/
├── crawler/                     采集程序源码
│   ├── k12crawler/              主程序包
│   │   ├── cli.py               命令行入口（全部命令支持 --json）
│   │   ├── pipeline.py          采集流水线（并发 / 断点 / 死区探矿）
│   │   ├── scheduler.py         无人值守守护进程
│   │   ├── fetcher.py           HTTP 层（限速 / 重试 / GBK / UA）
│   │   ├── benchmark.py         Benchmark 标注与抽样（本次数据集就是它产出的）
│   │   ├── parser/              HTML → 题目
│   │   ├── spiders/             站点适配器（三条路线）
│   │   ├── storage/             SQLite + JSONL
│   │   ├── llm/                 VLM/LLM Backbone 注册表
│   │   └── multimodal/          PDF / 图像 / 文本 → 题目
│   ├── tests/                   56 项单元测试（离线可跑）
│   ├── skills/k12-crawler/      Agent Skill 说明
│   ├── config/config.example.yaml  配置模板
│   ├── requirements.txt
│   └── pyproject.toml
└── data/
    ├── benchmark/
    │   ├── benchmark.jsonl      ★ 1,400 条评测数据集（主交付物）
    │   ├── stats.json           分布统计（板块 / 难度 / 子集 / 过滤明细）
    │   └── spec.json            标注规范（板块关键词、难度规则）
    ├── k12.sqlite3              原始题库（2,155 题，**仅高中数学 gzsx**）
    └── questions.jsonl          原始题库的 JSONL 导出（与 sqlite 同源，2,155 行）
```

---

## 二、数据集说明（`data/benchmark/benchmark.jsonl`）

每行一道题，字段如下：

| 字段 | 说明 |
|---|---|
| `id` | 全局唯一 ID，形如 `jiajiao1010-subject:s<md5>` |
| `board` | 知识板块（六类：函数 / 导数及其应用 / 数列 / 立体几何 / 解析几何 / 概率与统计） |
| `difficulty` | 基础 / 中档 / 压轴 |
| `difficulty_score` | 难度连续分值（0–8），便于自行重切阈值 |
| `stem` | 题干（公式以 `^` / `_` 记号保留幂次与下标） |
| `options` | 四个选项，形如 `["A. 3 对", …]` |
| `answer` | 答案字母，单个 `A`–`D` |
| `analysis` | 解析（65% 的题有，仅用于错因分析，不参与判分） |
| `has_formula_image` / `subset` | 是否含公式图；`core`（1,376）/ `extended`（24） |
| `source_url` | 原页面地址，便于溯源 |

**规模核对**：原始采集 2,155 → 剔除 660（板块证据不足 590、重复 17、答案不规范 35、无答案 5、非选择 6、题干过短 5、图片过多 1、串科 1）→ 可判分 1,495 → 分层抽样 **1,400**。

> 本包**只含高中数学（gzsx）**：`data/k12.sqlite3` 里是 2,155 道高中数学题，
> `benchmark.jsonl` 的 1,400 条即从这批题中筛出。库里不含初中或其他学科。

**分布**（六大板块，沿用第一阶段口径）：函数 639 / 概率与统计 201 / 数列 196 / 立体几何 185 /
解析几何 149 / 导数及其应用 30；基础 555 / 中档 550 / 压轴 295。
三角函数与集合/常用逻辑并入「函数」，计数原理并入「概率与统计」；不属于这六块的题不入库。

---

## 三、爬虫使用方式

### 1. 准备环境

```bash
cd crawler
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 初始化与体检

```bash
python -m k12crawler init          # 生成 config/config.yaml 与数据目录
python -m k12crawler doctor        # 依赖 / 目录 / 数据库 / 站点连通性
```

### 3. 采集

```bash
# 高中数学（gzsx），从第 1 页开始抓 50 页
python -m k12crawler crawl --source jiajiao1010-subject --column gzsx --pages 50

# 换学科只改 --column：czsx 初中数学 / gzwl 高中物理 / czhx 初中化学 …
python -m k12crawler crawl --column czwl --pages 20
```

- 断点自动保存：中断后重跑会从上次的页码继续，不会重复也不会漏页；
- 不同学科**各自独立断点**（游标键含学科名）；
- 加 `--json` 输出结构化结果，便于脚本消费；退出码 0 成功 / 1 运行失败 / 2 参数错误。

### 4. 无人值守

```bash
python -m k12crawler daemon --interval 30      # 每 30 分钟自动增量采集
python -m k12crawler daemon --rounds 2         # 只跑两轮后退出，用于验证
```

### 5. 生成 Benchmark 数据集

```bash
python -m k12crawler benchmark --column gzsx --target 1400 --out data/benchmark
```

会输出 `benchmark.jsonl`、`stats.json`、`spec.json`。`--target 0` 表示不抽样、全量导出。

### 6. 其他命令

```bash
python -m k12crawler status        # 题库统计
python -m k12crawler search "椭圆"  # 检索
python -m k12crawler export --out-json out.json --out-jsonl out.jsonl
python -m k12crawler reparse --source jiajiao1010-subject   # 解析规则升级后回填
python -m pytest tests/ -q         # 56 项单元测试
```

---

## 四、数据来源与合规

- 来源站点：精英家教网 `www.1010jiajiao.com`，仅采集公开可访问页面；
- 采集遵守 `robots.txt`，默认请求间隔 0.6 秒、并发 4、失败退避重试；
- 数据仅用于课程研究与模型评测，请勿商用；题目版权归原站点与原作者所有。

---

## 五、已知限制

1. 题目多为 2005–2013 年各地高考与统考卷，年份偏旧，尚未补入近年真题；
2. 未做人工改编，存在与模型训练语料重叠的可能（防污染靠去重与子集划分控制）；
3. 解析覆盖 61%，其余为站点该页本身未提供；
4. 知识板块与难度均为**规则标注**，非人工逐条标注；难度留待期中用多模型正确率校准。
