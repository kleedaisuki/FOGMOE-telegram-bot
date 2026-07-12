"""@brief 通用同步调用隔舱测试 / Tests for the generic blocking-call bulkhead."""

from __future__ import annotations

import asyncio
import threading

import pytest

from fogmoe_bot.infrastructure.blocking import (
    AsyncBlockingBulkhead,
    BlockingBulkheadClosed,
    BlockingBulkheadLifecycle,
    BlockingCallQueueFull,
    BlockingCallTimedOut,
)


def test_timed_out_thread_keeps_slot_until_real_completion() -> None:
    """@brief 超时线程退出前仍占用 slot / A timed-out thread keeps its slot until it exits."""

    async def scenario() -> None:
        """@brief 驱动超时场景 / Exercise the timeout scenario."""

        started = threading.Event()
        release = threading.Event()
        bulkhead = AsyncBlockingBulkhead(
            capacity=1,
            queue_timeout=0.02,
            call_timeout=0.02,
        )

        def blocked() -> int:
            """@brief 模拟不可取消线程 / Simulate a non-cancellable thread."""

            started.set()
            release.wait(timeout=1)
            return 1

        with pytest.raises(BlockingCallTimedOut):
            await bulkhead.call(blocked)
        assert started.is_set()
        assert len(bulkhead._pending_tasks) == 1
        with pytest.raises(BlockingCallQueueFull):
            await bulkhead.call(lambda: 2)
        release.set()
        await asyncio.sleep(0.05)
        assert len(bulkhead._pending_tasks) == 0
        assert await bulkhead.call(lambda: 3) == 3

    asyncio.run(scenario())


def test_cancelled_caller_keeps_slot_until_thread_completion() -> None:
    """@brief 调用方取消不提前释放运行中线程 / Cancellation does not release an in-flight thread early."""

    async def scenario() -> None:
        """@brief 驱动取消场景 / Exercise the cancellation scenario."""

        started = threading.Event()
        release = threading.Event()
        bulkhead = AsyncBlockingBulkhead(
            capacity=1,
            queue_timeout=0.02,
            call_timeout=1,
        )

        def blocked() -> int:
            """@brief 模拟不可取消线程 / Simulate a non-cancellable thread."""

            started.set()
            release.wait(timeout=1)
            return 1

        call = asyncio.create_task(bulkhead.call(blocked))
        while not started.is_set():
            await asyncio.sleep(0)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert len(bulkhead._pending_tasks) == 1
        with pytest.raises(BlockingCallQueueFull):
            await bulkhead.call(lambda: 2)
        release.set()
        await asyncio.sleep(0.05)
        assert len(bulkhead._pending_tasks) == 0

    asyncio.run(scenario())


def test_close_rejects_admission_and_waits_for_timed_out_thread() -> None:
    """Close owns timed-out threads until their synchronous call really exits."""

    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        bulkhead = AsyncBlockingBulkhead(
            capacity=1,
            queue_timeout=0.02,
            call_timeout=0.02,
        )

        def blocked() -> int:
            started.set()
            release.wait(timeout=1)
            return 1

        with pytest.raises(BlockingCallTimedOut):
            await bulkhead.call(blocked)
        assert started.is_set()

        closing = asyncio.create_task(bulkhead.close())
        await asyncio.sleep(0)
        assert not closing.done()
        with pytest.raises(BlockingBulkheadClosed):
            await bulkhead.call(lambda: 2)

        release.set()
        await asyncio.wait_for(closing, timeout=1)
        assert not bulkhead._pending_tasks

    asyncio.run(scenario())


def test_lifecycle_closes_all_bulkheads_after_stop() -> None:
    """The structured runtime service closes every owned bulkhead."""

    async def scenario() -> None:
        first = AsyncBlockingBulkhead()
        second = AsyncBlockingBulkhead()
        lifecycle = BlockingBulkheadLifecycle((first, second))
        stop = asyncio.Event()
        running = asyncio.create_task(lifecycle.run(stop))
        await asyncio.sleep(0)
        assert not running.done()

        stop.set()
        await running
        with pytest.raises(BlockingBulkheadClosed):
            await first.call(lambda: 1)
        with pytest.raises(BlockingBulkheadClosed):
            await second.call(lambda: 2)

    asyncio.run(scenario())


def test_cancelled_lifecycle_still_drains_started_threads() -> None:
    """Supervisor failure cannot orphan a thread that already crossed admission."""

    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        bulkhead = AsyncBlockingBulkhead(
            capacity=1,
            queue_timeout=0.02,
            call_timeout=0.02,
        )

        def blocked() -> int:
            started.set()
            release.wait(timeout=1)
            return 1

        with pytest.raises(BlockingCallTimedOut):
            await bulkhead.call(blocked)
        assert started.is_set()

        lifecycle = BlockingBulkheadLifecycle((bulkhead,))
        running = asyncio.create_task(lifecycle.run(asyncio.Event()))
        await asyncio.sleep(0)
        running.cancel()
        await asyncio.sleep(0)
        assert not running.done()
        with pytest.raises(BlockingBulkheadClosed):
            await bulkhead.call(lambda: 2)

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=1)
        assert not bulkhead._pending_tasks

    asyncio.run(scenario())
