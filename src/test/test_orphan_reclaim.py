"""孤儿沙箱容器判定回归测试（不依赖 Docker / MongoDB）。

判定错了会删掉用户的活容器，所以 ``_is_orphan`` 必须是纯函数、可表驱动覆盖。
容器来源见 ``src/agent/backends/sandbox_manager.py`` 模块文档。
"""
from __future__ import annotations

import sys
import threading
import types
import unittest

from src.agent.backends.sandbox_manager import (
    SANDBOX_CONTAINER_PREFIX,
    SandboxManager,
)

PREFIX = SANDBOX_CONTAINER_PREFIX


def _is_orphan(name, known=(), protected=()):
    return SandboxManager._is_orphan(name, set(known), set(protected))


class IsOrphanTests(unittest.TestCase):
    """表驱动：每行是 (名字, known, protected, 期望)。"""

    def test_decision_table(self):
        cases = [
            # (说明, name, known, protected, expected)
            ("预热池遗留，无登记 → 孤儿", f"{PREFIX}warm-a41e8524bcfb", [], [], True),
            ("用户沙箱遗留，无登记 → 孤儿", f"{PREFIX}alice-9f2c1d0e", [], [], True),
            ("本进程内存里登记着 → 保留", f"{PREFIX}warm-aaaaaaaaaaaa",
             [f"{PREFIX}warm-aaaaaaaaaaaa"], [], False),
            ("MongoDB sandbox_cache 里登记着 → 保留",
             f"{PREFIX}warm-bbbbbbbbbbbb", [], [f"{PREFIX}warm-bbbbbbbbbbbb"], False),
            ("两个集合都有 → 保留", f"{PREFIX}warm-cccccccccccc",
             [f"{PREFIX}warm-cccccccccccc"], [f"{PREFIX}warm-cccccccccccc"], False),
            ("无前缀（用户自己的容器）→ 保留", "nginx", [], [], False),
            ("无前缀但同名后段 → 保留", "my-erp-sandbox-warm-1", [], [], False),
            ("前缀大小写不符 → 保留", "ERP-SANDBOX-warm-1", [], [], False),
            ("恰好等于前缀（旧 README 手工容器）→ 保留", PREFIX, [], [], False),
            ("空名字 → 保留", "", [], [], False),
        ]
        for label, name, known, protected, expected in cases:
            with self.subTest(label):
                self.assertEqual(_is_orphan(name, known, protected), expected, label)

    def test_other_entries_do_not_shield_orphan(self):
        """known 里登记的是别的容器时，孤儿仍要判定为孤儿。"""
        self.assertTrue(
            _is_orphan(f"{PREFIX}warm-new", known=[f"{PREFIX}warm-old"])
        )


class ReclaimLoopTests(unittest.TestCase):
    """用假 docker 模块跑真实回收循环，确认只删孤儿（不连 Docker）。"""

    def _manager(self):
        # 绕过 __init__：它会拉起预热池，需要 Docker
        manager = object.__new__(SandboxManager)
        manager._entries = {}
        manager._lock = threading.Lock()
        manager._cached_container_names = lambda: {f"{PREFIX}warm-in-mongo"}
        return manager

    def test_only_orphans_are_removed(self):
        removed = []

        def _container(name):
            class _C:
                pass

            c = _C()
            c.name = name

            def _remove(force=False):
                removed.append(name)

            c.stop = lambda timeout=0: None
            c.remove = _remove
            return c

        names = [
            f"{PREFIX}warm-orphan",
            f"{PREFIX}warm-in-mongo",
            "nginx",
        ]

        class _Containers:
            def list(self, all=False, filters=None):
                return [_container(n) for n in names]

        class _Client:
            containers = _Containers()

            def close(self):
                pass

        fake_docker = types.SimpleNamespace(from_env=lambda: _Client())
        sys.modules["docker"] = fake_docker
        self.addCleanup(sys.modules.pop, "docker", None)

        reclaimed = self._manager()._reclaim_orphan_containers()

        self.assertEqual(removed, [f"{PREFIX}warm-orphan"])
        self.assertEqual(reclaimed, 1)

    def test_mongo_unavailable_skips_whole_step(self):
        """MongoDB 读不到时不能"宁删勿留"，必须整步跳过。"""

        def _boom():
            raise ConnectionError("no mongo")

        manager = self._manager()
        manager._cached_container_names = _boom

        class _Docker:
            @staticmethod
            def from_env():
                raise AssertionError("MongoDB 不可用时不应触碰 Docker")

        sys.modules["docker"] = _Docker
        self.addCleanup(sys.modules.pop, "docker", None)

        self.assertEqual(manager._reclaim_orphan_containers(), 0)


if __name__ == "__main__":
    unittest.main()
