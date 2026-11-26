"""批量评测入口：读数据集 → 逐题问模型 → 抽答案 → 算指标 → 导出。

一次运行 = 一份可复现的实验记录：配置、提示模板、逐题输出、汇总指标、切片表
全部落到 out_dir，文件名带 tag，方便同一模型多次跑、多个模型横向比。

常用：
  python evaluate.py --config config.example.yaml --provider mock --tag smoke
  python evaluate.py --config config.example.yaml --provider openai \
      --model deepseek-chat --base-url https://api.deepseek.com/v1 --tag ds-chat
  python evaluate.py --config config.example.yaml --provider hf \
      --model-path /models/Qwen2.5-7B-Instruct --tag qwen7b
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

import metrics
import prompts
from providers import Completion, build_provider, call_with_retry

log = logging.getLogger("eval")

ROOT = Path(__file__).resolve().parent
def _default_config() -> Path:
    """优先读 config.yaml（本地实际配置），没有再用示例。"""
    local = ROOT / "config.yaml"
    return local if local.exists() else ROOT / "config.example.yaml"


# ---------------------------------------------------------------------------
def load_config(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("run", {})
    cfg.setdefault("provider", {})
    cfg.setdefault("data", {})
    cfg.setdefault("output", {})
    return cfg


DIFF_FROM_TEMPLATE = {"easy": "基础", "medium": "中档", "hard": "压轴"}


def normalize_item(d: Dict[str, Any]) -> Dict[str, Any]:
    """兼容两种格式：
    · 内部扁平格式（stem/options/answer…）
    · 《高考题目数据集 JSON 收集规范 v1.0.0》格式（question/choices/answers/metadata…）
    """
    if "question" not in d or "choices" not in d:
        return d
    md = d.get("metadata") or {}
    ext = md.get("extensions") or {}
    choices = d.get("choices") or {}
    answers = d.get("answers") or []
    content = md.get("content") or {}
    exam = md.get("exam") or {}
    return {
        "id": d.get("id"),
        "board": ext.get("board") or (content.get("knowledge_points") or [None])[0],
        "sub_board": ext.get("sub_board"),
        "difficulty": DIFF_FROM_TEMPLATE.get(content.get("difficulty"), content.get("difficulty")),
        "subset": ext.get("subset"),
        "split": ext.get("split"),
        "source": (md.get("source") or {}).get("name") or "unknown",
        "year": exam.get("year"),
        "stem": re.sub(r"\{\{img://d+//}//}", "[图]", d.get("question") or ""),
        "options": [choices[k] for k in sorted(choices)],
        "answer": (answers or [""])[0],
        "analysis": (md.get("answer") or {}).get("explanation") or "",
    }


def load_items(path: Path, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = normalize_item(json.loads(line))
            d["year_bucket"] = year_bucket(d.get("year"))
            items.append(d)
    return items


def year_bucket(year: Any) -> str:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return "未知"
    if y >= 2019:
        return "2019 及以后"
    if y >= 2010:
        return "2010–2018"
    return "2009 及以前"


def filter_items(items: List[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    out = items
    if args.subset:
        out = [i for i in out if i.get("subset") in args.subset]
    if args.board:
        out = [i for i in out if i.get("board") in args.board]
    if args.difficulty:
        out = [i for i in out if i.get("difficulty") in args.difficulty]
    if args.split:
        out = [i for i in out if i.get("split") in args.split]
    if args.source:
        out = [i for i in out if i.get("source") in args.source]
    if args.limit:
        out = out[: args.limit]
    return out


# ---------------------------------------------------------------------------
def mock_completion(item: Dict[str, Any], base_acc: float, seed: int,
                    stage: int = 1, previous: str = "") -> Completion:
    """离线自检用：按难度调整答对概率，按 id 确定性地"答对/答错"。

    复核轮（stage>=2）以 0.9 的概率维持上一轮结论、0.1 的概率改答案，
    模拟"复核多数时候确认、偶尔纠正"，让 deep 模式的结果形状接近真实。
    """
    diff_shift = {"基础": 0.12, "中档": 0.0, "压轴": -0.14}
    p = max(0.02, min(0.98, base_acc + diff_shift.get(item.get("difficulty"), 0)))
    h = int(hashlib.md5((item["id"] + str(seed) + f":s{stage}").encode()).hexdigest(), 16)
    rng = random.Random(h)
    if stage >= 2 and previous:
        gold = item.get("answer") or ""
        roll = rng.random()
        # 复核轮：答错时有一定概率改对，答对时小概率被"过度思考"改错
        if previous != gold and roll < 0.35:
            return Completion(text=f"复核：重算后发现上一轮漏了一项，改正后应选 {gold}。\n答案：{gold}",
                              model="mock", latency=round(0.05 + rng.random() * 0.2, 3))
        if previous == gold and roll < 0.04:
            wrong = rng.choice([c for c in "ABCD" if c != previous])
            return Completion(text=f"复核：对边界条件判断有变，改选 {wrong}。\n答案：{wrong}",
                              model="mock", latency=round(0.05 + rng.random() * 0.2, 3))
        return Completion(text=f"复核：关键步骤重算后结论不变。\n答案：{previous}",
                          model="mock", latency=round(0.05 + rng.random() * 0.2, 3))
    roll = rng.random()
    gold = item["answer"]
    if roll < p * 0.97:
        pred = gold
        text = f"逐项检验后，只有 {gold} 满足条件。\n答案：{gold}"
    elif roll < p:
        pred = ""
        text = "这道题需要更多信息才能确定。"
    else:
        wrong = rng.choice([c for c in "ABCD" if c != gold])
        pred = wrong
        text = f"计算后选择 {wrong}。\n答案：{wrong}"
    return Completion(text=text, model="mock", latency=round(0.15 + rng.random() * 0.6, 3))


# ---------------------------------------------------------------------------
def run_one(
    item: Dict[str, Any],
    provider: Any,
    cfg: Dict[str, Any],
    *,
    mode: str,
    rounds: int,
    temperature: float,
    max_tokens: int,
    retries: int,
) -> Dict[str, Any]:
    """按测试方式跑一道题。

    normal：1 次调用。
    deep  ：首答 + (rounds-1) 轮固定提示词的独立复核，判分取最后一轮。
    """
    is_mock = getattr(provider, "name", "") == "mock"
    mock_acc = float(cfg.get("provider", {}).get("mock_accuracy", 0.62))
    seed = int(cfg.get("run", {}).get("seed", 0))

    stage_answers: List[str] = []
    stage_latency: List[float] = []
    stage_errors: List[str] = []
    stage_truncated: List[bool] = []
    prompt_tokens = 0
    completion_tokens = 0
    last_text = ""

    system, user = prompts.build_messages(item, mode)
    total_rounds = rounds if mode == "deep" else 1
    for stage in range(1, total_rounds + 1):
        if stage > 1:
            system, user = prompts.build_verify_messages(item, stage_answers[-1] if stage_answers else "")
        if is_mock:
            comp = mock_completion(item, mock_acc, seed, stage, stage_answers[-1] if stage_answers else "")
        else:
            comp = call_with_retry(
                provider, system, user,
                temperature=temperature, max_tokens=max_tokens, retries=retries,
            )
        last_text = comp.text
        truncated = comp.finish_reason == "length"
        stage_truncated.append(truncated)
        if comp.error:
            stage_errors.append(f"stage{stage}: {comp.error}")
        stage_latency.append(comp.latency)
        prompt_tokens += comp.prompt_tokens
        completion_tokens += comp.completion_tokens

        pred_stage, method_stage = prompts.extract_answer(comp.text, item.get("options"))
        # 被截断且没有明确「答案：X」标记 → 不认这次抽取，避免把正文里的字母当答案
        if truncated and method_stage not in ("答案标记", "boxed", "故选"):
            pred_stage = ""
        stage_answers.append(pred_stage)

    # 判分以最后一个**有效**轮次为准：末轮失败/空时回退到上一轮，并记录回退
    final_stage = len(stage_answers)
    pred = stage_answers[-1] if stage_answers else ""
    fallback = False
    if not pred:
        for i in range(len(stage_answers) - 2, -1, -1):
            if stage_answers[i]:
                pred = stage_answers[i]
                final_stage = i + 1
                fallback = i + 1 != len(stage_answers)
                break
    method = prompts.extract_answer(last_text, item.get("options"))[1]

    # error 只在"最终没有答案"时保留，避免答对却被算作失败（审计 P1-3）
    error = "" if pred else ("；".join(stage_errors[-2:]) if stage_errors else "")
    return {
        "id": item["id"],
        "mode": mode,
        "rounds": total_rounds,
        "stage_answers": stage_answers,
        "stage_errors": stage_errors,
        "stage_truncated": stage_truncated,
        "fallback_stage": final_stage if fallback else None,
        "calls": total_rounds,
        "board": item.get("board"),
        "sub_board": item.get("sub_board"),
        "difficulty": item.get("difficulty"),
        "subset": item.get("subset"),
        "source": item.get("source"),
        "split": item.get("split"),
        "year": item.get("year"),
        "year_bucket": item.get("year_bucket"),
        "gold": item.get("answer"),
        "pred": pred,
        "correct": bool(pred) and pred == item.get("answer"),
        "extract_method": method,
        "truncated": any(stage_truncated),
        "latency": round(sum(stage_latency), 3),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "error": error,
        "raw_output": last_text[:1200],
    }


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="高中数学 Benchmark 批量评测", allow_abbrev=False)
    ap.add_argument("--config", default=str(_default_config()))
    ap.add_argument("--dataset", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--provider", default="", help="openai / hf / llamacpp / mock")
    ap.add_argument("--model", default="", help="模型名（API 端点用）")
    ap.add_argument("--model-path", default="", help="本地权重路径（hf / llamacpp 用）")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--api-key", default="", help="直接给 Key（会进 shell 历史，推荐用 --api-key-env）")
    ap.add_argument("--api-key-env", default="")
    ap.add_argument("--skip-probe", action="store_true", help="跳过启动时的连通性探测")
    ap.add_argument("--temperature", type=float, default=None)
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--mode", default="", choices=["", "normal", "deep"],
                    help="测试方式：normal 常规作答；deep 深度反复思考（固定提示词多轮复核）")
    ap.add_argument("--deep-rounds", type=int, default=None,
                    help="deep 模式总轮数（含首答），默认 2：首答 + 1 轮复核")
    ap.add_argument("--template", default="", help="[已废弃] 兼容旧参数，cot/zero_shot 均映射到 normal")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--subset", nargs="*", default=[])
    ap.add_argument("--board", nargs="*", default=[])
    ap.add_argument("--difficulty", nargs="*", default=[])
    ap.add_argument("--split", nargs="*", default=[])
    ap.add_argument("--source", nargs="*", default=[])
    ap.add_argument("--resume", action="store_true", help="跳过已出结果的题（按 id）")
    ap.add_argument("--dry-run", action="store_true", help="只打印前两条 prompt，不调模型")
    args = ap.parse_args()

    try:  # Windows 控制台默认 GBK，中文提示词会花屏
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(args.config))

    raw_dataset = args.dataset or cfg["data"].get("dataset") or "../dataset/benchmark_v3.template.jsonl"
    dataset = Path(raw_dataset)
    if not dataset.is_absolute():
        dataset = (Path(args.config).resolve().parent / dataset).resolve()
    if not dataset.exists():
        # 回退：按 eval/ 目录找同级的 dataset/ 或 crawler/data/benchmark_v3/
        for cand in (
            ROOT.parent / "dataset" / "benchmark_v3.template.jsonl",
            ROOT.parent / "crawler" / "data" / "benchmark_v3" / "benchmark_v3.template.jsonl",
            ROOT / "dataset" / "benchmark_v3.template.jsonl",
        ):
            if cand.exists():
                dataset = cand
                log.warning("配置里的数据集不存在，已回退到 %s", cand)
                break
    out_dir = Path(args.out or cfg["output"].get("dir") or "results")
    if not out_dir.is_absolute():
        out_dir = (Path(args.config).resolve().parent / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    run_cfg = cfg["run"]
    mode = args.mode or str(run_cfg.get("mode") or cfg.get("mode") or "normal")
    if mode not in prompts.MODES:
        mode = "normal"
    rounds = args.deep_rounds if args.deep_rounds is not None else int(
        run_cfg.get("deep_rounds") or cfg.get("deep_rounds") or 2
    )
    rounds = max(1, rounds)
    temperature = args.temperature if args.temperature is not None else float(run_cfg.get("temperature", 0.0))
    default_mt = prompts.default_max_tokens(mode)
    if args.max_tokens:
        max_tokens = args.max_tokens
    elif mode == "deep":
        # 深度思考需要更长输出，配置里若仍是常规值，抬到 1024
        max_tokens = max(int(run_cfg.get("max_tokens") or 0), default_mt)
    else:
        max_tokens = int(run_cfg.get("max_tokens") or default_mt)
    concurrency = args.concurrency or int(run_cfg.get("concurrency", 8))
    retries = int(run_cfg.get("retries", 3))
    seed = int(run_cfg.get("seed", 0))

    prov_cfg = dict(cfg["provider"])
    if args.provider:
        prov_cfg["type"] = args.provider
    if args.model:
        prov_cfg["model"] = args.model
    if args.model_path:
        prov_cfg["model_path"] = args.model_path
    if args.base_url:
        prov_cfg["base_url"] = args.base_url
    if args.api_key_env:
        prov_cfg["api_key_env"] = args.api_key_env
    if str(prov_cfg.get("type", "")).lower() == "mock":
        prov_cfg["model"] = "mock"
    if args.api_key:
        prov_cfg["api_key"] = args.api_key
    key_env = prov_cfg.get("api_key_env")
    if key_env and not prov_cfg.get("api_key"):
        prov_cfg["api_key"] = os.environ.get(str(key_env), "")
    if prov_cfg.get("type") in ("openai", "openai_compatible", "api", "vllm", "ollama"):
        base = str(prov_cfg.get("base_url") or "")
        local = any(k in base for k in ("127.0.0.1", "localhost", "0.0.0.0"))
        if not prov_cfg.get("api_key") and not local:
            log.error(
                "未提供 API Key：用 --api-key 或 --api-key-env 指定，或先 export 对应环境变量。"
                "（本地 vLLM/Ollama 可留空）"
            )
            return 2

    items = filter_items(load_items(dataset, cfg), args)
    n_img = sum(1 for i in items if "[图]" in i["stem"])
    if n_img and not args.subset:
        log.warning(
            "本次有 %d 条题干含图片占位（共 %d 条）。离线评测读不到图，建议加 --subset core 只跑纯文字题。",
            n_img, len(items),
        )
    if not items:
        log.error("筛选后没有题目，检查 --subset/--board/--difficulty 参数")
        return 2

    if args.dry_run:
        for it in items[:2]:
            system, user = prompts.build_messages(it, mode)
            print("=" * 70)
            print("[system]\n" + system)
            print("[user]\n" + user)
        print(f"\n共 {len(items)} 题；dry-run 不调用模型。")
        return 0

    provider = build_provider(prov_cfg)
    if not args.skip_probe and prov_cfg.get("type") != "mock":
        log.info("连通性探测中……")
        probe = provider.complete("你是连通性测试助手。", "请只回复 ok", temperature=0.0, max_tokens=4)
        if probe.error:
            log.error("连通性探测失败，未开始评测：%s", probe.error)
            log.error("检查 base_url / API Key / 网络后重试；确需跳过可加 --skip-probe")
            return 3
    # 本地模型线程不安全，强制串行；API 端点按配置并发
    if prov_cfg.get("type") in ("hf", "transformers", "local", "llamacpp", "gguf"):
        concurrency = 1

    tag = args.tag or re.sub(
        r"[^\w.-]+", "_", f"{prov_cfg.get('model') or prov_cfg.get('type') or 'run'}-{mode}"
    )
    res_path = out_dir / f"results_{tag}.jsonl"
    done: set = set()
    prev_rows: List[Dict[str, Any]] = []
    if args.resume and res_path.exists():
        with res_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                prev_rows.append(row)
                done.add(row["id"])
        items = [i for i in items if i["id"] not in done]
        log.info("续跑：已有 %d 条，本次待跑 %d 条", len(done), len(items))

    meta = {
        "model": prov_cfg.get("model") or prov_cfg.get("model_path"),
        "provider": prov_cfg.get("type"),
        "dataset": str(dataset),
        "mode": mode,
        "deep_rounds": rounds if mode == "deep" else 1,
        "prompt": "fixed:" + mode,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "concurrency": concurrency,
        "seed": seed,
        "items": len(items),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    log.info("开始评测：%s，%d 题，并发 %d", meta["model"], len(items), concurrency)

    rows: List[Dict[str, Any]] = list(prev_rows)
    lock = threading.Lock()
    t0 = time.time()
    if res_path.exists() and not args.resume:
        log.warning("结果文件已存在且未加 --resume，将覆盖：%s", res_path)
    # 不加 --resume 时覆盖写，避免旧明细与新 summary 口径不一致（审计 P0-1）
    with res_path.open("a" if args.resume else "w", encoding="utf-8") as fh:
        if concurrency <= 1:
            for i, it in enumerate(items, 1):
                row = run_one(it, provider, cfg, mode=mode, rounds=rounds,
                              temperature=temperature, max_tokens=max_tokens, retries=retries)
                rows.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                if i % 100 == 0:
                    log.info("进度 %d/%d", i, len(items))
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futures = {
                    ex.submit(run_one, it, provider, cfg, mode=mode, rounds=rounds,
                              temperature=temperature, max_tokens=max_tokens, retries=retries): it
                    for it in items
                }
                for i, fut in enumerate(as_completed(futures), 1):
                    row = fut.result()
                    rows.append(row)
                    with lock:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        fh.flush()
                    if i % 100 == 0:
                        log.info("进度 %d/%d", i, len(items))

    meta["elapsed"] = round(time.time() - t0, 1)
    meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    summary = metrics.summarize(rows, meta)
    summary["meta"]["mock"] = prov_cfg.get("type") == "mock"

    (out_dir / f"summary_{tag}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"summary_{tag}.md").write_text(metrics.format_markdown(summary, f"评测结果 · {meta['model']}"), encoding="utf-8")

    with (out_dir / f"per_item_{tag}.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        cols = ["id", "mode", "rounds", "calls", "board", "sub_board", "difficulty", "subset",
                "source", "year", "gold", "stage_answers", "pred", "correct",
                "extract_method", "latency", "error"]
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c) for c in cols])

    with (out_dir / f"matrix_{tag}.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["board"] + metrics.DIFFICULTIES)
        for b in metrics.BOARDS:
            w.writerow([b] + [
                (f"{summary['board_x_difficulty'][b][d]['accuracy']:.4f}"
                 if d in summary["board_x_difficulty"].get(b, {}) else "")
                for d in metrics.DIFFICULTIES
            ])

    o = summary["overall"]
    print(json.dumps({
        "tag": tag,
        "n": o["n"],
        "accuracy": o["accuracy"],
        "ci95": o["ci95"],
        "macro_board": summary["macro_board_accuracy"],
        "invalid_rate": o["invalid_rate"],
        "error_rate": o["error_rate"],
        "elapsed": meta["elapsed"],
        "out": str(out_dir),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
