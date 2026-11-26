"""运行环境信息（供 `k12crawler version` 与 Skill 自检使用）。"""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Any, Dict


def runtime_info() -> Dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": f"{platform.system()} {platform.release()}",
        "cwd": str(Path.cwd()),
        "package": str(Path(__file__).resolve().parent.parent),
    }
