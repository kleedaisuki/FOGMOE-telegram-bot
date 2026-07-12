"""@brief 由组合根拥有的有界媒体运行状态 / Composition-owned bounded media runtime state."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.domain.media.identifiers import UserId


class BulkheadFull(RuntimeError):
    """媒体 bulkhead 排队容量耗尽 / Media bulkhead queue capacity exhausted."""


class MonotonicClock(Protocol):
    """@brief 单调时钟端口 / Monotonic-clock port."""

    def __call__(self) -> float:
        """@brief 读取单调秒数 / Read monotonic seconds.

        @return 单调秒数 / Monotonic seconds.
        """

        ...


@dataclass(slots=True)
class _CacheEntry[V]:
    """@brief TTL cache 内部条目 / Internal TTL-cache entry.

    @param value 缓存值 / Cached value.
    @param expires_at 单调过期时间 / Monotonic expiry.
    """

    value: V
    expires_at: float


class BoundedTtlCache[K, V]:
    """@brief 并发安全、LRU 淘汰的有界 TTL cache / Concurrent bounded TTL cache with LRU eviction."""

    def __init__(
        self,
        *,
        capacity: int,
        ttl_seconds: float,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        """@brief 创建 cache / Create the cache.

        @param capacity 最大条目数 / Maximum entries.
        @param ttl_seconds 固定 TTL / Fixed TTL.
        @param clock 单调时钟 / Monotonic clock.
        @raise ValueError 容量或 TTL 非正时抛出 / Raised for non-positive capacity or TTL.
        """

        if capacity <= 0 or ttl_seconds <= 0:
            raise ValueError("capacity and ttl_seconds must be positive")
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[K, _CacheEntry[V]] = OrderedDict()
        """@brief 按 LRU 排序的条目 / Entries ordered by LRU."""
        self._lock = asyncio.Lock()
        """@brief 保护条目的一进程锁 / In-process lock guarding entries."""

    async def get(self, key: K) -> V | None:
        """@brief 读取且提升一个未过期条目 / Read and promote one unexpired entry.

        @param key 缓存键 / Cache key.
        @return 值；缺失或过期时为 None / Value, or None when missing/expired.
        """

        async with self._lock:
            self._purge_expired(self._clock())
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry.value

    async def put(self, key: K, value: V) -> None:
        """@brief 写入并按 LRU 淘汰 / Store and evict by LRU.

        @param key 缓存键 / Cache key.
        @param value 缓存值 / Cache value.
        @return None / None.
        """

        async with self._lock:
            now = self._clock()
            self._purge_expired(now)
            self._entries[key] = _CacheEntry(value, now + self._ttl_seconds)
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    async def discard(self, key: K) -> None:
        """@brief 删除一个条目 / Discard one entry.

        @param key 缓存键 / Cache key.
        @return None / None.
        """

        async with self._lock:
            self._entries.pop(key, None)

    async def size(self) -> int:
        """@brief 返回清理后的条目数 / Return the entry count after expiry cleanup.

        @return 当前条目数 / Current entry count.
        """

        async with self._lock:
            self._purge_expired(self._clock())
            return len(self._entries)

    def _purge_expired(self, now: float) -> None:
        """@brief 在锁内清理过期条目 / Purge expired entries while locked.

        @param now 当前单调时间 / Current monotonic time.
        @return None / None.
        """

        expired = [
            key for key, entry in self._entries.items() if entry.expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)


class SlidingWindowLimiter:
    """@brief 有界用户集合上的并发安全滑动窗口限流器 / Concurrent sliding-window limiter over a bounded user set."""

    def __init__(
        self,
        *,
        capacity: int,
        max_requests: int,
        window_seconds: float,
        cooldown_seconds: float,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        """@brief 创建限流器 / Create the limiter.

        @param capacity 最多跟踪用户数 / Maximum tracked users.
        @param max_requests 窗口内请求上限 / Requests per window.
        @param window_seconds 滑动窗口 / Sliding window.
        @param cooldown_seconds 超限冷却 / Cooldown after rejection.
        @param clock 单调时钟 / Monotonic clock.
        """

        if (
            min(capacity, max_requests) <= 0
            or min(window_seconds, cooldown_seconds) <= 0
        ):
            raise ValueError("limiter bounds must be positive")
        self._capacity = capacity
        self._max_requests = max_requests
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._requests: OrderedDict[UserId, deque[float]] = OrderedDict()
        """@brief 用户请求时间序列 / Per-user request instants."""
        self._cooldowns: dict[UserId, float] = {}
        """@brief 用户冷却截止时间 / Per-user cooldown deadlines."""
        self._lock = asyncio.Lock()
        """@brief 原子判定锁 / Atomic-decision lock."""

    async def admit(self, user_id: UserId) -> tuple[bool, int | None]:
        """@brief 原子判定并记录一次请求 / Atomically decide and record one request.

        @param user_id 用户标识 / User identifier.
        @return ``(allowed, retry_after_seconds)`` / ``(allowed, retry_after_seconds)``.
        """

        async with self._lock:
            now = self._clock()
            cooldown = self._cooldowns.get(user_id)
            if cooldown is not None and cooldown > now:
                return False, max(1, int(cooldown - now + 0.999))
            self._cooldowns.pop(user_id, None)
            requests = self._requests.setdefault(user_id, deque())
            cutoff = now - self._window
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self._max_requests:
                self._cooldowns[user_id] = now + self._cooldown
                return False, max(1, int(self._cooldown + 0.999))
            requests.append(now)
            self._requests.move_to_end(user_id)
            while len(self._requests) > self._capacity:
                evicted, _ = self._requests.popitem(last=False)
                self._cooldowns.pop(evicted, None)
            return True, None


class AsyncBulkhead:
    """@brief 具有显式排队超时的异步 bulkhead / Async bulkhead with an explicit queue timeout."""

    def __init__(self, *, capacity: int, queue_timeout_seconds: float) -> None:
        """@brief 创建 bulkhead / Create the bulkhead.

        @param capacity 最大并发数 / Maximum concurrency.
        @param queue_timeout_seconds 等待槽位超时 / Slot-acquisition timeout.
        """

        if capacity <= 0 or queue_timeout_seconds <= 0:
            raise ValueError("bulkhead bounds must be positive")
        self._semaphore = asyncio.Semaphore(capacity)
        self._queue_timeout = queue_timeout_seconds

    async def run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """@brief 在 bulkhead 内执行操作 / Run an operation inside the bulkhead.

        @param operation 延迟异步操作 / Deferred async operation.
        @return 操作结果 / Operation result.
        @raise BulkheadFull 排队超时时抛出 / Raised when slot acquisition times out.
        """

        try:
            async with asyncio.timeout(self._queue_timeout):
                await self._semaphore.acquire()
        except TimeoutError as error:
            raise BulkheadFull("media bulkhead queue timed out") from error
        try:
            return await operation()
        finally:
            self._semaphore.release()
