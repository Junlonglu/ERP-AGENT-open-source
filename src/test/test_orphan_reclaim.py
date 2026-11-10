"""孤儿沙箱容器判定回归测试（不依赖 Docker / MongoDB）。

判定错了会删掉用户的活容器（本文件诞生前真的发生过一次），所以判定逻辑
被拆成两个纯函数逐条覆盖：

- ``_is_orphan``：前缀 / 内存登记表 / MongoDB sandbox_cache 三道；
- ``_should_reclaim``：在上面三道之上再叠 sandbox_registry 的 host + pid 心跳。

约定：所有「拿不准」的分支一律保留（宁可漏清，不可错删）。
"""
from __future__ import annotations

import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.agent.backends.sandbox_manager import (
    SANDBOX_CONTAINER_PREFIX,
    SANDBOX_HOST,
    SANDBOX_REGISTRY_TTL_SECONDS,
    SandboxManager,
)

PREFIX = SANDBOX_CONTAINER_PREFIX
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
HOST = "test-host"


def _is_orphan(name, known=(), protected=()):
    return SandboxManager._is_orphan(name, set(known), set(protected))


def _should_reclaim(
    name,
    *,
    known=(),
    protected=(),
    doc=None,
    age_seconds=None,
    host=HOST,
    alive=False,
    now=NOW,
):
    return SandboxManager._should_reclaim(
        name,
        known=set(known),
        protected=set(protected),
        doc=doc,
        age_seconds=age_seconds,
        host=host,
        is_pid_alive=lambda pid: alive,
        now=now,
    )


def _doc(pid=1234, host=HOST, age_seconds=0):
    return {
        "container_name": f"{PREFIX}warm-x",
        "host": host,
        "pid": pid,
        "updated_at": NOW - timedelta(seconds=age_seconds),
    }


class IsOrphanTests(unittest.TestCase):
    """前置三道：前缀、本进程内存、MongoDB sandbox_cache。"""

    def test_decision_table(self):
        cases = [
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


class RegistryDecisionTests(unittest.TestCase):
    """叠加 sandbox_registry 之后的判定（这是误删活容器的真正防线）。"""

    NAME = f"{PREFIX}warm-9f8e7d6c5b4a"

    def test_same_host_live_pid_is_kept(self):
        """同机 + pid 活 = 另一个活进程持有 → 必须保留（历史 bug 现场）。"""
        self.assertFalse(
            _should_reclaim(self.NAME, doc=_doc(pid=1234), alive=True, age_seconds=99 * 3600)
        )

    def test_same_host_dead_pid_is_reclaimed(self):
        """同机 + pid 已死 = 崩溃遗留 → 回收。"""
        self.assertTrue(
            _should_reclaim(self.NAME, doc=_doc(pid=1234), alive=False, age_seconds=1)
        )

    def test_other_host_fresh_heartbeat_is_kept(self):
        """别的机器上心跳还新鲜 → 保留，不跨机抢删。"""
        self.assertFalse(
            _should_reclaim(
                self.NAME,
                doc=_doc(host="other-host", age_seconds=SANDBOX_REGISTRY_TTL_SECONDS - 60),
                alive=False,
            )
        )

    def test_other_host_stale_heartbeat_is_reclaimed(self):
        """别的机器上心跳过期 → 回收。"""
        self.assertTrue(
            _should_reclaim(
                self.NAME,
                doc=_doc(host="other-host", age_seconds=SANDBOX_REGISTRY_TTL_SECONDS + 60),
                alive=False,
            )
        )

    def test_other_host_malformed_doc_is_kept(self):
        """登记字段损坏 → 拿不准 → 保留。"""
        doc = {"container_name": self.NAME, "host": "other-host", "pid": 1}
        self.assertFalse(_should_reclaim(self.NAME, doc=doc, alive=False))

    def test_naive_datetime_is_treated_as_utc(self):
        """MongoDB 可能返回 naive datetime，不能因此误判为过期或崩溃。"""
        doc = _doc(host="other-host", age_seconds=60)
        doc["updated_at"] = doc["updated_at"].replace(tzinfo=None)
        self.assertFalse(_should_reclaim(self.NAME, doc=doc, alive=False))

    def test_unregistered_young_container_is_kept(self):
        """没有任何登记 + 容器还很年轻 → 保留（容忍登记写入失败）。"""
        self.assertFalse(_should_reclaim(self.NAME, doc=None, age_seconds=600))

    def test_unregistered_old_container_is_reclaimed(self):
        """没有任何登记 + 容器很老 → 孤儿（旧版本进程留下的泄漏）。"""
        self.assertTrue(_should_reclaim(self.NAME, doc=None, age_seconds=48 * 3600))

    def test_unknown_age_unregistered_is_kept(self):
        """解析不出创建时间 → 保留。"""
        self.assertFalse(_should_reclaim(self.NAME, doc=None, age_seconds=None))

    def test_known_or_protected_still_wins_over_registry(self):
        """前置三道优先级最高：内存/缓存里有的，即使登记说 pid 已死也保留。"""
        doc = _doc(pid=1)
        self.assertFalse(
            _should_reclaim(self.NAME, known=[self.NAME], doc=doc, alive=False)
        )
        self.assertFalse(
            _should_reclaim(self.NAME, protected=[self.NAME], doc=doc, alive=False)
        )

    def test_container_age_parsing(self):
        """docker inspect 的 Created 是 RFC3339 带 Z 的字符串。"""
        created = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        age = SandboxManager._container_age_seconds({"Created": created})
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age / 3600, 3, delta=0.1)

    def test_container_age_parsing_failures(self):
        for attrs in (None, {}, {"Created": "not-a-date"}):
            with self.subTest(attrs=attrs):
                self.assertIsNone(SandboxManager._container_age_seconds(attrs))


class PidAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        import os

        from src.agent.backends.sandbox_manager import _pid_alive

        self.assertTrue(_pid_alive(os.getpid()))

    def test_invalid_pids_are_dead(self):
        from src.agent.backends.sandbox_manager import _pid_alive

        for pid in (0, -1, None, "abc"):
            with self.subTest(pid=pid):
                self.assertFalse(_pid_alive(pid))

    def test_unused_pid_is_dead(self):
        from src.agent.backends.sandbox_manager import _pid_alive

        # 取一个几乎不可能存在的 pid（Windows 上限 2^22，Linux 上限 2^22 默认）
        self.assertFalse(_pid_alive(4194303))


class ReclaimLoopTests(unittest.TestCase):
    """用假 docker 模块跑真实回收循环，确认只删孤儿（不连 Docker）。"""

    def _manager(self, registry=None):
        # 绕过 __init__：它会拉起预热池，需要 Docker
        manager = object.__new__(SandboxManager)
        manager._entries = {}
        manager._lock = threading.Lock()
        manager._cached_container_names = lambda: {f"{PREFIX}warm-in-mongo"}
        manager._registry_docs = lambda: dict(registry or {})
        manager._unregistered = []
        manager._unregister_container = manager._unregistered.append
        return manager

    def _install_fake_docker(self, names, attrs=None):
        removed = []
        attrs = attrs or {}

        def _container(name):
            class _C:
                pass

            c = _C()
            c.name = name
            c.attrs = attrs.get(name, {})

            def _remove(force=False):
                removed.append(name)

            c.stop = lambda timeout=0: None
            c.remove = _remove
            return c

        class _Containers:
            def list(self, all=False, filters=None):
                return [_container(n) for n in names]

        class _Client:
            containers = _Containers()

            def close(self):
                pass

        sys.modules["docker"] = types.SimpleNamespace(from_env=lambda: _Client())
        self.addCleanup(sys.modules.pop, "docker", None)
        return removed

    def test_only_orphans_are_removed(self):
        old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        freed = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        names = [
            f"{PREFIX}warm-orphan",       # 无登记、很老 → 删
            f"{PREFIX}warm-young",        # 无登记、很年轻 → 留
            f"{PREFIX}warm-live",         # 登记里 pid 活 → 留
            f"{PREFIX}warm-in-mongo",     # sandbox_cache 有 → 留
            "nginx",                      # 无前缀 → 留
        ]
        manager = self._manager(
            registry={
                f"{PREFIX}warm-live": {**dict(_doc()), "host": SANDBOX_HOST,
                                       "pid": _current_pid()},
            }
        )
        removed = self._install_fake_docker(
            names,
            attrs={
                f"{PREFIX}warm-orphan": {"Created": old},
                f"{PREFIX}warm-young": {"Created": freed},
            },
        )

        reclaimed = manager._reclaim_orphan_containers()

        self.assertEqual(removed, [f"{PREFIX}warm-orphan"])
        self.assertEqual(reclaimed, 1)
        # 删掉的容器要从登记表移除，否则表会一直长大
        self.assertEqual(manager._unregistered, [f"{PREFIX}warm-orphan"])

    def test_mongo_unavailable_skips_whole_step(self):
        """sandbox_cache 读不到时不能「宁删勿留」，必须整步跳过。"""

        def _boom():
            raise ConnectionError("no mongo")

        manager = self._manager()
        manager._cached_container_names = _boom
        manager._registry_docs = lambda: (_ for _ in ()).throw(
            ConnectionError("no mongo")
        )

        class _Docker:
            @staticmethod
            def from_env():
                raise AssertionError("MongoDB 不可用时不应触碰 Docker")

        sys.modules["docker"] = _Docker
        self.addCleanup(sys.modules.pop, "docker", None)

        self.assertEqual(manager._reclaim_orphan_containers(), 0)

    def test_registry_unavailable_skips_whole_step(self):
        """登记表读不到时同样整步跳过（此时无法判断别的进程是否在跑）。"""

        def _boom():
            raise ConnectionError("no registry")

        manager = self._manager()
        manager._registry_docs = _boom

        class _Docker:
            @staticmethod
            def from_env():
                raise AssertionError("登记表不可用时不应触碰 Docker")

        sys.modules["docker"] = _Docker
        self.addCleanup(sys.modules.pop, "docker", None)

        self.assertEqual(manager._reclaim_orphan_containers(), 0)


def _current_pid():
    import os

    return os.getpid()


class ClaimStalenessTests(unittest.TestCase):
    """``_claim_is_stale``：已认领缓存的 TTL 判定。"""

    TTL = timedelta(minutes=120)

    def _stale(self, doc, registry_doc=None, *, alive=False, self_pid=None):
        return SandboxManager._claim_is_stale(
            doc,
            registry_doc,
            NOW,
            self.TTL,
            host=HOST,
            is_pid_alive=lambda pid: alive,
            self_pid=1234 if self_pid is None else self_pid,
        )

    def test_other_live_process_keeps_its_sandbox(self):
        doc = {"last_active_at": (NOW - timedelta(days=30)).isoformat()}
        self.assertFalse(self._stale(doc, {"host": HOST, "pid": 4321}, alive=True))

    def test_dead_process_past_ttl_is_stale(self):
        doc = {"last_active_at": (NOW - timedelta(minutes=121)).isoformat()}
        self.assertTrue(self._stale(doc, {"host": HOST, "pid": 4321}))

    def test_dead_process_within_ttl_is_kept(self):
        doc = {"last_active_at": (NOW - timedelta(minutes=119)).isoformat()}
        self.assertFalse(self._stale(doc, {"host": HOST, "pid": 4321}))

    def test_own_pid_past_ttl_is_stale(self):
        # 本进程已经不在内存表里（销毁/重建后遗留的缓存） → 可以回改
        doc = {"last_active_at": (NOW - timedelta(days=2)).isoformat()}
        self.assertTrue(self._stale(doc, {"host": HOST, "pid": 1234}, alive=True))

    def test_foreign_host_relies_on_ttl(self):
        doc = {"last_active_at": (NOW - timedelta(days=2)).isoformat()}
        self.assertTrue(self._stale(doc, {"host": "other-host", "pid": 4321}, alive=True))

    def test_falls_back_to_claimed_at_then_created_at(self):
        old = (NOW - timedelta(days=2)).isoformat()
        self.assertTrue(self._stale({"claimed_at": old}))
        self.assertTrue(self._stale({"created_at": old}))

    def test_datetime_values_are_accepted(self):
        doc = {"last_active_at": datetime.now(timezone.utc) - timedelta(days=2)}
        self.assertTrue(self._stale(doc))

    def test_missing_or_broken_timestamps_are_kept(self):
        for doc in ({}, {"last_active_at": None}, {"last_active_at": "not-a-date"}, {"claimed_at": 12345}):
            with self.subTest(doc=doc):
                self.assertFalse(self._stale(doc))


class StaleClaimLoopTests(unittest.TestCase):
    """跑真实回收循环（假 docker + 假 Mongo 文档），确认只删陈旧认领容器。"""

    def _manager(self, cache_docs, registry=None):
        manager = object.__new__(SandboxManager)
        manager._entries = {}
        manager._lock = threading.Lock()
        manager._last_stale_claim_check = None
        manager._cache_docs = lambda: list(cache_docs)
        manager._registry_docs = lambda: dict(registry or {})
        manager._unregistered = []
        manager._unregister_container = manager._unregistered.append
        manager._dropped_caches = []
        manager._cleanup_mongodb_cache = manager._dropped_caches.append
        return manager

    def _install_fake_docker(self, existing):
        removed = []

        class _C:
            def __init__(self, name):
                self.name = name

            def stop(self, timeout=0):
                return None

            def remove(self, force=False):
                removed.append(self.name)

        class _Containers:
            def get(self, name):
                if name not in existing:
                    raise RuntimeError(f"no such container: {name}")
                return _C(name)

        class _Client:
            containers = _Containers()

            def close(self):
                pass

        sys.modules["docker"] = types.SimpleNamespace(from_env=lambda: _Client())
        self.addCleanup(sys.modules.pop, "docker", None)
        return removed

    def test_reclaims_stale_claim_and_cache_row(self):
        name = f"{PREFIX}warm-stale"
        doc = {
            "user_id": "gone-user",
            "container_name": name,
            "last_active_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
        }
        manager = self._manager([doc], {name: {"host": SANDBOX_HOST, "pid": 999999}})
        removed = self._install_fake_docker(existing={name})

        self.assertEqual(manager._reclaim_stale_claims(force=True), 1)
        self.assertEqual(removed, [name])
        self.assertEqual(manager._unregistered, [name])
        self.assertEqual(manager._dropped_caches, ["gone-user"])

    def test_keeps_container_held_by_live_process(self):
        name = f"{PREFIX}warm-live"
        doc = {
            "user_id": "busy-user",
            "container_name": name,
            "last_active_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        }
        manager = self._manager([doc], {name: {"host": SANDBOX_HOST, "pid": 4321}})
        removed = self._install_fake_docker(existing={name})

        # 另一个活进程持有这个容器：再旧也不能动
        with patch("src.agent.backends.sandbox_manager._pid_alive", return_value=True):
            self.assertEqual(manager._reclaim_stale_claims(force=True), 0)
        self.assertEqual(removed, [])
        self.assertEqual(manager._dropped_caches, [])

    def test_missing_container_only_drops_cache(self):
        name = f"{PREFIX}warm-ghost"
        doc = {
            "user_id": "ghost-user",
            "container_name": name,
            "last_active_at": (datetime.now(timezone.utc) - timedelta(days=9)).isoformat(),
        }
        manager = self._manager([doc], {})
        removed = self._install_fake_docker(existing=set())

        self.assertEqual(manager._reclaim_stale_claims(force=True), 0)
        self.assertEqual(removed, [])
        self.assertEqual(manager._dropped_caches, ["ghost-user"])

    def test_throttled_between_calls(self):
        manager = self._manager([], {})
        self._install_fake_docker(existing=set())
        self.assertEqual(manager._reclaim_stale_claims(force=True), 0)
        manager._cache_docs = lambda: (_ for _ in ()).throw(AssertionError("不应再次查 Mongo"))
        self.assertEqual(manager._reclaim_stale_claims(), 0)

    def test_mongo_unavailable_touches_nothing(self):
        manager = self._manager([], {})
        manager._cache_docs = lambda: (_ for _ in ()).throw(RuntimeError("mongo down"))
        removed = self._install_fake_docker(existing={f"{PREFIX}warm-x"})

        self.assertEqual(manager._reclaim_stale_claims(force=True), 0)
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
