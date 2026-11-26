"""可插拔的模型骨干（Backbone）注册与调用。

设计目标（对应拓展要求 2）：
    「所有流程都能简单配置不同的 VLM 作为 Backbone」

因此这里只做一件事：把 config.yaml 里的 backbone 名字解析成一个统一的
OpenAI 兼容客户端。换 Backbone = 改一行配置（或加一个 --backbone 参数），
爬取、公式识别、PDF 取题、图像取题等一切下游流程无需改任何代码。

支持的 provider：
    · ollama            —— 本地部署（Qwen 系列默认走这条）
    · openai_compatible —— 任何 OpenAI 兼容端点（OpenAI / DeepSeek / 智谱 ...）
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

log = logging.getLogger("k12.llm")


class BackboneError(RuntimeError):
    """Backbone 不可用（配置错误 / 服务未启动 / 缺 Key）。"""


# ---------------------------------------------------------------------------
def _load_image_b64(source: Union[str, Path, bytes]) -> tuple[str, str]:
    """返回 (mime, base64)。支持路径与原始字节。"""
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        mime = "image/png"
    else:
        p = Path(source)
        if not p.exists():
            raise BackboneError(f"图片不存在：{p}")
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
    return mime, base64.b64encode(data).decode("ascii")


def _data_url(source: Union[str, Path, bytes]) -> str:
    mime, b64 = _load_image_b64(source)
    return f"data:{mime};base64,{b64}"


# ---------------------------------------------------------------------------
class ModelClient:
    """统一的模型调用入口。文本、图像、多图共用同一个 chat 接口。"""

    def __init__(self, spec: Dict[str, Any]):
        if not spec:
            raise BackboneError("Backbone 配置为空")
        self.spec = dict(spec)
        self.name: str = spec.get("name", spec.get("model", "unknown"))
        self.provider: str = (spec.get("provider") or "ollama").lower()
        self.model: str = spec.get("model") or self.name
        self.base_url: str = spec.get("base_url") or "http://127.0.0.1:11434/v1"
        self.vision: bool = bool(spec.get("vision", True))
        self.temperature: float = float(spec.get("temperature", 0.1) or 0.1)
        self.max_tokens: int = int(spec.get("max_tokens", 4096) or 4096)
        self.timeout: int = int(spec.get("timeout", 180) or 180)
        self.request_retries: int = int(spec.get("request_retries", 2) or 0)
        self._client = None

    # ------------------------------------------------------------------
    @property
    def api_key(self) -> str:
        env_name = self.spec.get("api_key_env")
        if env_name:
            val = os.environ.get(env_name, "").strip()
            if not val:
                raise BackboneError(
                    f"Backbone '{self.name}' 需要环境变量 {env_name}，但它为空。"
                    f"请先 export {env_name}=<你的 Key>"
                )
            return val
        return (self.spec.get("api_key") or "not-needed").strip()

    def client(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise BackboneError("缺少 openai 依赖，请执行：pip install openai") from exc
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=0,  # 自行控制重试，便于给出中文错误
        )
        return self._client

    # ------------------------------------------------------------------
    def chat(self, messages: List[Dict[str, Any]], **overrides: Any) -> str:
        """底层对话接口，返回纯文本。"""
        if self.provider == "mock":
            return _mock_response(messages)
        params = {
            "model": self.model,
            "messages": messages,
            "temperature": overrides.pop("temperature", self.temperature),
            "max_tokens": overrides.pop("max_tokens", self.max_tokens),
        }
        params.update(overrides)
        last_err: Optional[Exception] = None
        for attempt in range(1, self.request_retries + 2):
            try:
                resp = self.client().chat.completions.create(**params)
                if not resp.choices:
                    raise BackboneError("模型返回空 choices")
                content = resp.choices[0].message.content
                if isinstance(content, list):  # 某些实现返回分段内容
                    content = "".join(
                        seg.get("text", "") for seg in content if isinstance(seg, dict)
                    )
                return (content or "").strip()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.debug("模型调用失败(%d)：%s", attempt, exc)
        raise BackboneError(
            f"Backbone '{self.name}' 调用失败：{last_err}\n"
            f"  端点：{self.base_url}\n"
            f"  模型：{self.model}\n"
            f"  提示：本地 Ollama 请确认已 `ollama serve` 并 `ollama pull {self.model}`"
        )

    # ------------------------------------------------------------------
    def chat_text(self, prompt: str, system: str = "") -> str:
        msgs: List[Dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return self.chat(msgs)

    def chat_image(self, image: Union[str, Path, bytes], prompt: str, system: str = "") -> str:
        return self.chat_images([image], prompt, system=system)

    def chat_images(
        self,
        images: Sequence[Union[str, Path, bytes]],
        prompt: str,
        system: str = "",
    ) -> str:
        if not self.vision:
            raise BackboneError(
                f"Backbone '{self.name}' 未开启视觉能力（vision: false），无法处理图像。"
                f"请换用 qwen3-vl-8b 之类的多模态 Backbone。"
            )
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": _data_url(img)}})
        msgs: List[Dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": content})
        return self.chat(msgs)

    # ------------------------------------------------------------------
    def health_check(self) -> Dict[str, Any]:
        """连通性自检，供 `k12crawler backbones --check` 使用。"""
        info: Dict[str, Any] = {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "vision": self.vision,
        }
        try:
            reply = self.chat_text("只回复两个字：可用")
            info.update({"ok": True, "reply": reply[:40]})
        except Exception as exc:  # noqa: BLE001
            info.update({"ok": False, "error": str(exc)[:300]})
        return info

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ModelClient {self.name} model={self.model} base={self.base_url}>"


# ---------------------------------------------------------------------------
# 离线自检用的 Mock Backbone
#   作用：在没有本地 VLM / 没有 API Key 的机器上，也能验证"多模态解析链路"
#   的完整性与输出 schema —— 用于 `k12crawler selftest` 和 CI。
#   它不会替代真实 Backbone：所有产出都会被打上 parser=mock 便于识别。
# ---------------------------------------------------------------------------

_MOCK_QUESTIONS = {
    "questions": [
        {
            "number": "1",
            "stem": "已知函数 $f(x)=x^{2}-2x+3$，求 $f(x)$ 在区间 $[0,3]$ 上的最小值。",
            "options": [],
            "answer": "$f(x)_{\\min}=f(1)=2$",
            "analysis": "配方得 $f(x)=(x-1)^{2}+2$，对称轴 $x=1$ 落在区间内，故最小值为 $2$。",
            "qtype": "解答题",
            "subject": "初中数学",
            "tags": ["二次函数", "最值"],
            "difficulty": 0.45,
        },
        {
            "number": "2",
            "stem": "下列物理量中，属于矢量的是（　　）",
            "options": ["A．功", "B．电势", "C．电势能", "D．电场强度"],
            "answer": "D",
            "analysis": "电场强度既有大小又有方向，是矢量；功、电势、电势能均为标量。",
            "qtype": "选择题",
            "subject": "高中物理",
            "tags": ["矢量", "电场强度"],
            "difficulty": 0.25,
        },
    ]
}


def _mock_response(messages: List[Dict[str, Any]]) -> str:
    """根据提示词特征返回结构合法的假响应。"""
    text_parts: List[str] = []
    for m in messages:
        if isinstance(m, str):
            text_parts.append(m)
            continue
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for seg in content:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text_parts.append(seg.get("text", ""))
    prompt = "\n".join(text_parts)

    # 判定顺序很重要：题目抽取的提示词里同样会出现 "LaTeX" 字样，
    # 因此必须先匹配更具体的特征串。
    if "clean_stem" in prompt or "难度估计" in prompt:
        return '{"clean_stem": "（mock）已清洗题干", "tags": ["二次函数"], "difficulty": 0.4}'
    if "判断下面这道题" in prompt:
        return '{"grade": "初中", "subject": "初中数学", "qtype": "选择题"}'
    if "只输出 LaTeX 本体" in prompt:
        return "x^{2}-2x+3"
    if '"questions"' in prompt or "questions" in prompt:
        return json.dumps(_MOCK_QUESTIONS, ensure_ascii=False)
    return "可用"


# ---------------------------------------------------------------------------
def build_client(cfg, backbone: str = "") -> ModelClient:
    """按名字（或默认）构造客户端。"""
    spec = cfg.backbone(backbone)
    return ModelClient(spec)


# ---------------------------------------------------------------------------
# JSON 宽松解析：模型输出常常带 ```json 围栏或前后废话
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_json_loose(text: str) -> Any:
    """从模型输出里尽量稳地抠出 JSON。"""
    if not text:
        raise ValueError("模型输出为空")
    text = text.strip()

    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 找第一个 { 或 [ 到最后一个配对的 } 或 ]。
    # 注意：必须按"哪个先出现"决定尝试顺序，否则 [{"a":1}] 会被
    # 误判成对象 {"a":1}（这是自检抓到的真实 bug）。
    order = [("{", "}"), ("[", "]")]
    first_obj, first_arr = text.find("{"), text.find("[")
    if first_arr != -1 and (first_obj == -1 or first_arr < first_obj):
        order.reverse()

    for open_ch, close_ch in order:
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            chunk = text[start: end + 1]
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                chunk2 = re.sub(r",(\s*[}\]])", r"\1", chunk)  # 去掉尾随逗号
                try:
                    return json.loads(chunk2)
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"无法从模型输出中解析 JSON：{text[:200]}")
