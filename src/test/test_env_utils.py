"""控制台编码工具的回归测试。

背景：Windows 控制台默认 GBK，服务启动日志里的 emoji 会让进程在第一步就抛
UnicodeEncodeError 死掉（曾要求启动前必须手工设置 PYTHONIOENCODING=utf-8）。
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from src.agent.env_utils import ensure_utf8_stdio


class EnsureUtf8StdioTests(unittest.TestCase):
    def test_switches_stream_encoding_to_utf8(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        with patch("sys.stdout", stream):
            ensure_utf8_stdio()
            self.assertEqual(stream.encoding, "utf-8")
            stream.write("🚀 ⚠️ ✓")  # 修复前这里会抛 UnicodeEncodeError
            stream.flush()

    def test_survives_stream_without_reconfigure(self):
        # pytest / 测试框架可能把 stdout 换成没有 reconfigure 的对象，不能因此崩溃
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            ensure_utf8_stdio()


if __name__ == "__main__":
    unittest.main()
