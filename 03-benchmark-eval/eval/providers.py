"""模型调用后端：一套接口，三种部署方式。

  · openai    —— 任何 OpenAI 兼容端点（OpenAI / DeepSeek / 通义 / 智谱 / vLLM / Ollama /v1）
  · hf        —— 本地 transformers（需 pip install transformers torch accelerate）
  · llamacpp  —— 本地 GGUF（需 pip install llama-cpp-python）
  · mock      —— 不调模型，按 id 哈希生成确定性答案，用于跑通链路与压测

设计取舍：
  不用官方 openai SDK，直接用 requests 打 /chat/completions。
  好处是零额外依赖、可指向任何兼容端点（含本地 vLLM），坏处是少一层 SDK 便利，
  但评测只需要 chat + usage 两个字段，不值得为此引入依赖。
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("eval.provider")


@dataclass
class Completion:
    text: str
    model: str = ""
    latency: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    finish_reason: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class Provider:
    name = "base"

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> Completion:
        raise NotImplementedError


# ---------------------------------------------------------------------------
class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.base_url = str(cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        self.model = str(cfg.get("model") or "gpt-4o-mini")
        self.api_key = str(cfg.get("api_key") or "")
        self.timeout = float(cfg.get("timeout") or 120)
        self.name = f"openai:{self.model}"
        self._session = requests.Session()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._session.headers.update(headers)

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> Completion:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        t0 = time.time()
        try:
            r = self._session.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout)
            if r.status_code >= 400:
                return Completion("", self.model, time.time() - t0, error=f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            if not text and choice.get("text"):
                text = choice["text"]
            usage = data.get("usage") or {}
            return Completion(
                text=text,
                model=data.get("model") or self.model,
                latency=time.time() - t0,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                finish_reason=str(choice.get("finish_reason") or ""),
                raw={"finish_reason": choice.get("finish_reason")},
            )
        except Exception as exc:  # noqa: BLE001
            return Completion("", self.model, time.time() - t0, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
class HFProvider(Provider):
    """本地 transformers 推理：适合 7B 级别、单卡或 CPU 量化模型。"""

    name = "hf"

    def __init__(self, cfg: Dict[str, Any]) -> None:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("本地 HF 推理需要 transformers 与 torch：pip install transformers torch accelerate") from exc
        self.model_path = cfg.get("model_path") or cfg.get("model") or ""
        self.name = f"hf:{self.model_path}"
        self.device = cfg.get("device") or ("cuda" if torch.cuda.is_available() else "cpu")
        self.dtype = cfg.get("dtype") or ("float16" if self.device == "cuda" else "float32")
        self.trust_remote_code = bool(cfg.get("trust_remote_code", True))
        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(self.model_path, trust_remote_code=self.trust_remote_code)
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            torch_dtype=getattr(torch, self.dtype),
            device_map=cfg.get("device_map") or ("auto" if self.device == "cuda" else None),
            trust_remote_code=self.trust_remote_code,
        )
        self._model.eval()

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> Completion:
        torch = self._torch
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            prompt = self._tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        except Exception:  # 部分基座模型没有 chat template
            prompt = f"{system}\n\n{user}\n\n答案："
        t0 = time.time()
        try:
            inputs = self._tok(prompt, return_tensors="pt").to(self._model.device)
            with torch.no_grad():
                out = self._model.generate(
                    **inputs,
                    max_new_tokens=max_tokens,
                    do_sample=temperature > 0,
                    temperature=max(temperature, 0.01),
                    pad_token_id=self._tok.eos_token_id,
                )
            gen = out[0][inputs["input_ids"].shape[1] :]
            text = self._tok.decode(gen, skip_special_tokens=True)
            return Completion(text=text, model=self.name, latency=time.time() - t0,
                              prompt_tokens=int(inputs["input_ids"].shape[1]),
                              completion_tokens=int(gen.shape[0]),
                              finish_reason="length" if gen.shape[0] >= max_tokens else "stop")
        except Exception as exc:  # noqa: BLE001
            return Completion("", self.name, time.time() - t0, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
class LlamaCppProvider(Provider):
    """本地 GGUF：llama.cpp 绑定，适合没有 GPU 的机器跑量化模型。"""

    name = "llamacpp"

    def __init__(self, cfg: Dict[str, Any]) -> None:
        try:
            from llama_cpp import Llama
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("GGUF 推理需要 llama-cpp-python：pip install llama-cpp-python") from exc
        self.model_path = cfg.get("model_path") or cfg.get("model") or ""
        self.name = f"llamacpp:{self.model_path}"
        self._llm = Llama(
            model_path=self.model_path,
            n_ctx=int(cfg.get("n_ctx") or 4096),
            n_threads=int(cfg.get("n_threads") or 0) or None,
            verbose=False,
        )

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> Completion:
        t0 = time.time()
        try:
            res = self._llm.create_chat_completion(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = (res.get("choices") or [{}])[0].get("message", {}).get("content", "")
            usage = res.get("usage") or {}
            return Completion(text=text, model=self.name, latency=time.time() - t0,
                              prompt_tokens=int(usage.get("prompt_tokens") or 0),
                              completion_tokens=int(usage.get("completion_tokens") or 0),
                              finish_reason=str(res.get("choices", [{}])[0].get("finish_reason") or "stop"))
        except Exception as exc:  # noqa: BLE001
            return Completion("", self.name, time.time() - t0, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
class MockProvider(Provider):
    """占位实现：真正的 mock 作答由 evaluate.mock_completion 生成。"""

    name = "mock"

    def complete(self, system: str, user: str, *, temperature: float, max_tokens: int) -> Completion:
        return Completion(text="答案：A", model="mock", latency=0.002, finish_reason="stop")


# ---------------------------------------------------------------------------
def build_provider(cfg: Dict[str, Any]) -> Provider:
    kind = str(cfg.get("type") or "openai").lower()
    if kind in ("openai", "openai_compatible", "api", "vllm", "ollama"):
        return OpenAICompatProvider(cfg)
    if kind in ("hf", "transformers", "local"):
        return HFProvider(cfg)
    if kind in ("llamacpp", "gguf"):
        return LlamaCppProvider(cfg)
    if kind == "mock":
        # mock 的作答逻辑在 evaluate.mock_completion 里（需要题目 id 与难度），
        # 这里只返回一个占位 provider，保证接口统一。
        return MockProvider()
    raise ValueError(f"未知 provider 类型：{kind}（可选 openai / hf / llamacpp / mock）")


def call_with_retry(
    provider: Provider,
    system: str,
    user: str,
    *,
    temperature: float,
    max_tokens: int,
    retries: int = 3,
    backoff: float = 1.5,
) -> Completion:
    last: Optional[Completion] = None
    for attempt in range(1, retries + 1):
        c = provider.complete(system, user, temperature=temperature, max_tokens=max_tokens)
        if not c.error:
            return c
        last = c
        log.warning("调用失败（第 %d/%d 次）：%s", attempt, retries, c.error)
        if attempt < retries:
            time.sleep(backoff ** (attempt - 1) * 0.5)
    return last or Completion("", error="unknown")
