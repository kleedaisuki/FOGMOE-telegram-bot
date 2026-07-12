"""@brief 有界键控邮箱运行时测试 / Tests for the bounded keyed-mailbox runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from fogmoe_bot.application.runtime import (
    Accepted,
    AggregateKey,
    KeyedMailboxRuntime,
    Overloaded,
    OverloadScope,
    RuntimeState,
    RuntimeUnavailable,
    ShutdownMode,
    Submission,
    WorkPriority,
    WorkTicket,
)


def _ticket[T](submission: Submission[T]) -> WorkTicket[T]:
    """@brief 从成功准入结果提取 ticket / Extract a ticket from successful admission.

    @param submission 待断言的准入结果 / Admission result to assert.
    @return 类型化工作 ticket / Typed work ticket.
    """

    assert isinstance(submission, Accepted)
    return submission.ticket


def test_aggregate_key_is_explicit_immutable_and_validated() -> None:
    """@brief 聚合键显式表达种类和复合身份 / Aggregate keys express kind and composite identity."""

    key = AggregateKey.of("conversation", -100, 7, "thread-3")

    assert key.aggregate_type == "conversation"
    assert key.identity == (-100, 7, "thread-3")
    assert hash(key) == hash(AggregateKey("conversation", (-100, 7, "thread-3")))
    with pytest.raises(ValueError):
        AggregateKey.of(" ", 1)
    with pytest.raises(ValueError):
        AggregateKey.of("conversation")
    with pytest.raises(TypeError):
        AggregateKey.of("conversation", True)


def test_same_key_runs_strict_fifo_and_never_overlaps() -> None:
    """@brief 同一 key 即使有多个 worker 也严格 FIFO 串行 / One key stays FIFO-serial with many workers."""

    async def scenario() -> None:
        """@brief 驱动同 key 串行场景 / Drive the same-key serialization scenario.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=4,
            global_capacity=8,
            per_key_capacity=8,
        )
        key = AggregateKey.of("conversation", 7)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        events: list[str] = []
        active = 0
        maximum_active = 0

        async def operation(index: int) -> int:
            """@brief 记录一个有序工作 / Record one ordered work item.

            @param index 准入顺序 / Admission order.
            @return 原样返回顺序 / Admission order unchanged.
            """

            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            events.append(f"start-{index}")
            if index == 0:
                first_started.set()
                await release_first.wait()
            await asyncio.sleep(0)
            events.append(f"end-{index}")
            active -= 1
            return index

        def operation_for(index: int) -> Callable[[], Awaitable[int]]:
            """@brief 将有参测试操作绑定成零参工作 / Bind a parameterized test operation into zero-argument work.

            @param index 待绑定准入顺序 / Admission order to bind.
            @return 可提交的零参异步 callable / Zero-argument async callable ready for submission.
            """

            return lambda: operation(index)

        async with runtime:
            tickets = [
                _ticket(runtime.submit(key, operation_for(index))) for index in range(3)
            ]
            await first_started.wait()
            await asyncio.sleep(0)
            assert events == ["start-0"]
            release_first.set()
            assert await asyncio.gather(*(ticket.wait() for ticket in tickets)) == [
                0,
                1,
                2,
            ]

        assert maximum_active == 1
        assert events == [
            "start-0",
            "end-0",
            "start-1",
            "end-1",
            "start-2",
            "end-2",
        ]

    asyncio.run(scenario())


def test_distinct_keys_run_concurrently() -> None:
    """@brief 不同 key 能占用不同固定 worker 并发执行 / Distinct keys use separate fixed workers concurrently."""

    async def scenario() -> None:
        """@brief 驱动跨 key 并发场景 / Drive the cross-key concurrency scenario.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=2,
            global_capacity=4,
            per_key_capacity=2,
        )
        release = asyncio.Event()
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def operation(started: asyncio.Event, result: str) -> str:
            """@brief 标记已并发进入并等待统一释放 / Mark concurrent entry and await shared release.

            @param started 对应工作的进入事件 / Entry event for this work.
            @param result 工作返回值 / Work result.
            @return 输入返回值 / Input result.
            """

            started.set()
            await release.wait()
            return result

        async with runtime:
            first = _ticket(
                runtime.submit(
                    AggregateKey.of("conversation", 1),
                    lambda: operation(first_started, "first"),
                )
            )
            second = _ticket(
                runtime.submit(
                    AggregateKey.of("conversation", 2),
                    lambda: operation(second_started, "second"),
                )
            )
            async with asyncio.timeout(1):
                await first_started.wait()
                await second_started.wait()
            assert runtime.snapshot().active_count == 2
            release.set()
            results = await asyncio.gather(first.wait(), second.wait())
            assert tuple(results) == ("first", "second")

    asyncio.run(scenario())


def test_admission_reports_per_key_and_global_overload_without_invoking_rejected_work() -> (
    None
):
    """@brief 两级容量过载以值返回且不创建被拒绝 coroutine / Two-level overload is data and invokes no rejected work."""

    async def scenario() -> None:
        """@brief 驱动两级背压场景 / Drive the two-level backpressure scenario.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=1,
            global_capacity=2,
            per_key_capacity=1,
        )
        first_key = AggregateKey.of("account", 1)
        second_key = AggregateKey.of("account", 2)
        third_key = AggregateKey.of("account", 3)
        started = asyncio.Event()
        release = asyncio.Event()
        rejected_invocations = 0

        async def blocking() -> str:
            """@brief 占用唯一 worker / Occupy the sole worker.

            @return 固定结果 / Fixed result.
            """

            started.set()
            await release.wait()
            return "first"

        async def rejected() -> None:
            """@brief 若错误执行则记录 / Record erroneous invocation.

            @return None / None.
            """

            nonlocal rejected_invocations
            rejected_invocations += 1

        async with runtime:
            first = _ticket(runtime.submit(first_key, blocking))
            await started.wait()

            key_overload = runtime.submit(first_key, rejected)
            assert isinstance(key_overload, Overloaded)
            assert key_overload.scope is OverloadScope.AGGREGATE
            assert key_overload.capacity == 1
            assert key_overload.pending == 1

            second = _ticket(
                runtime.submit(second_key, lambda: asyncio.sleep(0, result="second"))
            )
            global_overload = runtime.submit(third_key, rejected)
            assert isinstance(global_overload, Overloaded)
            assert global_overload.scope is OverloadScope.GLOBAL
            assert global_overload.capacity == 2
            assert global_overload.pending == 2

            release.set()
            assert await first.wait() == "first"
            assert await second.wait() == "second"

        assert rejected_invocations == 0

    asyncio.run(scenario())


def test_priority_selects_between_keys_but_never_overtakes_one_key_head() -> None:
    """@brief 优先级仅跨 key 生效而同 key 不插队 / Priority acts across keys without overtaking a key head."""

    async def scenario() -> None:
        """@brief 驱动优先级与 FIFO 组合场景 / Drive combined priority and FIFO semantics.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=1,
            global_capacity=5,
            per_key_capacity=3,
        )
        gate_key = AggregateKey.of("control", "gate")
        fifo_key = AggregateKey.of("conversation", 1)
        other_key = AggregateKey.of("conversation", 2)
        gate_started = asyncio.Event()
        release_gate = asyncio.Event()
        order: list[str] = []

        async def gate() -> None:
            """@brief 暂停 worker 以建立确定队列 / Pause the worker to build a deterministic queue.

            @return None / None.
            """

            gate_started.set()
            await release_gate.wait()

        async def record(label: str) -> str:
            """@brief 记录执行标签 / Record an execution label.

            @param label 待记录标签 / Label to record.
            @return 标签 / Label.
            """

            order.append(label)
            return label

        async with runtime:
            gate_ticket = _ticket(runtime.submit(gate_key, gate))
            await gate_started.wait()
            low_head = _ticket(
                runtime.submit(
                    fifo_key,
                    lambda: record("low-head"),
                    priority=WorkPriority.LOW,
                )
            )
            critical_tail = _ticket(
                runtime.submit(
                    fifo_key,
                    lambda: record("critical-tail"),
                    priority=WorkPriority.CRITICAL,
                )
            )
            critical_other = _ticket(
                runtime.submit(
                    other_key,
                    lambda: record("critical-other"),
                    priority=WorkPriority.CRITICAL,
                )
            )
            release_gate.set()
            await asyncio.gather(
                gate_ticket.wait(),
                low_head.wait(),
                critical_tail.wait(),
                critical_other.wait(),
            )

        assert order == ["critical-other", "low-head", "critical-tail"]

    asyncio.run(scenario())


def test_operation_failure_only_fails_ticket_and_worker_continues() -> None:
    """@brief operation 异常不破坏 worker 或同 key 后续工作 / Operation failure spares worker and later key work."""

    async def scenario() -> None:
        """@brief 驱动异常隔离场景 / Drive the failure-isolation scenario.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=1,
            global_capacity=2,
            per_key_capacity=2,
        )
        key = AggregateKey.of("conversation", 1)

        async def fail() -> None:
            """@brief 抛出预期异常 / Raise the expected error.

            @return None / None.
            @raises ValueError 始终抛出 / Always.
            """

            raise ValueError("boom")

        async with runtime:
            failed = _ticket(runtime.submit(key, fail))
            succeeded = _ticket(
                runtime.submit(key, lambda: asyncio.sleep(0, result=42))
            )
            with pytest.raises(ValueError, match="boom"):
                await failed.wait()
            assert await succeeded.wait() == 42
            assert runtime.state is RuntimeState.RUNNING

    asyncio.run(scenario())


def test_idle_mailboxes_are_reclaimed_without_polling_workers() -> None:
    """@brief 固定回收 task 按 TTL 删除空邮箱 / A fixed reaper task removes empty mailboxes by TTL."""

    async def scenario() -> None:
        """@brief 驱动空闲回收场景 / Drive the idle-reclamation scenario.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=1,
            global_capacity=1,
            per_key_capacity=1,
            idle_ttl=0.02,
            reap_interval=0.005,
        )
        async with runtime:
            ticket = _ticket(
                runtime.submit(
                    AggregateKey.of("conversation", 1),
                    lambda: asyncio.sleep(0, result="done"),
                )
            )
            assert await ticket.wait() == "done"
            assert runtime.mailbox_count == 1
            async with asyncio.timeout(1):
                while runtime.mailbox_count:
                    await asyncio.sleep(0.005)
            assert runtime.snapshot().mailbox_count == 0

    asyncio.run(scenario())


def test_drain_rejects_new_work_and_completes_every_accepted_ticket() -> None:
    """@brief DRAIN 关闭准入并完成既有 ticket / DRAIN closes admission and completes accepted tickets."""

    async def scenario() -> None:
        """@brief 驱动优雅排空场景 / Drive the graceful-drain scenario.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=1,
            global_capacity=2,
            per_key_capacity=2,
        )
        await runtime.start()
        key = AggregateKey.of("conversation", 1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking() -> str:
            """@brief 保持一个工作执行中 / Keep one work running.

            @return 固定结果 / Fixed result.
            """

            started.set()
            await release.wait()
            return "finished"

        ticket = _ticket(runtime.submit(key, blocking))
        await started.wait()
        shutdown_task = asyncio.create_task(runtime.shutdown(ShutdownMode.DRAIN))
        while runtime.state is RuntimeState.RUNNING:
            await asyncio.sleep(0)

        rejected = runtime.submit(key, lambda: asyncio.sleep(0))
        assert isinstance(rejected, RuntimeUnavailable)
        assert rejected.state is RuntimeState.DRAINING
        release.set()
        assert await ticket.wait() == "finished"
        await shutdown_task
        assert runtime.state is RuntimeState.CLOSED
        assert runtime.pending_count == 0
        assert runtime.mailbox_count == 0

    asyncio.run(scenario())


def test_submit_before_start_returns_typed_unavailable_without_calling_operation() -> (
    None
):
    """@brief 启动前准入返回 NEW 而非抛异常 / Admission before start returns NEW instead of raising.

    @note 被拒绝操作不得创建 coroutine 或产生副作用 / The rejected operation must not create a coroutine or side effect.
    """

    async def scenario() -> None:
        """@brief 验证 NEW 生命周期结果 / Verify the NEW lifecycle result.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=1,
            global_capacity=1,
            per_key_capacity=1,
        )
        key = AggregateKey.of("conversation", 1)
        called = False

        async def operation() -> None:
            """@brief 记录意外执行 / Record unexpected execution.

            @return None / None.
            """

            nonlocal called
            called = True

        rejected = runtime.submit(key, operation)

        assert isinstance(rejected, RuntimeUnavailable)
        assert rejected.state is RuntimeState.NEW
        assert called is False
        await runtime.shutdown(ShutdownMode.CANCEL)

    asyncio.run(scenario())


def test_cancel_shutdown_cancels_running_and_queued_tickets_without_leaks() -> None:
    """@brief CANCEL 终结执行中与排队 ticket 且清空索引 / CANCEL settles running and queued tickets and clears indexes."""

    async def scenario() -> None:
        """@brief 驱动强制取消场景 / Drive the forced-cancellation scenario.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=1,
            global_capacity=3,
            per_key_capacity=3,
        )
        await runtime.start()
        key = AggregateKey.of("conversation", 1)
        started = asyncio.Event()
        operation_stopped = asyncio.Event()

        async def blocking() -> None:
            """@brief 等待运行时取消并记录 finally / Await runtime cancellation and record finally.

            @return None / None.
            """

            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                operation_stopped.set()

        running = _ticket(runtime.submit(key, blocking))
        queued = _ticket(runtime.submit(key, lambda: asyncio.sleep(0, result="never")))
        await started.wait()
        await runtime.shutdown(ShutdownMode.CANCEL)

        assert operation_stopped.is_set()
        assert running.cancelled
        assert queued.cancelled
        with pytest.raises(asyncio.CancelledError):
            await running.wait()
        with pytest.raises(asyncio.CancelledError):
            await queued.wait()
        assert runtime.snapshot().state is RuntimeState.CLOSED
        assert runtime.pending_count == 0
        assert runtime.mailbox_count == 0

    asyncio.run(scenario())


def test_cancel_can_escalate_an_in_progress_drain() -> None:
    """@brief 并发 CANCEL 可升级尚在等待的 DRAIN / Concurrent CANCEL can escalate a waiting DRAIN."""

    async def scenario() -> None:
        """@brief 驱动关停升级场景 / Drive the shutdown-escalation scenario.

        @return None / None.
        """

        runtime = KeyedMailboxRuntime(
            max_concurrency=1,
            global_capacity=1,
            per_key_capacity=1,
        )
        await runtime.start()
        started = asyncio.Event()

        async def blocking() -> None:
            """@brief 永久等待直到取消 / Wait indefinitely until cancelled.

            @return None / None.
            """

            started.set()
            await asyncio.Event().wait()

        ticket = _ticket(runtime.submit(AggregateKey.of("conversation", 1), blocking))
        await started.wait()
        drain_task = asyncio.create_task(runtime.shutdown(ShutdownMode.DRAIN))
        while runtime.state is RuntimeState.RUNNING:
            await asyncio.sleep(0)

        await runtime.shutdown(ShutdownMode.CANCEL)
        await drain_task
        assert runtime.state is RuntimeState.CLOSED
        assert ticket.cancelled

    asyncio.run(scenario())
