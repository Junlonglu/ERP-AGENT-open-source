#!/usr/bin/env python
"""统一开发命令入口。

为什么不用 Makefile：本项目主要在 Windows 上开发，`make` 默认不装；而 Python
一定存在（.venv），所以用一个脚本承担"唯一命令来源"，本地和 CI 调同一份。

用法（在仓库根目录执行）：
    .venv/Scripts/python.exe scripts/dev.py install    # 装后端依赖 + 测试/检查工具
    .venv/Scripts/python.exe scripts/dev.py mcp        # 启动 MCP 服务 :9000
    .venv/Scripts/python.exe scripts/dev.py api        # 启动后端 API :8000
    .venv/Scripts/python.exe scripts/dev.py web        # 启动前端  :3000
    .venv/Scripts/python.exe scripts/dev.py lint       # ruff
    .venv/Scripts/python.exe scripts/dev.py test       # pytest
    .venv/Scripts/python.exe scripts/dev.py check      # lint + test
    .venv/Scripts/python.exe scripts/dev.py typecheck  # 前端 tsc --noEmit
    .venv/Scripts/python.exe scripts/dev.py build      # 前端生产构建
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
NPM = "npm.cmd" if os.name == "nt" else "npm"


def run(cmd: list[str], cwd: Path = ROOT, env: dict | None = None) -> int:
    merged = os.environ.copy()
    # 控制台默认编码可能是 GBK，服务启动时的 emoji 日志会直接抛 UnicodeEncodeError
    merged.setdefault("PYTHONIOENCODING", "utf-8")
    if env:
        merged.update(env)
    print(f"$ {' '.join(cmd)}  (cwd={cwd.relative_to(ROOT) or '.'})", flush=True)
    return subprocess.call(cmd, cwd=str(cwd), env=merged)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0

    task, args = argv[0], argv[1:]

    if task == "install":
        return run([
            sys.executable, "-m", "pip", "install",
            "-r", "requirements.txt",
            "-r", "requirements-observability.txt",
            "pytest", "ruff", "pre-commit",
        ])

    if task == "mcp":
        return run([sys.executable, "-m", "src.mcp_server.server_main", *args])

    if task == "api":
        return run([sys.executable, "-m", "src.api_view.web_main", *args])

    if task == "web":
        # 继承来的 PORT（例如被编辑器/工具链设成 8787）会覆盖 Next 默认端口
        return run([NPM, "run", "dev"], cwd=FRONTEND, env={"PORT": "3000"})

    if task == "lint":
        return run([sys.executable, "-m", "ruff", "check", "src/"])

    if task == "test":
        return run([sys.executable, "-m", "pytest", "-q", *args])

    if task == "check":
        code = run([sys.executable, "-m", "ruff", "check", "src/"])
        return code or run([sys.executable, "-m", "pytest", "-q", *args])

    if task == "typecheck":
        return run([NPM, "run", "typecheck"], cwd=FRONTEND)

    if task == "build":
        return run([NPM, "run", "build"], cwd=FRONTEND)

    print(f"未知任务: {task}\n", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
