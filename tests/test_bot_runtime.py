"""@brief 顶层 BotRuntime 结构化生命周期测试 / Top-level BotRuntime structured-lifecycle tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from fogmoe_bot.application.runtime import (
    BotRuntime,
    BotRuntimeState,
    KeyedMailboxRuntime,
    RuntimeState,
    ServiceBinding,
    ShutdownMode,
)


class _DrainableService:
    """@brief 可观察 drain 的后台服务替身 / Observable drainable background-service double."""

    def __init__(self) -> None:
        """@brief 初始化同步点 / Initialize synchronization points."""

        self.started = asyncio.Event()
        self.draining = asyncio.Event()
        self.release = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 等待停止后阻塞 drain / Block during drain after shutdown is requested.

        @param stop_event 共享停止信号 / Shared stop signal.
        @return None / None.
        """

        self.loop = asyncio.get_running_loop()
        self.started.set()
        await stop_event.wait()
        self.draining.set()
        await self.release.wait()


class _NeverStopsService:
    """@brief 只能由 CANCEL 终止的服务替身 / Service double terminable only by cancellation."""

    def __init__(self) -> None:
        """@brief 初始化状态 / Initialize state."""

        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 忽略 drain 信号直至被取消 / Ignore drain until cancelled.

        @param stop_event 未使用的共享信号 / Unused shared signal.
        @return None / None.
        """

        del stop_event
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _EarlyReturnService:
    """@brief 错误地提前返回的服务替身 / Service double that incorrectly returns early."""

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 在停止前直接返回 / Return before shutdown.

        @param stop_event 未使用的停止信号 / Unused stop signal.
        @return None / None.
        """

        del stop_event


class _CancelDuringStartupService:
    """@brief 在服务启动时取消 start 调用方 / Cancel the ``start`` caller as the service begins."""

    def __init__(self, cancel_owner: Callable[[], bool]) -> None:
        """@brief 注入 owner task 的取消函数 / Inject the owner-task cancellation function.

        @param cancel_owner 取消 ``BotRuntime.start`` 调用方 / Cancel the caller of ``BotRuntime.start``.
        """

        self._cancel_owner = cancel_owner
        self.cancelled = False

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 触发启动取消并等待自身被收回 / Trigger startup cancellation and await reclamation.

        @param stop_event 运行时停止信号 / Runtime stop signal.
        @return None / None.
        """

        del stop_event
        self._cancel_owner()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _execution_runtime() -> KeyedMailboxRuntime:
    """@brief 创建最小执行器 / Create a minimal execution runtime.

    @return 测试执行器 / Test execution runtime.
    """

    return KeyedMailboxRuntime(
        max_concurrency=2,
        global_capacity=4,
        per_key_capacity=2,
    )


def _runtime_state(runtime: BotRuntime) -> BotRuntimeState:
    """@brief 重新读取异步可变生命周期状态 / Re-read asynchronously mutable lifecycle state.

    @param runtime 顶层运行时 / Top-level runtime.
    @return 调用时状态 / State at call time.
    """

    return runtime.state


def _executor_state(runtime: KeyedMailboxRuntime) -> RuntimeState:
    """@brief 重新读取异步可变执行器状态 / Re-read asynchronously mutable executor state.

    @param runtime keyed mailbox runtime / Keyed mailbox runtime.
    @return 调用时状态 / State at call time.
    """

    return runtime.state


def test_runtime_shares_one_loop_and_drains_all_services_before_executor() -> None:
    """@brief 所有服务共用主 loop 且先于 executor 排空 / Services share one loop and drain before the executor."""

    async def scenario() -> None:
        """@brief 驱动统一 drain 生命周期 / Drive the unified drain lifecycle.

        @return None / None.
        """

        first = _DrainableService()
        second = _DrainableService()
        executor = _execution_runtime()
        runtime = BotRuntime(
            execution_runtime=executor,
            services=(
                ServiceBinding("first", first, shutdown_phase=0),
                ServiceBinding("second", second, shutdown_phase=10),
            ),
        )

        await runtime.start()
        await asyncio.gather(first.started.wait(), second.started.wait())
        shutdown = asyncio.create_task(runtime.shutdown(ShutdownMode.DRAIN))
        await first.draining.wait()

        assert _runtime_state(runtime) is BotRuntimeState.STOPPING
        assert _executor_state(executor) is RuntimeState.RUNNING
        assert first.loop is asyncio.get_running_loop()
        assert second.loop is asyncio.get_running_loop()
        assert not second.draining.is_set()
        assert not shutdown.done()

        first.release.set()
        await second.draining.wait()
        assert not shutdown.done()
        second.release.set()
        await asyncio.wait_for(shutdown, timeout=1)
        assert _runtime_state(runtime) is BotRuntimeState.STOPPED
        assert _executor_state(executor) is RuntimeState.CLOSED

    asyncio.run(scenario())


def test_cancel_terminates_non_cooperative_service_and_executor() -> None:
    """@brief CANCEL 终止不配合 drain 的服务 / CANCEL terminates a non-cooperative drain service."""

    async def scenario() -> None:
        """@brief 驱动强制取消 / Drive forced cancellation.

        @return None / None.
        """

        service = _NeverStopsService()
        executor = _execution_runtime()
        runtime = BotRuntime(
            execution_runtime=executor,
            services=(ServiceBinding("stuck", service),),
        )
        await runtime.start()
        await service.started.wait()

        await runtime.shutdown(ShutdownMode.CANCEL)

        assert service.cancelled
        assert runtime.state is BotRuntimeState.STOPPED
        assert executor.state is RuntimeState.CLOSED
        await runtime.shutdown(ShutdownMode.CANCEL)

    asyncio.run(scenario())


def test_early_service_return_fails_fast_and_cancels_executor() -> None:
    """@brief 服务无信号返回会使运行时 fail-fast / Unsolicited service return fails the runtime fast."""

    async def scenario() -> None:
        """@brief 观察失败传播 / Observe failure propagation.

        @return None / None.
        """

        executor = _execution_runtime()
        runtime = BotRuntime(
            execution_runtime=executor,
            services=(ServiceBinding("early", _EarlyReturnService()),),
        )
        try:
            await runtime.start()
        except RuntimeError:
            pass
        await runtime.wait_terminated()

        assert runtime.state is BotRuntimeState.FAILED
        assert runtime.failure is not None
        assert executor.state is RuntimeState.CLOSED
        with pytest.raises(RuntimeError, match="Bot runtime failed"):
            await runtime.shutdown()

    asyncio.run(scenario())


def test_start_cancellation_reclaims_supervisor_services_and_executor() -> None:
    """@brief 外部取消部分启动不会泄漏后台任务 / Cancelling partial startup leaks no background tasks."""

    async def scenario() -> None:
        """@brief 在 supervisor 创建服务后取消启动 / Cancel startup after the supervisor creates its service."""

        owner = asyncio.current_task()
        assert owner is not None
        service = _CancelDuringStartupService(owner.cancel)
        executor = _execution_runtime()
        runtime = BotRuntime(
            execution_runtime=executor,
            services=(ServiceBinding("cancel-start", service),),
        )

        with pytest.raises(asyncio.CancelledError):
            await runtime.start()

        assert service.cancelled
        assert runtime.state is BotRuntimeState.STOPPED
        assert executor.state is RuntimeState.CLOSED

    asyncio.run(scenario())


def test_runtime_rejects_duplicate_service_names_and_restart() -> None:
    """@brief 名称唯一且运行时不可重启 / Service names are unique and the runtime is single-use."""

    service = _DrainableService()
    with pytest.raises(ValueError, match="names must be unique"):
        BotRuntime(
            execution_runtime=_execution_runtime(),
            services=(
                ServiceBinding("same", service),
                ServiceBinding("same", service),
            ),
        )
    with pytest.raises(ValueError, match="phase cannot be negative"):
        ServiceBinding("bad-phase", service, shutdown_phase=-1)

    async def scenario() -> None:
        """@brief 终结未启动实例后拒绝启动 / Finalize an unstarted instance and reject startup.

        @return None / None.
        """

        runtime = BotRuntime(execution_runtime=_execution_runtime(), services=())
        await runtime.shutdown()
        with pytest.raises(RuntimeError, match="cannot start from stopped"):
            await runtime.start()

    asyncio.run(scenario())
