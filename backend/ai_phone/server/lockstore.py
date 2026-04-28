"""设备占用锁（进程内内存实现，带 TTL 心跳续期）。

规则：
- 一个 serial 同一时刻只能被一个 "holder" 持有
- holder 类型区分：``manual`` (浏览器手动调试) / ``auto`` (VLM Runner 执行 case)
- 锁由 ``acquire`` 获取 token，心跳 ``heartbeat(token)`` 续期，``release(token)``
  释放；超过 TTL 未心跳则自动过期
- 所有接口纯同步 + ``asyncio.Lock`` 保护，够用且零依赖；后面要做分布式改用 Redis

这个文件不依赖 FastAPI，也不依赖 DB，是最小单元，方便独立单测。
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# 心跳 TTL。浏览器每 5s 心跳一次；容忍 3 次丢心跳 → 15s。
DEFAULT_TTL_SECONDS = 15.0


@dataclass
class LockInfo:
    serial: str
    holder: str                 # 持有者标识（浏览器 session id / agent id / run_id）
    holder_type: str            # manual / auto
    token: str                  # 续期/释放用
    acquired_at: float
    last_heartbeat_at: float
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    meta: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.monotonic()
        return (t - self.last_heartbeat_at) > self.ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "serial": self.serial,
            "holder": self.holder,
            "holder_type": self.holder_type,
            "acquired_at": self.acquired_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "ttl_seconds": self.ttl_seconds,
            "meta": dict(self.meta),
        }


class LockConflict(Exception):
    """已被别人持有。"""


class LockNotFound(Exception):
    """锁不存在（可能已超时释放或从未被获取）。"""


class BadToken(Exception):
    """token 对不上。"""


class DeviceLockStore:
    """线程/协程安全的设备占用锁表。"""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._locks: Dict[str, LockInfo] = {}
        # 本 Store 只被单个 asyncio 事件循环里的协程访问，且所有 mutation 段内
        # 都没有 await；asyncio 单任务性已经保证互斥，不再叠 asyncio.Lock。
        # 这样还能规避「Lock 绑定在旧 loop 上导致跨 loop 报错」的问题。

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def peek(self, serial: str, *, now: Optional[float] = None) -> Optional[LockInfo]:
        """不加锁的快速查询；返回的是引用，调用方不要改。过期则视为不存在。"""
        info = self._locks.get(serial)
        if info is None:
            return None
        if info.is_expired(now):
            return None
        return info

    def snapshot(self) -> Dict[str, LockInfo]:
        """当前所有有效锁的副本。"""
        self._reap()
        return {k: v for k, v in self._locks.items()}

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    async def acquire(
        self,
        serial: str,
        holder: str,
        holder_type: str,
        *,
        ttl_seconds: Optional[float] = None,
        meta: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> LockInfo:
        """尝试获取锁。同一 holder 再取视为续期，不同 holder 抛 LockConflict。

        ``force=True`` 可跨 holder 强占（用于紧急释放）。
        """
        now = time.monotonic()
        self._reap(now)

        existing = self._locks.get(serial)
        if existing is not None and not existing.is_expired(now):
            # 新锁模型：互斥只看 holder 身份；holder_type（session/job/webhook/manual/auto）
            # 只是元数据标签，用来展示"谁在占着"。同一 holder 再 acquire 视为续期。
            same_holder = existing.holder == holder
            if same_holder:
                existing.last_heartbeat_at = now
                # holder_type 允许更新（比如浏览器先占，然后升级标签），但不影响 token
                existing.holder_type = holder_type or existing.holder_type
                if meta:
                    existing.meta.update(meta)
                return existing
            if not force:
                raise LockConflict(
                    f"设备 {serial} 已被 {existing.holder_type}:{existing.holder} 占用"
                )

        info = LockInfo(
            serial=serial,
            holder=holder,
            holder_type=holder_type,
            token=secrets.token_hex(16),
            acquired_at=now,
            last_heartbeat_at=now,
            ttl_seconds=ttl_seconds or self._ttl,
            meta=dict(meta or {}),
        )
        self._locks[serial] = info
        return info

    async def heartbeat(self, serial: str, token: str) -> LockInfo:
        info = self._locks.get(serial)
        if info is None or info.is_expired():
            raise LockNotFound(f"设备 {serial} 未被锁定或已超时")
        if info.token != token:
            raise BadToken("锁 token 不匹配")
        info.last_heartbeat_at = time.monotonic()
        return info

    async def release(self, serial: str, token: str, *, force: bool = False) -> bool:
        info = self._locks.get(serial)
        if info is None:
            return False
        if not force and info.token != token:
            raise BadToken("锁 token 不匹配")
        self._locks.pop(serial, None)
        return True

    # ------------------------------------------------------------------
    # 内部：过期清理
    # ------------------------------------------------------------------
    def _reap(self, now: Optional[float] = None) -> None:
        t = now if now is not None else time.monotonic()
        expired = [s for s, info in self._locks.items() if info.is_expired(t)]
        for s in expired:
            self._locks.pop(s, None)


# 进程级单例，FastAPI 里通过 app.state 注入；测试可以直接 new 一个。
_default_store: Optional[DeviceLockStore] = None


def get_default_lock_store() -> DeviceLockStore:
    global _default_store
    if _default_store is None:
        _default_store = DeviceLockStore()
    return _default_store


def reset_default_lock_store() -> None:
    """测试用：重置全局单例。"""
    global _default_store
    _default_store = None
