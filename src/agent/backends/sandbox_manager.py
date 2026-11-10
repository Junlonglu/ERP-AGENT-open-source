"""
沙箱生命周期管理（Harness 五态模型）

五态流转：
┌─────────┐    认领     ┌─────────┐    持久化    ┌────────────┐
│  预热池  │ ──────────→ │  已认领  │ ──────────→ │ MongoDB缓存 │
│ (WARM)  │             │ (CLAIMED)│             │  (CACHED)  │
└─────────┘             └─────────┘             └────────────┘
     ↑                       │
     │ 补充预热               │ 故障/超时
     │                       ↓
┌─────────┐             ┌─────────┐
│  新建    │ ←────────── │  销毁    │
│ (CREATE)│   预热池空    │(DESTROY)│
└─────────┘             └─────────┘

设计要点：
- 预热池保持 N 个空闲容器，认领时 < 100ms
- MongoDB 持久化 user→container 映射，服务重启不丢失
- 健康检查 + 自动重建故障容器
- 超时回收闲置沙箱（默认 30 分钟）
"""
import os
import socket
import time
import threading
import uuid
from typing import Dict, Optional
from datetime import datetime, timedelta, timezone

from .custom_opensandbox import CustomOpenSandbox
from .sandbox_setup import SandboxConfig, create_secure_sandbox
from ..config import (
    SANDBOX_CLAIM_TTL_MINUTES,
    SANDBOX_IDLE_TIMEOUT_MINUTES,
    SANDBOX_WARM_POOL_SIZE,
)
from ..log_utils import sandbox_logger
from ..observability import measured


# ============================================================
# 沙箱状态枚举
# ============================================================

# 容器名统一前缀：预热池 erp-sandbox-warm-*，用户沙箱 erp-sandbox-<user>-*
SANDBOX_CONTAINER_PREFIX = "erp-sandbox-"

# 容器存活登记表：名字 → {host, pid, updated_at}。
# 内存登记表只对本进程可见，跨进程判定“这个容器是否被别的活进程持有”
# 只能靠外部共享状态，否则第二个进程启动就会删掉活进程的容器。
SANDBOX_REGISTRY_COLLECTION = "sandbox_registry"

# 心跳保鲜期：超过这个时间没有刷新的登记视为陈旧（另一台机器上可能已崩）。
SANDBOX_REGISTRY_TTL_SECONDS = max(60, SANDBOX_IDLE_TIMEOUT_MINUTES * 2) * 60
# 陈旧认领检查很便宜（一次 Mongo 查询 + 一次 docker list），但没必要每分钟做
SANDBOX_STALE_CLAIM_CHECK_SECONDS = 600

# 没有任何登记记录的容器，只有在“足够老”时才敢当孤儿删掉。
# 这个窗口容忍「旧版本进程创建的容器」与「登记写入失败」。
SANDBOX_ORPHAN_MIN_AGE_SECONDS = (
    int(os.getenv("SANDBOX_ORPHAN_MIN_AGE_MINUTES", "120")) * 60
)

# 本进程所在机器（用于区分“同一个 pid 号”是否指同一个进程）
SANDBOX_HOST = socket.gethostname()


def _pid_alive(pid: int) -> bool:
    """pid 对应的进程是否还活着（标准库实现，不引入 psutil）。

    注意 Windows 上不能用 ``os.kill(pid, 0)`` 探测：它在本平台是
    TerminateProcess（直接杀进程）。这里改用 ``OpenProcess(SYNCHRONIZE)``
    + ``WaitForSingleObject(0)``。

    pid 会被系统复用，所以调用方必须同时校验 host；“同机 + pid 活”
    只是“很可能就是当初那个进程”，而滥用这个判定的代价是误删用户容器，
    所以调用方还叠了心跳时间窗口作为第二道保险。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False  # 进程不存在，或没权限（后者几乎不可能）
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但属于其他用户
    except OSError:
        return False
    return True

class SandboxState:
    WARM = "warm"           # 预热池中的空闲容器
    CLAIMED = "claimed"     # 已分配给用户
    CACHED = "cached"       # 已持久化到 MongoDB
    CREATING = "creating"   # 正在创建中
    DESTROYED = "destroyed" # 已销毁


# ============================================================
# 沙箱元数据
# ============================================================

class SandboxEntry:
    """沙箱条目（跟踪容器的完整生命周期）"""

    def __init__(
        self,
        sandbox: CustomOpenSandbox,
        user_id: str = "",
        state: str = SandboxState.WARM,
    ):
        self.sandbox = sandbox
        self.user_id = user_id
        self.state = state
        self.created_at = datetime.now()
        self.claimed_at: Optional[datetime] = None
        self.last_active_at = datetime.now()
        self.active_runs = 0

    def touch(self):
        """更新活跃时间"""
        self.last_active_at = datetime.now()

    @property
    def container_name(self) -> str:
        return self.sandbox.container_name

    @property
    def container_id(self) -> str:
        return self.sandbox.container_id

    def is_healthy(self) -> bool:
        """检查容器是否健康"""
        return self.sandbox.ping()

    def to_dict(self) -> dict:
        """序列化为字典（用于 MongoDB 存储）"""
        return {
            "container_name": self.container_name,
            "container_id": self.container_id,
            "user_id": self.user_id,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "last_active_at": self.last_active_at.isoformat(),
            "active_runs": self.active_runs,
        }


# ============================================================
# 沙箱生命周期管理器
# ============================================================

class SandboxManager:
    """
    沙箱生命周期管理器（Harness 五态模型）

    核心职责：
    1. 预热池管理：保持 N 个空闲容器随时可用
    2. 认领机制：用户请求时从预热池快速分配
    3. MongoDB 缓存：持久化 user→container 映射
    4. 自动新建：预热池空时按需创建
    5. 超时回收 + 故障自动重建
    """

    _instance: Optional['SandboxManager'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        warm_pool_size: Optional[int] = None,
        idle_timeout_minutes: Optional[int] = None,
    ):
        if self._initialized:
            return
        self._initialized = True

        # --- 核心状态 ---
        self._entries: Dict[str, SandboxEntry] = {}   # container_name → entry
        self._user_map: Dict[str, str] = {}           # user_id → container_name
        self._warm_creating = 0                        # 避免并发补池超量创建
        self._orphans_reclaimed = False                # 启动时只扫一次遗留容器
        self._last_stale_claim_check: Optional[datetime] = None

        # --- 配置 ---
        self._warm_pool_size = (
            SANDBOX_WARM_POOL_SIZE if warm_pool_size is None else warm_pool_size
        )
        effective_idle_timeout = (
            SANDBOX_IDLE_TIMEOUT_MINUTES
            if idle_timeout_minutes is None
            else idle_timeout_minutes
        )
        self._idle_timeout = timedelta(minutes=effective_idle_timeout)
        self._lock = threading.Lock()

        sandbox_logger.info(
            f"SandboxManager initialized (warm_pool={self._warm_pool_size}, "
            f"idle_timeout={effective_idle_timeout}min)"
        )

    # ============================================================
    # 预热池管理
    # ============================================================

    def ensure_warm_pool(self):
        """确保预热池中有足够的空闲容器"""
        self._reclaim_orphan_containers_once()
        self._touch_registry()  # 心跳：让别的进程知道这些容器有人用
        with self._lock:
            warm_count = sum(
                1 for e in self._entries.values()
                if e.state == SandboxState.WARM
            )

            needed = self._warm_pool_size - warm_count - self._warm_creating
            if needed <= 0:
                return
            self._warm_creating += needed

            sandbox_logger.info(f"Warm pool refill: need {needed} more containers")

        # 在锁外创建容器（避免长时间持有锁）
        for i in range(needed):
            try:
                entry = self._create_warm_container()
                with self._lock:
                    self._entries[entry.container_name] = entry
                sandbox_logger.info(
                    f"Warm pool container created: {entry.container_name}"
                )
            except Exception as e:
                sandbox_logger.error(f"Failed to create warm pool container: {e}")
            finally:
                with self._lock:
                    self._warm_creating = max(0, self._warm_creating - 1)

    def _reclaim_orphan_containers_once(self):
        """进程内只回收一次遗留容器。"""
        with self._lock:
            if self._orphans_reclaimed:
                return
            self._orphans_reclaimed = True
        self._reclaim_orphan_containers()
        self._reclaim_stale_claims()

    def _cache_docs(self) -> list:
        """``sandbox_cache`` 全量文档（认领过的用户沙箱）。"""
        from pymongo import MongoClient
        import os

        mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
        db_name = os.getenv("MONGODB_DB_NAME", "erp_agent")
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        try:
            return list(client[db_name].sandbox_cache.find({}))
        finally:
            client.close()

    def _cached_container_names(self) -> set:
        """MongoDB 里登记过的容器名。

        这些容器跨进程仍可通过 ``_restore_from_mongodb`` 恢复，不能当孤儿删掉。
        """
        from pymongo import MongoClient
        import os

        mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
        db_name = os.getenv("MONGODB_DB_NAME", "erp_agent")
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        try:
            docs = client[db_name].sandbox_cache.find({}, {"container_name": 1})
            return {d.get("container_name", "") for d in docs} - {""}
        finally:
            client.close()

    @staticmethod
    def _is_orphan(name: str, known: set, protected: set) -> bool:
        """判定容器是否为孤儿（可安全删除）。

        三个条件全部满足才算孤儿，任一条不满足都必须保留：

        - 名字带 ``erp-sandbox-`` 前缀：避免误删同宿主上的其他容器；
        - 不在 ``known``（本进程内存登记表）里：在表里的容器正在被使用；
        - 不在 ``protected``（MongoDB ``sandbox_cache`` 登记的名字）里：
          这些容器跨进程仍可被 ``_restore_from_mongodb`` 恢复成用户沙箱。

        抽成纯函数是为了能离线覆盖判定逻辑——这里判错会删掉用户的活容器。
        """
        if not name or not name.startswith(SANDBOX_CONTAINER_PREFIX):
            return False
        # 名字恰好等于前缀：旧版 README 教用户手工 `docker run --name erp-sandbox`
        # 建的就是这个名字，代码从不产生它，保守保留。
        if name == SANDBOX_CONTAINER_PREFIX:
            return False
        return name not in known and name not in protected

    @staticmethod
    def _should_reclaim(
        name: str,
        *,
        known: set,
        protected: set,
        doc: Optional[dict],
        age_seconds: Optional[float],
        host: str,
        is_pid_alive,
        now: datetime,
        fresh_seconds: Optional[int] = None,
        min_age_seconds: Optional[int] = None,
    ) -> bool:
        """结合存活登记表判定是否该回收（纯函数，完全可离线覆盖）。

        判定顺序与理由：

        1. 前缀 / 内存登记 / Mongo 缓存 —— 复用 ``_is_orphan``；
        2. 有登记且同 host：pid 活 ⇒ 别的活进程持有 ⇒ 保留；pid 死 ⇒ 孤儿；
        3. 有登记但不同 host：看不见对方 pid，只能靠心跳；陈旧的才算孤儿；
        4. 无登记：不知道谁建的，只有“足够老”才当孤儿删（容忍登记写入失败
           与旧版本进程留下的容器）。

        所有“拿不准”的分支一律返回 False——宁可漏清，不可错删。
        """
        if not SandboxManager._is_orphan(name, known, protected):
            return False

        fresh_seconds = (
            SANDBOX_REGISTRY_TTL_SECONDS if fresh_seconds is None else fresh_seconds
        )
        min_age_seconds = (
            SANDBOX_ORPHAN_MIN_AGE_SECONDS
            if min_age_seconds is None
            else min_age_seconds
        )

        if doc:
            if doc.get("host") == host:
                return not is_pid_alive(doc.get("pid") or 0)
            updated = doc.get("updated_at")
            if not isinstance(updated, datetime):
                return False  # 登记字段坏了，宁可保留
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            return (now - updated).total_seconds() > fresh_seconds

        if age_seconds is None:
            return False
        return age_seconds > min_age_seconds

    # -------------------- 存活登记表（MongoDB） --------------------

    def _registry_collection(self):
        """返回 (client, sandbox_registry 集合)；由调用方关闭 client。"""
        from pymongo import MongoClient

        mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
        db_name = os.getenv("MONGODB_DB_NAME", "erp_agent")
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
        return client, client[db_name][SANDBOX_REGISTRY_COLLECTION]

    def _register_container(self, container_name: str) -> None:
        """登记录当前进程持有该容器（失败只告警，不影响主流程）。"""
        try:
            client, coll = self._registry_collection()
            try:
                now = datetime.now(timezone.utc)
                coll.update_one(
                    {"container_name": container_name},
                    {
                        "$set": {
                            "host": SANDBOX_HOST,
                            "pid": os.getpid(),
                            "updated_at": now,
                        },
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )
            finally:
                client.close()
        except Exception as e:
            sandbox_logger.warning(
                f"Failed to register sandbox {container_name}: {e}"
            )

    def _unregister_container(self, container_name: str) -> None:
        try:
            client, coll = self._registry_collection()
            try:
                coll.delete_one({"container_name": container_name})
            finally:
                client.close()
        except Exception as e:
            sandbox_logger.warning(
                f"Failed to unregister sandbox {container_name}: {e}"
            )

    def _touch_registry(self) -> None:
        """心跳：把本进程所有容器的 updated_at 刷新到现在。"""
        with self._lock:
            names = list(self._entries.keys())
        if not names:
            return
        try:
            client, coll = self._registry_collection()
            try:
                coll.update_many(
                    {"container_name": {"$in": names}},
                    {
                        "$set": {
                            "host": SANDBOX_HOST,
                            "pid": os.getpid(),
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )
            finally:
                client.close()
        except Exception as e:
            sandbox_logger.warning(f"Failed to refresh sandbox registry: {e}")

    def _registry_docs(self) -> Dict[str, dict]:
        """读取整张登记表：容器名 → 登记文档。"""
        client, coll = self._registry_collection()
        try:
            return {
                d["container_name"]: d
                for d in coll.find({})
                if d.get("container_name")
            }
        finally:
            client.close()

    @staticmethod
    def _container_age_seconds(attrs: Optional[dict]) -> Optional[float]:
        """从 docker inspect 的 attrs 解析容器年龄（秒）。"""
        created = (attrs or {}).get("Created")
        if not created:
            return None
        try:
            ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def _reclaim_orphan_containers(self) -> int:
        """回收上次进程遗留的沙箱容器。

        容器登记表只在本进程内存（``self._entries``）里，后端重启后旧容器
        无人认领，会一直占着内存和 CPU（历史上每次重启漏一个）。

        但“本进程内存里没有”远不等于“没人用”：预热池与活进程持有的容器
        都不在内存以外任何地方，只看内存会把别的活进程的容器当孤儿删掉。
        所以这里叠三道保护：内存登记表、MongoDB ``sandbox_cache``（可恢复
        的用户沙箱）、MongoDB ``sandbox_registry``（每个容器一行心跳，带
        host + pid）。任何一道读不到就整步跳过：宁可漏清，不可错删。
        """
        try:
            protected = self._cached_container_names()
        except Exception as e:
            sandbox_logger.warning(
                f"Orphan sandbox reclamation skipped (MongoDB cache unavailable): {e}"
            )
            return 0
        try:
            registry = self._registry_docs()
        except Exception as e:
            sandbox_logger.warning(
                f"Orphan sandbox reclamation skipped (registry unavailable): {e}"
            )
            return 0

        reclaimed = 0
        try:
            import docker
            client = docker.from_env()
            try:
                containers = client.containers.list(
                    all=True, filters={"name": SANDBOX_CONTAINER_PREFIX}
                )
                with self._lock:
                    known = set(self._entries.keys())
                now = datetime.now(timezone.utc)
                for container in containers:
                    name = container.name or ""
                    if not self._should_reclaim(
                        name,
                        known=known,
                        protected=protected,
                        doc=registry.get(name),
                        age_seconds=self._container_age_seconds(
                            getattr(container, "attrs", None)
                        ),
                        host=SANDBOX_HOST,
                        is_pid_alive=_pid_alive,
                        now=now,
                    ):
                        continue
                    try:
                        container.stop(timeout=2)
                    except Exception:
                        pass
                    try:
                        container.remove(force=True)
                        self._unregister_container(name)
                        reclaimed += 1
                        sandbox_logger.info(f"Removed orphan sandbox container: {name}")
                    except Exception as e:
                        sandbox_logger.warning(
                            f"Failed to remove orphan sandbox {name}: {e}"
                        )
            finally:
                client.close()
        except Exception as e:
            sandbox_logger.warning(f"Orphan sandbox reclamation skipped: {e}")
        if reclaimed:
            sandbox_logger.info(
                f"Reclaimed {reclaimed} orphan sandbox container(s) on startup"
            )
        return reclaimed

    @staticmethod
    def _doc_time(value) -> Optional[datetime]:
        """把 Mongo 里的时间字段解析成带时区的 datetime（拿不准返回 None）。"""
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return None

    @staticmethod
    def _claim_is_stale(
        doc: dict,
        registry_doc: Optional[dict],
        now: datetime,
        ttl: timedelta,
        *,
        host: str = SANDBOX_HOST,
        is_pid_alive=None,
        self_pid: Optional[int] = None,
    ) -> bool:
        """已认领容器是否已经“陈旧”到可以回收（纯函数，可离线覆盖）。

        两个条件决定保留：
        1. 心跳指向**别的活进程**——那份 ``sandbox_cache`` 是它正在用的沙箱；
        2. 拿不到时间戳——不知道多久没人用，就宁可留着。
        """
        registry_doc = registry_doc or {}
        pid = registry_doc.get("pid")
        self_pid = os.getpid() if self_pid is None else self_pid
        is_pid_alive = _pid_alive if is_pid_alive is None else is_pid_alive
        if (
            registry_doc.get("host") == host
            and pid
            and pid != self_pid
            and is_pid_alive(pid)
        ):
            return False
        last_active = (
            SandboxManager._doc_time(doc.get("last_active_at"))
            or SandboxManager._doc_time(doc.get("claimed_at"))
            or SandboxManager._doc_time(doc.get("created_at"))
        )
        if last_active is None:
            return False
        return (now - last_active) > ttl

    def _reclaim_stale_claims(self, force: bool = False) -> int:
        """回收「已被认领、但长期没人回来」的沙箱容器。

        孤儿回收管不了它们：它们在 ``sandbox_cache`` 里（删了就丢用户文件），
        所以 ``_is_orphan`` 永远返回 False；而 ``cleanup_idle_sandboxes`` 只遍历
        本进程内存，重启后这些容器已经不在表里，于是两边都不管——
        每换一个用户、每重启一次就永久漏一个。

        这里的口径是：心跳指向的进程已死（或就是本进程且已不在内存），
        并且 ``last_active_at`` 超过 ``SANDBOX_CLAIM_TTL_MINUTES``，才连容器带
        缓存一起回收。容器已经不在了就只清缓存。
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            if not force and self._last_stale_claim_check is not None:
                elapsed = (now - self._last_stale_claim_check).total_seconds()
                if elapsed < SANDBOX_STALE_CLAIM_CHECK_SECONDS:
                    return 0
            self._last_stale_claim_check = now

        try:
            cache_docs = self._cache_docs()
            registry = self._registry_docs()
        except Exception as e:
            sandbox_logger.warning(
                f"Stale claim reclamation skipped (MongoDB unavailable): {e}"
            )
            return 0

        ttl = timedelta(minutes=SANDBOX_CLAIM_TTL_MINUTES)
        with self._lock:
            known = set(self._entries.keys())

        reclaimed = 0
        try:
            import docker
            client = docker.from_env()
            try:
                for doc in cache_docs:
                    doc = doc or {}
                    name = doc.get("container_name") or ""
                    user_id = doc.get("user_id") or ""
                    if not name.startswith(SANDBOX_CONTAINER_PREFIX) or name in known:
                        continue
                    if not self._claim_is_stale(
                        doc, registry.get(name), now, ttl, self_pid=os.getpid()
                    ):
                        continue
                    try:
                        container = client.containers.get(name)
                    except Exception:
                        self._cleanup_mongodb_cache(user_id)
                        sandbox_logger.info(
                            f"Dropped stale sandbox cache for missing container: {name}"
                        )
                        continue
                    try:
                        container.stop(timeout=2)
                    except Exception:
                        pass
                    try:
                        container.remove(force=True)
                    except Exception as e:
                        sandbox_logger.warning(
                            f"Failed to remove stale claimed sandbox {name}: {e}"
                        )
                        continue
                    self._unregister_container(name)
                    self._cleanup_mongodb_cache(user_id)
                    reclaimed += 1
                    sandbox_logger.info(
                        f"Reclaimed stale claimed sandbox: {name} (user={user_id}, "
                        f"ttl={SANDBOX_CLAIM_TTL_MINUTES}min)"
                    )
            finally:
                client.close()
        except Exception as e:
            sandbox_logger.warning(f"Stale claim reclamation skipped: {e}")
        if reclaimed:
            sandbox_logger.info(
                f"Reclaimed {reclaimed} stale claimed sandbox container(s)"
            )
        return reclaimed

    def _create_warm_container(self) -> SandboxEntry:
        """创建一个预热容器"""
        # 生成唯一容器名
        name = f"erp-sandbox-warm-{uuid.uuid4().hex[:12]}"

        # python:3.11-slim 已带 Python；图表和 Skill 依赖按需安装到
        # /workspace/python-packages，预热阶段不做耗时网络安装。
        config = SandboxConfig(name=name, runtimes=[])
        sandbox = create_secure_sandbox(config)
        sandbox.execute(
            "mkdir -p /workspace /workspace/data /workspace/analysis "
            "/workspace/output /skills"
        )
        # 登记存活心跳：否则别的进程启动时会把它当成孤儿删掉
        self._register_container(name)

        return SandboxEntry(sandbox=sandbox, state=SandboxState.WARM)

    # ============================================================
    # 认领机制（用户请求沙箱）
    # ============================================================

    def get_sandbox(self, user_id: str) -> CustomOpenSandbox:
        """
        获取用户的沙箱实例

        优先级：
        1. 已有且健康 → 直接返回
        2. MongoDB 有缓存且健康 → 恢复
        3. 预热池有空闲 → 认领
        4. 新建容器
        """
        with self._lock:
            # 1. 检查用户是否已有沙箱
            container_name = self._user_map.get(user_id)
            if container_name and container_name in self._entries:
                entry = self._entries[container_name]
                if entry.is_healthy():
                    entry.touch()
                    sandbox_logger.debug(f"Returning existing sandbox for {user_id}")
                    return entry.sandbox
                else:
                    # 容器故障，清理并重建
                    sandbox_logger.warning(
                        f"Sandbox unhealthy for {user_id}, will rebuild"
                    )
                    self._remove_entry(entry)

        # 2. 服务重启后优先恢复已有容器，避免先认领预热容器导致旧容器孤儿化。
        restored = self._restore_from_mongodb(user_id)
        if restored:
            return restored

        # 3. 尝试从预热池认领
        claimed = self._claim_from_warm_pool(user_id)
        if claimed:
            return claimed

        # 4. 新建容器
        sandbox_logger.info(f"No warm/cached sandbox for {user_id}, creating new")
        return self._create_for_user(user_id)

    @measured('sandbox.claim_attempt')
    def _claim_from_warm_pool(self, user_id: str) -> Optional[CustomOpenSandbox]:
        """从预热池中认领一个空闲容器"""
        with self._lock:
            for entry in self._entries.values():
                if entry.state == SandboxState.WARM and entry.is_healthy():
                    # 认领！
                    entry.user_id = user_id
                    entry.state = SandboxState.CLAIMED
                    entry.claimed_at = datetime.now()
                    entry.touch()
                    self._user_map[user_id] = entry.container_name

                    sandbox_logger.info(
                        f"Claimed warm sandbox {entry.container_name} for {user_id}"
                    )

                    # 异步持久化到 MongoDB
                    self._persist_to_mongodb(entry)

                    # 补充预热池
                    threading.Thread(
                        target=self.ensure_warm_pool,
                        daemon=True,
                    ).start()

                    return entry.sandbox

        return None

    @measured('sandbox.create')
    def _create_for_user(self, user_id: str) -> CustomOpenSandbox:
        """为用户创建新容器"""
        # 正常 Web 链路由 AgentLoader 串行创建；这里仍做二次检查，防止其他调用方
        # 并发进入 Manager 时各自创建并覆盖同一 user_id 的映射。
        with self._lock:
            current_name = self._user_map.get(user_id)
            current = self._entries.get(current_name) if current_name else None
            if current is not None and current.is_healthy():
                current.touch()
                return current.sandbox

        safe_user_id = "".join(c if c.isalnum() else "_" for c in user_id)
        name = f"erp-sandbox-{safe_user_id}-{uuid.uuid4().hex[:12]}"

        config = SandboxConfig(name=name, runtimes=[])
        sandbox = create_secure_sandbox(config)
        sandbox.execute(
            "mkdir -p /workspace /workspace/data /workspace/analysis "
            "/workspace/output /skills"
        )

        entry = SandboxEntry(
            sandbox=sandbox,
            user_id=user_id,
            state=SandboxState.CLAIMED,
        )
        entry.claimed_at = datetime.now()
        self._register_container(name)

        raced_entry = None
        with self._lock:
            current_name = self._user_map.get(user_id)
            current = self._entries.get(current_name) if current_name else None
            if current is not None and current.is_healthy():
                current.touch()
                raced_entry = current
            else:
                self._entries[name] = entry
                self._user_map[user_id] = name

        if raced_entry is not None:
            # 有并发调用已经抢先成功，锁外清理自己刚创建的多余容器。
            try:
                sandbox.destroy_container()
            except Exception:
                pass
            return raced_entry.sandbox

        self._persist_to_mongodb(entry)
        sandbox_logger.info(f"New sandbox created for {user_id}: {name}")
        return sandbox

    # ============================================================
    # MongoDB 缓存（持久化 user→container 映射）
    # ============================================================

    def _persist_to_mongodb(self, entry: SandboxEntry):
        """将沙箱映射持久化到 MongoDB"""
        try:
            from pymongo import MongoClient
            import os
            mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
            db_name = os.getenv("MONGODB_DB_NAME", "erp_agent")

            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
            db = client[db_name]

            db.sandbox_cache.update_one(
                {"user_id": entry.user_id},
                {"$set": entry.to_dict()},
                upsert=True,
            )
            entry.state = SandboxState.CACHED
            client.close()
            sandbox_logger.debug(f"Persisted sandbox cache for {entry.user_id}")
        except Exception as e:
            sandbox_logger.warning(f"Failed to persist sandbox cache: {e}")

    def _restore_from_mongodb(self, user_id: str) -> Optional[CustomOpenSandbox]:
        """从 MongoDB 恢复沙箱映射"""
        try:
            from pymongo import MongoClient
            import os
            import docker
            mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
            db_name = os.getenv("MONGODB_DB_NAME", "erp_agent")

            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
            db = client[db_name]

            doc = db.sandbox_cache.find_one({"user_id": user_id})
            client.close()

            if not doc:
                return None

            container_name = doc.get("container_name", "")
            sandbox_logger.info(
                f"Found cached sandbox mapping: {user_id} -> {container_name}"
            )

            # 尝试连接已有容器
            docker_client = docker.from_env()
            try:
                container = docker_client.containers.get(container_name)
                if container.status == "running":
                    sandbox = CustomOpenSandbox(container_name=container_name)
                    entry = SandboxEntry(
                        sandbox=sandbox,
                        user_id=user_id,
                        state=SandboxState.CACHED,
                    )
                    entry.claimed_at = datetime.now()

                    with self._lock:
                        self._entries[container_name] = entry
                        self._user_map[user_id] = container_name

                    # 认领回本进程：更新登记表里的 host/pid，否则旧 pid 已死，
                    # 别的进程会把这个正在用的容器当孤儿删掉。
                    self._register_container(container_name)

                    sandbox_logger.info(
                        f"Restored sandbox from MongoDB cache: {container_name}"
                    )
                    return sandbox
                else:
                    # 容器已停止，清理缓存
                    sandbox_logger.info(
                        f"Cached container not running ({container.status}), cleaning cache"
                    )
                    self._cleanup_mongodb_cache(user_id)
            except docker.errors.NotFound:
                sandbox_logger.info(
                    f"Cached container not found, cleaning cache"
                )
                self._cleanup_mongodb_cache(user_id)
            finally:
                docker_client.close()

        except Exception as e:
            sandbox_logger.warning(f"Failed to restore from MongoDB cache: {e}")

        return None

    def _cleanup_mongodb_cache(self, user_id: str):
        """清理 MongoDB 中的无效缓存"""
        try:
            from pymongo import MongoClient
            import os
            mongo_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
            db_name = os.getenv("MONGODB_DB_NAME", "erp_agent")

            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
            db = client[db_name]
            db.sandbox_cache.delete_one({"user_id": user_id})
            client.close()
        except Exception as e:
            sandbox_logger.warning(f"Failed to cleanup MongoDB cache: {e}")

    # ============================================================
    # 重建 / 销毁
    # ============================================================

    def rebuild(self, user_id: str = "default_user") -> CustomOpenSandbox:
        """重建用户沙箱（故障恢复）"""
        active_runs = 0
        with self._lock:
            container_name = self._user_map.get(user_id)
            if container_name and container_name in self._entries:
                entry = self._entries[container_name]
                active_runs = entry.active_runs
                self._remove_entry(entry)

        sandbox = self._create_for_user(user_id)
        if active_runs:
            with self._lock:
                replacement = self._entries.get(self._user_map.get(user_id, ""))
                if replacement is not None:
                    replacement.active_runs = active_runs
        return sandbox

    def destroy_user_sandbox(self, user_id: str):
        """销毁用户沙箱"""
        with self._lock:
            container_name = self._user_map.pop(user_id, None)
            if container_name and container_name in self._entries:
                entry = self._entries[container_name]
                self._remove_entry(entry)
                self._cleanup_mongodb_cache(user_id)
                sandbox_logger.info(f"Sandbox destroyed for user: {user_id}")

    def destroy_all(self):
        """销毁所有沙箱"""
        with self._lock:
            for entry in list(self._entries.values()):
                try:
                    entry.sandbox.destroy_container()
                except Exception:
                    pass
                self._unregister_container(entry.container_name)
            self._entries.clear()
            self._user_map.clear()
        sandbox_logger.info("All sandboxes destroyed")

    # ============================================================
    # 超时回收
    # ============================================================

    def cleanup_idle_sandboxes(self):
        """回收超时的空闲沙箱"""
        now = datetime.now()
        with self._lock:
            for entry in list(self._entries.values()):
                if entry.state in (SandboxState.CLAIMED, SandboxState.CACHED):
                    if entry.active_runs > 0:
                        continue
                    idle_time = now - entry.last_active_at
                    if idle_time > self._idle_timeout:
                        sandbox_logger.info(
                            f"Reclaiming idle sandbox: {entry.container_name} "
                            f"(idle {idle_time.total_seconds()/60:.0f}min)"
                        )
                        user_id = entry.user_id
                        self._remove_entry(entry)
                        if user_id:
                            self._user_map.pop(user_id, None)
                            self._cleanup_mongodb_cache(user_id)
        # 内存表的回收管不到“重启前就已认领”的容器，单独按 TTL 扫一遍。
        self._reclaim_stale_claims()

    # ============================================================
    # 健康检查
    # ============================================================

    def get_active_container(self, user_id: str = "default_user") -> Optional[str]:
        """获取用户当前活跃的容器ID（供健康检查中间件使用）"""
        with self._lock:
            container_name = self._user_map.get(user_id)
            if container_name and container_name in self._entries:
                entry = self._entries[container_name]
                if entry.is_healthy():
                    return entry.container_id
        return None

    def touch(self, user_id: str) -> None:
        """记录某个用户沙箱仍在被使用，供健康检查链路调用。"""
        with self._lock:
            container_name = self._user_map.get(user_id)
            entry = self._entries.get(container_name) if container_name else None
            if entry is not None:
                entry.touch()

    def acquire_lease(self, user_id: str) -> bool:
        """标记正在运行的 Agent 流，防止超时回收误删工作中的容器。"""
        with self._lock:
            container_name = self._user_map.get(user_id)
            entry = self._entries.get(container_name) if container_name else None
            if entry is None:
                return False
            entry.active_runs += 1
            entry.touch()
            return True

    def release_lease(self, user_id: str) -> None:
        """释放运行租约；异常清理路径也不会让计数变为负数。"""
        with self._lock:
            container_name = self._user_map.get(user_id)
            entry = self._entries.get(container_name) if container_name else None
            if entry is not None:
                entry.active_runs = max(0, entry.active_runs - 1)
                entry.touch()

    def health_check_all(self):
        """检查所有沙箱健康状态"""
        with self._lock:
            unhealthy = []
            for name, entry in self._entries.items():
                if not entry.is_healthy():
                    unhealthy.append(entry)

        # 在锁外重建故障容器
        for entry in unhealthy:
            sandbox_logger.warning(
                f"Unhealthy sandbox detected: {entry.container_name} "
                f"(user={entry.user_id})"
            )
            if entry.user_id:
                self.rebuild(entry.user_id)

    # ============================================================
    # 内部工具
    # ============================================================

    def _remove_entry(self, entry: SandboxEntry):
        """移除并销毁一个条目（必须在锁内调用）"""
        try:
            entry.sandbox.destroy_container()
        except Exception:
            pass
        entry.state = SandboxState.DESTROYED
        self._entries.pop(entry.container_name, None)
        if entry.user_id:
            self._user_map.pop(entry.user_id, None)
        self._unregister_container(entry.container_name)


# ============================================================
# 全局单例
# ============================================================

sandbox_manager = SandboxManager()
