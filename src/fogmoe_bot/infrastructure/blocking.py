"""@brief 同步 SDK 的显式异步隔舱 / Explicit async bulkhead for synchronous SDKs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import cast


class BlockingCallQueueFull(RuntimeError):
    """@brief 同步调用的隔舱排队预算耗尽 / Blocking-call bulkhead queue budget exhausted."""


class BlockingCallTimedOut(RuntimeError):
    """@brief 同步调用超过响应预算 / Blocking call exceeded its response budget."""


class BlockingBulkheadClosed(RuntimeError):
    """The blocking-call bulkhead no longer accepts work."""


class AsyncBlockingBulkhead:
    """@brief 限制 ``to_thread`` 调用的并发与排队 / Bound concurrency and queuing for ``to_thread`` calls.

    @note Python 无法强制终止已开始的线程；超时或调用方取消后，slot 仍保留到线程真实结束。
    Python cannot forcibly terminate an in-flight thread; after timeout or caller cancellation,
    its slot remains held until the thread actually exits.
    """

    def __init__(
        self,
        *,
        capacity: int = 4,
        queue_timeout: float = 2.0,
        call_timeout: float = 15.0,
        task_name: str = "blocking-sdk-call",
    ) -> None:
        """@brief 创建阻塞调用隔舱 / Create a blocking-call bulkhead.

        @param capacity 同时运行的最大调用数 / Maximum concurrently running calls.
        @param queue_timeout 获取 slot 的最长秒数 / Maximum seconds to acquire a slot.
        @param call_timeout 单次调用的响应预算 / Response budget for one call.
        @param task_name 可观测任务名 / Observable task name.
        """

        if capacity < 1:
            raise ValueError("Blocking-call capacity must be positive")
        if queue_timeout <= 0 or call_timeout <= 0:
            raise ValueError("Blocking-call timeouts must be positive")
        if not task_name.strip():
            raise ValueError("Blocking-call task name must not be blank")
        self._semaphore = asyncio.BoundedSemaphore(capacity)
        self._queue_timeout = queue_timeout
        self._call_timeout = call_timeout
        self._task_name = task_name
        self._pending_tasks: set[asyncio.Task[object]] = set()
        self._closed = False

    async def call[T](self, operation: Callable[[], T]) -> T:
        """@brief 在有界线程 slot 中执行同步函数 / Execute a synchronous function in a bounded thread slot.

        @param operation 无参数同步调用 / Zero-argument synchronous call.
        @return 同步调用结果 / Synchronous call result.
        """

        self._require_open()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._queue_timeout,
            )
        except TimeoutError as error:
            raise BlockingCallQueueFull("Blocking-call bulkhead is full") from error

        try:
            self._require_open()
        except BlockingBulkheadClosed:
            self._semaphore.release()
            raise
        try:
            task = asyncio.create_task(
                asyncio.to_thread(operation),
                name=self._task_name,
            )
        except BaseException:
            self._semaphore.release()
            raise
        owned_task = cast(asyncio.Task[object], task)
        self._pending_tasks.add(owned_task)
        owned_task.add_done_callback(self._release_when_finished)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._call_timeout,
            )
        except TimeoutError as error:
            raise BlockingCallTimedOut("Blocking call timed out") from error

    async def close(self) -> None:
        """Stop admission and wait for every already-started thread call."""

        self._stop_admission()
        while self._pending_tasks:
            pending = asyncio.gather(
                *tuple(self._pending_tasks),
                return_exceptions=True,
            )
            await asyncio.shield(pending)

    def _release_when_finished(self, task: asyncio.Task[object]) -> None:
        """@brief 在线程真实结束后释放 slot / Release a slot after its thread actually finishes."""

        self._pending_tasks.discard(task)
        self._semaphore.release()
        if not task.cancelled():
            task.exception()

    def _require_open(self) -> None:
        if self._closed:
            raise BlockingBulkheadClosed("Blocking-call bulkhead is closed")

    def _stop_admission(self) -> None:
        self._closed = True


class BlockingBulkheadLifecycle:
    """Drain a fixed set of blocking-call bulkheads during runtime shutdown."""

    def __init__(self, bulkheads: Sequence[AsyncBlockingBulkhead]) -> None:
        if not bulkheads:
            raise ValueError("At least one blocking-call bulkhead is required")
        if len({id(bulkhead) for bulkhead in bulkheads}) != len(bulkheads):
            raise ValueError("Blocking-call bulkheads must be unique")
        self._bulkheads = tuple(bulkheads)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Reject new calls and drain threads on phased stop or supervisor cancellation."""

        try:
            await stop_event.wait()
        finally:
            for bulkhead in self._bulkheads:
                bulkhead._stop_admission()
            await asyncio.gather(*(bulkhead.close() for bulkhead in self._bulkheads))


__all__ = [
    "AsyncBlockingBulkhead",
    "BlockingBulkheadClosed",
    "BlockingBulkheadLifecycle",
    "BlockingCallQueueFull",
    "BlockingCallTimedOut",
]
