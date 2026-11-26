# 高中数学 LLM 解题能力评测基准

课程项目：Educational LLM Benchmark。做一份**能自动判分**的高中数学选择题集，
用来横向比较不同大模型的解题能力，并把"常规作答"与"深度思考"两种测试方式分开跑。

仓库按三次汇报分目录，**每次汇报的交付物原样保留**，方便看演进过程。

---

## 目录：三次汇报对应关系

| 目录 | 汇报阶段 | 内容 |
|---|---|---|
| [`01-proposal/`](01-proposal/) | 第一次：选题 | 选题与方案（材料待补） |
| [`02-crawler-dataset/`](02-crawler-dataset/) | 第二次：数据采集与数据集 | 采集程序（1010jiajiao）+ 1,400 条数据集 |
| [`03-benchmark-eval/`](03-benchmark-eval/) | 第三次：数据集完成 + 评测 | 4,000 条数据集、评测脚本、真实评测记录 |

三次的口径没有变：**只收四选一、只算准确率、判分不过模型**。
变的是规模（1,400 → 4,000）、来源（单站 → 两站三路）、以及是否带评测工具。

---

## 数据规模

| 阶段 | 规模 | 说明 |
|---|---|---|
| 第二次 | 1,400 条 | 单站（1010jiajiao），六板块 |
| 第三次 | **4,000 条** | 函数 / 数列 / 立体几何 / 解析几何 / 概率与统计各 740 条，导数单列 300 条 |

第三次数据集按团队《高考题目数据集 JSON 收集规范 v1.0.0》输出：
`03-benchmark-eval/dataset/benchmark_v3.template.jsonl`，4,000 条全部通过格式校验。

---

## 快速开始（评测）

```bash
cd 03-benchmark-eval
pip install -r requirements.txt

# 冒烟：不调模型，验证链路
python eval/evaluate.py --provider mock --limit 100

# 常规测试 / 深度思考测试
export DEEPSEEK_API_KEY=sk-xxx
python eval/evaluate.py --provider openai --model deepseek-chat \
    --base-url https://api.deepseek.com/v1 --mode normal --tag ds-normal
python eval/evaluate.py --provider openai --model deepseek-chat \
    --base-url https://api.deepseek.com/v1 --mode deep --tag ds-deep

# 汇总对比
python eval/compare.py --results results --out results/leaderboard.md
```

两种测试方式的提示词**写死在 `eval/prompts.py`**，只切换 `--mode`，不换题。

---

## 已有评测记录

`03-benchmark-eval/results/` 放着一次全量 4,000 题的实测（豆包 normal 82.15% / deep 82.17%，
含 338 条无效作答的说明）。这些是**历史记录**，由当时的脚本产生；换模型请按上面的命令重跑，
不要直接引用旧数字。

---

## 需要如实说明的地方

1. **只测单选题**：填空、解答要核对等价写法，这一版不碰。
2. **答案分布偏 C（约 29.9%）**：绝对分会虚高，报告里同时给「全选 C」的盲猜下限。
3. **难度是规则初标**：板块先验 + 文本特征打分，再按板块内排名切 40/40/20，未做人工逐题标注。
   200 条抽检清单与 kappa 计算脚本已备好，标注仍需人工完成。
4. **图片**：149 条题的 233 张图已本地化到 `dataset/assets/`；另有 40 条源页无图，
   纯文本不可解，已在 `quality.issues` 标注。
5. **数据来源**：菁优网、青夏教育精英家教网公开页面，仅用于课程研究与模型评测，
   题目版权归原站点与原作者。

---

## 仓库结构

```
hs-math-benchmark/
├── 01-proposal/                 第一次汇报：选题
├── 02-crawler-dataset/          第二次汇报
│   ├── crawler/                 采集程序（k12crawler）
│   ├── data/                    1,400 条数据集 + 原始题库
│   ├── slides/                  第二次汇报 PPT
│   └── README_delivery.md       原始交付说明
└── 03-benchmark-eval/           第三次汇报
    ├── dataset/                 4,000 条（JSON 模板格式）+ 统计 + 质检 + 图片
    ├── eval/                    评测脚本（API / 本地 HF / llama.cpp / mock）
    ├── docs/                    数据集说明、格式说明、评测说明、汇报要点、审计修复对照
    ├── results/                 历史评测记录
    ├── slides/                  第三次汇报 PPT
    └── README.md                交付包使用说明
```
