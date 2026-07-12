"""@brief Durable inbox worker 测试 / Tests for the durable inbox worker."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging

import pytest
from observability_testkit import make_telemetry

from fogmoe_bot.application.conversation.inbox_worker import (
    FullJitterRetryPolicy,
    InboxWorker,
    PermanentIngressError,
)
from fogmoe_bot.application.conversation.router import (
    DispatchDeferred,
    Dispatched,
    Ignored,
)
from fogmoe_bot.application.runtime import (
    AggregateKey,
    RuntimeState,
    RuntimeUnavailable,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    LeaseToken,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import (
    InboundClaim,
    InboundStatus,
    InboundUpdate,
)


NOW = datetime(2026, 7, 11, 10, tzinfo=timezone.utc)
"""@brief 测试基准时间 / Test reference time."""


def _claim(*, attempt_count: int = 1, update_id: int = 1) -> InboundClaim:
    """@brief 构造 processing claim / Build a processing claim.

    @param attempt_count 已记录领取次数 / Recorded claim count.
    @param update_id Update ID / Update identifier.
    @return 测试 claim / Test claim.
    """

    pending = InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:7"),
        payload={"kind": "message"},
        received_at=NOW,
    )
    processing = replace(
        pending,
        status=InboundStatus.PROCESSING,
        version=1,
        attempt_count=attempt_count,
        next_attempt_at=None,
        updated_at=NOW + timedelta(seconds=1),
    )
    return InboundClaim(
        update=processing,
        token=LeaseToken.new(),
        lease_expires_at=NOW + timedelta(minutes=1),
    )


class _Clock:
    """@brief 固定时钟 / Fixed clock."""

    def now(self) -> datetime:
        """@brief 返回测试时间 / Return test time.

        @return 固定 UTC 时间 / Fixed UTC time.
        """

        return NOW + timedelta(seconds=2)


class _Repository:
    """@brief 记录 inbox 状态调用的 repository 替身 / Repository double recording inbox state calls."""

    def __init__(self, claims: tuple[InboundClaim, ...] = ()) -> None:
        """@brief 创建 repository 替身 / Create the repository double.

        @param claims 首轮可领取 claims / Claims available in the first poll.
        """

        self.claims = list(claims)
        self.claim_limits: list[int] = []
        self.processed: list[tuple[InboundClaim, datetime]] = []
        self.retried: list[tuple[InboundClaim, datetime, datetime, str]] = []
        self.failed: list[tuple[InboundClaim, datetime, str]] = []
        self.recover_calls = 0

    async def claim_inbound(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[InboundClaim, ...]:
        """@brief 按上限领取 claims / Claim values up to the limit.

        @param now 当前时间 / Current time.
        @param limit 领取上限 / Claim limit.
        @param lease_for 未使用租约 / Unused lease duration.
        @return claims / Claims.
        """

        del now, lease_for
        self.claim_limits.append(limit)
        claimed = tuple(self.claims[:limit])
        del self.claims[:limit]
        return claimed

    async def mark_inbound_processed(
        self,
        claim: InboundClaim,
        *,
        processed_at: datetime,
    ) -> None:
        """@brief 记录完成 / Record completion.

        @param claim 当前 claim / Current claim.
        @param processed_at 完成时间 / Completion time.
        @return None / None.
        """

        self.processed.append((claim, processed_at))

    async def retry_inbound(
        self,
        claim: InboundClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 记录重试 / Record retry.

        @param claim 当前 claim / Current claim.
        @param failed_at 失败时间 / Failure time.
        @param retry_at 重试时间 / Retry time.
        @param error 错误文本 / Error text.
        @return None / None.
        """

        self.retried.append((claim, failed_at, retry_at, error))

    async def fail_inbound(
        self,
        claim: InboundClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 记录最终失败 / Record final failure.

        @param claim 当前 claim / Current claim.
        @param failed_at 失败时间 / Failure time.
        @param error 错误文本 / Error text.
        @return None / None.
        """

        self.failed.append((claim, failed_at, error))

    async def recover_expired_inbound_leases(self, *, now: datetime) -> int:
        """@brief 记录 lease recovery / Record lease recovery.

        @param now 当前时间 / Current time.
        @return 0 / Zero.
        """

        del now
        self.recover_calls += 1
        return 0


class _OverclaimingRepository(_Repository):
    """@brief 模拟违反 claim limit 的 persistence / Persistence double violating the claim limit."""

    def __init__(self) -> None:
        """@brief 初始化超额 claims 与观测事件 / Initialize excess claims and an observation event."""

        super().__init__((_claim(update_id=1), _claim(update_id=2)))
        self.overclaimed = asyncio.Event()

    async def claim_inbound(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[InboundClaim, ...]:
        """@brief 首次忽略 limit 返回全部 claims / Ignore the first limit and return every claim."""

        del now, lease_for
        self.claim_limits.append(limit)
        if not self.claims:
            return ()
        claims = tuple(self.claims)
        self.claims.clear()
        self.overclaimed.set()
        return claims


class _Router:
    """@brief 返回固定结果或异常的 router 替身 / Router double returning a fixed outcome or exception."""

    def __init__(self, result: object) -> None:
        """@brief 创建 router / Create the router.

        @param result RouteOutcome 或 Exception / RouteOutcome or Exception.
        """

        self.result = result
        self.started: list[int] = []
        self.release: asyncio.Event | None = None

    async def route(self, update: InboundUpdate):
        """@brief 返回或抛出固定结果 / Return or raise the fixed result.

        @param update 已领取 Update / Claimed Update.
        @return 固定 route 结果 / Fixed route outcome.
        """

        self.started.append(update.update_id.value)
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _worker(
    repository: _Repository,
    router: _Router,
    *,
    max_attempts: int = 3,
    worker_count: int = 1,
) -> InboxWorker:
    """@brief 构造确定性测试 worker / Build a deterministic test worker.

    @param repository repository 替身 / Repository double.
    @param router router 替身 / Router double.
    @param max_attempts 最大尝试数 / Maximum attempts.
    @param worker_count worker 数 / Worker count.
    @return inbox worker / Inbox worker.
    """

    return InboxWorker(
        repository=repository,
        router=router,
        worker_count=worker_count,
        poll_interval=0.01,
        lease_for=timedelta(minutes=1),
        retry_policy=FullJitterRetryPolicy(
            max_attempts=max_attempts,
            initial_delay=timedelta(seconds=4),
            max_delay=timedelta(seconds=10),
            jitter=lambda lower, upper: upper,
        ),
        clock=_Clock(),
        telemetry=make_telemetry(),
    )


def test_successful_route_marks_claim_processed() -> None:
    """@brief 成功 route 以 claim token 完成 Update / Successful routing completes the claimed Update."""

    async def scenario() -> None:
        """@brief 运行成功场景 / Run success scenario.

        @return None / None.
        """

        repository = _Repository()
        worker = _worker(repository, _Router(Dispatched("assistant", ())))
        claim = _claim()

        await worker.process_claim(claim)

        assert repository.processed == [(claim, NOW + timedelta(seconds=2))]
        assert repository.retried == []
        assert repository.failed == []

    asyncio.run(scenario())


def test_runtime_deferred_is_retried_with_full_jitter_backoff() -> None:
    """@brief runtime deferred 保留 Update 并安排退避 / Runtime deferral retains the Update with backoff."""

    async def scenario() -> None:
        """@brief 运行 deferred 场景 / Run deferred scenario.

        @return None / None.
        """

        repository = _Repository()
        cause = RuntimeUnavailable(
            key=AggregateKey.of("conversation", 7),
            state=RuntimeState.DRAINING,
        )
        worker = _worker(
            repository,
            _Router(DispatchDeferred(operation_name="assistant", cause=cause)),
        )

        await worker.process_claim(_claim(attempt_count=2))

        _, failed_at, retry_at, error = repository.retried[0]
        assert failed_at == NOW + timedelta(seconds=2)
        assert retry_at == failed_at + timedelta(seconds=8)
        assert error.startswith("RuntimeAdmissionError:")

    asyncio.run(scenario())


def test_retryable_failure_is_logged_after_retry_is_persisted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """@brief 可重试失败在持久化重试后记录完整异常 / Retryable failures log the exception after retry persistence."""

    repository = _Repository()
    worker = _worker(repository, _Router(OSError("network")))

    with caplog.at_level(
        logging.WARNING,
        logger="fogmoe_bot.application.conversation.inbox_worker",
    ):
        asyncio.run(worker.process_claim(_claim(update_id=17)))

    assert len(repository.retried) == 1
    record = next(
        record
        for record in caplog.records
        if record.message.startswith("Inbox update retry scheduled:")
    )
    assert record.exc_info is not None
    assert "update_id=17" in record.message


def test_permanent_failure_is_quarantined_without_retry() -> None:
    """@brief 永久入口错误直接隔离 / Permanent ingress errors are quarantined directly."""

    async def scenario() -> None:
        """@brief 运行永久错误场景 / Run permanent-error scenario.

        @return None / None.
        """

        repository = _Repository()
        claim = _claim()
        worker = _worker(repository, _Router(PermanentIngressError("invalid command")))

        await worker.process_claim(claim)

        assert repository.retried == []
        assert repository.failed[0][:2] == (claim, NOW + timedelta(seconds=2))
        assert "invalid command" in repository.failed[0][2]

    asyncio.run(scenario())


def test_final_failure_is_logged_after_quarantine_is_persisted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """@brief 最终隔离成功后记录错误异常 / Final quarantine logs the error after persistence succeeds."""

    repository = _Repository()
    worker = _worker(repository, _Router(PermanentIngressError("invalid command")))

    with caplog.at_level(
        logging.ERROR,
        logger="fogmoe_bot.application.conversation.inbox_worker",
    ):
        asyncio.run(worker.process_claim(_claim(update_id=18)))

    assert len(repository.failed) == 1
    record = next(
        record
        for record in caplog.records
        if record.message.startswith("Inbox update moved to final-failure quarantine:")
    )
    assert record.exc_info is not None
    assert "update_id=18" in record.message


def test_transient_failure_stops_after_attempt_budget() -> None:
    """@brief 瞬态错误耗尽尝试预算后隔离 / Transient errors quarantine after exhausting the attempt budget."""

    async def scenario() -> None:
        """@brief 运行预算耗尽场景 / Run exhausted-budget scenario.

        @return None / None.
        """

        repository = _Repository()
        worker = _worker(repository, _Router(OSError("network")), max_attempts=3)

        await worker.process_claim(_claim(attempt_count=3))

        assert repository.retried == []
        assert "OSError: network" in repository.failed[0][2]

    asyncio.run(scenario())


def test_run_never_claims_more_than_worker_capacity() -> None:
    """@brief 已领取未终结总量受 worker_count 约束 / Claimed-but-unfinalized work is capped by worker_count."""

    async def scenario() -> None:
        """@brief 运行容量场景 / Run capacity scenario.

        @return None / None.
        """

        repository = _Repository(
            (_claim(update_id=1), _claim(update_id=2), _claim(update_id=3))
        )
        router = _Router(Ignored())
        router.release = asyncio.Event()
        worker = _worker(repository, router, worker_count=2)
        stop_event = asyncio.Event()
        task = asyncio.create_task(worker.run(stop_event))

        while len(router.started) < 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0.03)
        assert repository.claim_limits == [2]
        assert sorted(router.started) == [1, 2]

        stop_event.set()
        router.release.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_run_rejects_a_repository_batch_larger_than_available_capacity() -> None:
    """@brief persistence 超额返回不得突破 worker 容量 / An oversized persistence batch cannot exceed worker capacity."""

    async def scenario() -> None:
        """@brief 运行一次超额 claim / Run one oversized claim poll."""

        repository = _OverclaimingRepository()
        router = _Router(Ignored())
        worker = _worker(repository, router, worker_count=1)
        stop_event = asyncio.Event()
        task = asyncio.create_task(worker.run(stop_event))

        await asyncio.wait_for(repository.overclaimed.wait(), timeout=1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

        assert repository.claim_limits == [1]
        assert router.started == []

    asyncio.run(scenario())


def test_external_cancellation_cancels_blocked_consumers_without_draining() -> None:
    """@brief 强制取消不等待阻塞中的 handler drain / Forced cancellation does not wait for a blocked handler to drain."""

    async def scenario() -> None:
        """@brief 取消正在处理 claim 的 worker / Cancel a worker processing a claim.

        @return None / None.
        """

        repository = _Repository((_claim(),))
        router = _Router(Ignored())
        router.release = asyncio.Event()
        task = asyncio.create_task(_worker(repository, router).run(asyncio.Event()))
        while not router.started:
            await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.1)
        assert not any(
            pending.get_name().startswith("inbox-consumer-")
            for pending in asyncio.all_tasks()
            if pending is not asyncio.current_task()
        )

    asyncio.run(scenario())
