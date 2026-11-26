"""配置加载：YAML -> 点号访问的 Config 对象。

设计原则：
  * 所有路径都以项目根目录为基准解析，配置里只写相对路径；
  * 缺字段时回落到内置默认值，不会因为少写一行就崩；
  * `config.yaml` 不存在时自动从 `config.example.yaml` 复制一份。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

# project_root/k12crawler/config.py -> project_root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.yaml"

# 内置兜底默认值（配置里没写时生效）
DEFAULTS: Dict[str, Any] = {
    "project": {
        "name": "k12-crawler",
        "data_dir": "data",
        "log_dir": "logs",
        "log_level": "INFO",
    },
    "crawler": {
        "source": "jiajiao1010-subject",
        "base_url": "http://www.1010jiajiao.com",
        "column": "czsx",
        "start_page": 1,
        "max_pages": 40,
        "page_step": 1,
        "max_questions": 400,
        "fetch_detail": True,
        "concurrency": 4,
        "delay": 0.6,
        "jitter": 0.3,
        "empty_tolerance": 3,
        "empty_jump": 8,
        "questions_per_paper": 6,
        "timeout": 20,
        "retries": 3,
        "backoff": 2.0,
        "respect_robots": True,
        "proxy": "",
        "account": {"username": "", "password": "", "cookie": ""},
        "user_agents": [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ],
    },
    "storage": {
        "sqlite_path": "data/k12.sqlite3",
        "jsonl_path": "data/questions.jsonl",
        "json_snapshot_dir": "data/json",
        "snapshot_every": 200,
        "save_raw_html": False,
        "raw_html_dir": "data/raw",
    },
    "schedule": {
        "enabled": True,
        "interval_minutes": 30,
        "run_on_start": True,
        "window": "",
        "max_rounds": 0,
    },
    "models": {"default": "qwen3-9b", "backbones": {}},
    "multimodal": {
        "enabled": True,
        "backbone": "",
        "render_dpi": 200,
        "page_range": "",
        "text_first": True,
        "formula_to_latex": True,
        "max_images_per_question": 12,
        "image_extensions": [".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"],
    },
    "enrich": {
        "enabled": False,
        "backbone": "",
        "infer_tags": True,
        "infer_difficulty": True,
        "clean_stem": True,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """点号访问的配置容器。"""

    def __init__(self, data: Dict[str, Any], root: Path = PROJECT_ROOT, path: Optional[Path] = None):
        self._data = data
        self.root = Path(root)
        self.path = path

    # -------------------------------------------------- 读取
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, name: str) -> Dict[str, Any]:
        val = self._data.get(name, {})
        return val if isinstance(val, dict) else {}

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def as_dict(self) -> Dict[str, Any]:
        return self._data

    # -------------------------------------------------- 路径
    def path_of(self, dotted: str, default: str = "") -> Path:
        """把配置里的相对路径解析成绝对路径。"""
        raw = self.get(dotted, default) or default
        p = Path(str(raw))
        return p if p.is_absolute() else (self.root / p)

    @property
    def data_dir(self) -> Path:
        return self.path_of("project.data_dir", "data")

    @property
    def log_dir(self) -> Path:
        return self.path_of("project.log_dir", "logs")

    # -------------------------------------------------- 模型
    def backbone(self, name: str = "") -> Dict[str, Any]:
        """取某个 Backbone 的配置；name 为空则用 models.default。"""
        name = name or self.get("models.default", "qwen3-9b")
        backbones = self.get("models.backbones", {}) or {}
        if name not in backbones:
            raise KeyError(
                f"未找到 Backbone '{name}'。可用：{', '.join(sorted(backbones)) or '(无)'}"
            )
        cfg = dict(backbones[name])
        cfg["name"] = name
        for k in ("temperature", "max_tokens", "timeout", "request_retries"):
            if k not in cfg and self.get(f"models.{k}") is not None:
                cfg[k] = self.get(f"models.{k}")
        return cfg

    def backbone_names(self) -> List[str]:
        return sorted((self.get("models.backbones", {}) or {}).keys())


def load_config(path: Optional[str | os.PathLike] = None, *, auto_create: bool = True) -> Config:
    """加载配置；找不到 config.yaml 时自动从示例复制。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not cfg_path.exists():
        if auto_create and EXAMPLE_CONFIG_PATH.exists() and cfg_path == DEFAULT_CONFIG_PATH:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(EXAMPLE_CONFIG_PATH, cfg_path)
        else:
            raise FileNotFoundError(f"配置文件不存在：{cfg_path}")

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    data = _deep_merge(DEFAULTS, raw)
    return Config(data, root=PROJECT_ROOT, path=cfg_path)


def iter_backbones(cfg: Config) -> Iterable[str]:
    yield from cfg.backbone_names()
