"""@brief 媒体 runtime controls 的并发与容量测试 / Concurrency and capacity tests for media runtime controls."""

import asyncio

import pytest

from fogmoe_bot.application.media.runtime import (
    AsyncBulkhead,
    BoundedTtlCache,
    BulkheadFull,
    SlidingWindowLimiter,
)
from fogmoe_bot.domain.media.identifiers import UserId


class FakeClock:
    """@brief 可推进单调时钟 / Advanceable monotonic clock."""

    def __init__(self) -> None:
        """@brief 从零开始 / Start at zero."""

        self.value = 0.0

    def __call__(self) -> float:
        """@brief 返回当前值 / Return the current value."""

        return self.value


def test_bounded_ttl_cache_evicts_lru_and_expired_values() -> None:
    """@brief cache 同时执行容量和 TTL 边界 / Cache enforces capacity and TTL bounds."""

    async def scenario() -> None:
        clock = FakeClock()
        cache = BoundedTtlCache[str, int](capacity=2, ttl_seconds=10, clock=clock)
        await cache.put("a", 1)
        await cache.put("b", 2)
        assert await cache.get("a") == 1
        await cache.put("c", 3)
        assert await cache.get("b") is None
        assert await cache.size() == 2
        clock.value = 11
        assert await cache.get("a") is None
        assert await cache.get("c") is None
        assert await cache.size() == 0

    asyncio.run(scenario())


def test_sliding_window_limiter_is_atomic_under_concurrency() -> None:
    """@brief 并发准入不会超过配置容量 / Concurrent admission never exceeds configured capacity."""

    async def scenario() -> None:
        limiter = SlidingWindowLimiter(
            capacity=100,
            max_requests=5,
            window_seconds=10,
            cooldown_seconds=15,
        )
        results = await asyncio.gather(*(limiter.admit(UserId(7)) for _ in range(40)))
        assert sum(allowed for allowed, _ in results) == 5
        assert all(retry is None for allowed, retry in results if allowed)
        assert all((retry or 0) > 0 for allowed, retry in results if not allowed)

    asyncio.run(scenario())


def test_bulkhead_fails_fast_when_queue_timeout_expires() -> None:
    """@brief 已占满 bulkhead 会有界拒绝等待者 / A saturated bulkhead rejects a waiter within its bound."""

    async def scenario() -> None:
        bulkhead = AsyncBulkhead(capacity=1, queue_timeout_seconds=0.01)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held() -> str:
            entered.set()
            await release.wait()
            return "done"

        first = asyncio.create_task(bulkhead.run(held))
        await entered.wait()
        with pytest.raises(BulkheadFull):
            await bulkhead.run(held)
        release.set()
        assert await first == "done"

    asyncio.run(scenario())
