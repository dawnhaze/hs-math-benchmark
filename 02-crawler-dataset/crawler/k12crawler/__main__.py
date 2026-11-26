"""支持 `python -m k12crawler <子命令>` 的调用方式。

走 `main()` 而不是 `app()`，这样全局开关（如 `--json`）写在子命令
之后也能被正确归一化 —— Agent 更习惯 `k12crawler status --json`。
"""

from .cli import main

if __name__ == "__main__":
    main()
