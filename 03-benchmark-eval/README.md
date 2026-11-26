# 交付包：高中数学 LLM 评测数据集 + 评测脚本

本文件夹**自带数据集与脚本，可独立运行**，不依赖外面的采集工程。

---

## 一、里面有什么

```
交付_数据集与评测脚本/
├── README.md                       ← 本文件
├── dataset/
│   ├── benchmark_v3.template.jsonl    ★ 数据集 4,000 条（JSON 规范 v1.0.0，一行一题）
│   ├── stats_v3.json                  分布与均衡统计
│   ├── quality_report.md              质检漏斗与六道闸通过率
│   ├── spec_v3.json                   板块关键词、难度与质检规则
│   ├── review_sample.csv              200 条人工抽检清单
│   ├── template_validation.json       格式校验汇总（4,000/4,000）
│   ├── image_manifest.json            图片本地化结果（149 条 / 233 张）
│   ├── assets/images/                 本地图片（img_url 指向这里）
│   └── charts/                        汇报图（板块堆叠、年份、漏斗）
├── eval/
│   ├── evaluate.py                    批量评测入口
│   ├── providers.py                   API / HF / llama.cpp / mock 四类后端
│   ├── prompts.py                     Prompt 模板与答案抽取
│   ├── metrics.py                     准确率、置信区间、切片
│   ├── compare.py                     多模型对比表
│   ├── config.yaml                    可直接运行的配置（已指向本包数据集）
│   ├── config.example.yaml            配置模板（换成 API/本地模型时改它）
│   ├── tests/test_eval.py             单元自测
│   └── results/                       一次完整示例评测（mock，仅验证链路）
└── docs/
    ├── 数据集说明.md                  规模、字段、清洗、难度口径、偏差
    ├── 数据格式说明.md                JSON 模板字段映射与校验
    ├── 评测说明.md                    指标定义、结果展示口径
    ├── 脚本使用说明.md                采集/构建/评测完整命令
    └── 第三次汇报要点.md              10 页讲法与数字
```

数据集：4,000 条四选一，函数 / 数列 / 立体几何 / 解析几何 / 概率与统计各 740 条，导数单列 300 条。

---

## 二、跑起来（30 秒）

```bash
cd eval
python evaluate.py --provider mock --tag smoke --limit 100
```

依赖：`pip install -r requirements.txt`（requests、PyYAML；本地模型另装 transformers / llama-cpp-python）。

---

## 三、两种测试方式（只换提示词，不换题）

| 方式 | 命令 | 提示词 | 调用次数 |
|---|---|---|---|
| 常规测试 | `--mode normal` | 一次作答，最后一行给答案 | 1 |
| 深度思考测试 | `--mode deep` | 固定五步推理 + 固定复核轮，独立重算纠错 | 默认 2，可 `--deep-rounds 3` |

```bash
cd eval
python evaluate.py --provider mock --mode normal --tag demo-normal
python evaluate.py --provider mock --mode deep   --tag demo-deep
python compare.py --results results --out results/leaderboard.md
```

最终输出**正确率**（含 95% 置信区间、六板块宏平均、板块×难度矩阵、未抽取率、平均调用次数）。
两种方式的差值就是"深度思考"的净收益，成本看调用次数与耗时。

---

## 四、跑真模型

```bash
cd eval

# OpenAI 兼容端点（OpenAI / DeepSeek / 通义 / 智谱 / vLLM / Ollama）
export DEEPSEEK_API_KEY=sk-xxx
python evaluate.py --provider openai --model deepseek-chat \
    --base-url https://api.deepseek.com/v1 --tag deepseek

# 本地 transformers
python evaluate.py --provider hf --model-path /models/Qwen2.5-7B-Instruct --tag qwen7b

# 本地 GGUF
python evaluate.py --provider llamacpp --model-path /models/qwen2.5-7b-q4_k_m.gguf --tag qwen7b-gguf

# 汇总成对比表
python compare.py --results results --out results/leaderboard.md
```

常用筛选：`--subset core`（只跑纯文字）、`--board 导数及其应用`、`--difficulty 压轴`、
`--split dev`（调 Prompt 用）、`--limit N`（冒烟）、`--resume`（续跑）、`--dry-run`（只印 prompt）。

---

## 五、产物

```
results/
├── results_<tag>.jsonl   逐题：pred / gold / correct / 抽取方式 / 耗时 / 原始输出
├── summary_<tag>.json    全量指标（含各切片与 6×3 矩阵）
├── summary_<tag>.md      可直接贴进汇报的结果页
├── per_item_<tag>.csv    逐题表（Excel 可开）
└── matrix_<tag>.csv      板块 × 难度矩阵
```

`results/` 里放了两份完整示例（mock 后端）：`demo-normal` 与 `demo-deep`，
`meta.mock = true`，**不是模型成绩**，只用来确认表格长什么样、两种方式差在哪。

---

## 六、数据怎么读

```python
import json

rows = [json.loads(l) for l in open("dataset/benchmark_v3.template.jsonl", encoding="utf-8")]
q = rows[0]
print(q["question"])                    # 题干
print(q["choices"])                     # {"A": "...", "B": "...", "C": "...", "D": "..."}
print(q["answers"])                     # ["B"]
print(q["metadata"]["content"]["difficulty"])   # easy / medium / hard
print(q["metadata"]["extensions"]["board"])     # 板块
```

字段全表见 `docs/数据格式说明.md`。

---

## 七、运行注意（审计后加固）

- 同 tag 重跑会**覆盖**旧明细；要续跑加 `--resume`；
- 非本地端点缺 API Key 会直接退出（exit 2），网络不通在连通性探测阶段退出（exit 3）；
- 输出被截断（`finish_reason=length`）且没有明确「答案：X」时，记为未抽取，不猜正文里的字母；
- deep 的复核轮**不把上一轮答案写进提示词**，避免锚定；summary 单列「多轮改判」统计；
- 离线环境建议 `--subset core`（core 3,811；extended 189 中 149 条图片已本地化、40 条源页无图）；
- `dataset/review_sample.csv` 是 200 条人工抽检清单（160 纯文字 + 40 图片题），
  填完「人工难度」「是否可解」后跑 `python eval/check_review.py` 可得一致率与 Cohen's kappa。

## 八、已知边界（答辩要主动说）

1. 图片：149 条已本地化（`dataset/assets/images/`），40 条源页无图，纯文本不可解；
2. 年份/试卷名覆盖低（4.2%），3,833 条按规范标 `needs_review`；
3. 导数只有 300 条（四选一供给有限），其余五个板块各 740 条、极差 0；
4. 难度是规则初标，人工复标清单在 `dataset/review_sample.csv`。

---

## 九、路径对照

`docs/` 里的说明是按开发目录写的，对应关系：

| 文档中的路径 | 本包中的位置 |
|---|---|
| `crawler/data/benchmark_v3/...` | `dataset/` |
| `eval/...` | `eval/` |
| `docs/...` | `docs/` |

只做评测的话，看本 README 与 `docs/评测说明.md` 即可；
复现采集与构建流程，看 `docs/脚本使用说明.md`（需要完整的 `mathbench_v3/` 工程）。
