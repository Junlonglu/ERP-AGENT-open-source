"""
环境变量加载工具
从项目根目录 .env 文件加载环境变量到 os.environ
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv


def ensure_utf8_stdio() -> None:
    """将标准输出/错误切换为 UTF-8，避免控制台编码导致的崩溃。

    Windows 控制台默认使用 GBK，日志里的 emoji（🚀/⚠️）会直接抛
    UnicodeEncodeError，让服务在启动第一步就死掉。这里统一改为 UTF-8（遇到无法
    编码的字符降级为替代符），并在 Windows 上把控制台代码页设为 65001，使输出
    既不崩溃也不会显示为乱码。重定向到文件时同样无害。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 非 TextIOWrapper（如被 pytest 替换）
            pass

    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # 无控制台或受限环境，忽略
            pass


def load_env():
    """加载项目根目录的 .env 文件"""
    # 项目根目录：src/agent/../../.env
    project_root = Path(__file__).parent.parent.parent
    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=True)
    else:
        print(f"[warn] .env file not found at {env_file}")


def get_env(key: str, default: str = None) -> str:
    """获取环境变量"""
    return os.getenv(key, default)


def get_env_int(key: str, default: int = 0) -> int:
    """获取整数环境变量"""
    return int(os.getenv(key, str(default)))


# 模块加载时自动加载 .env
load_env()
#加载进去之后可以直接通过 os.getenv(key, default) 获取 key是键  default是默认值
