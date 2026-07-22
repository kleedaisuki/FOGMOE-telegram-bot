"""@brief Durable compaction worker tests / Durable compaction-worker tests."""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import pytest

from fogmoe_bot.application.context_window.worker import (
    CompactionSourceError,
    CompactionWorker,
    FullJitterCompactionRetryPolicy,
    RetryableCompactionError,
)
from fogmoe_bot.application.runtime import AdaptivePollingPolicy
from fogmoe_bot.domain.context_window.budget import TokenCount
from fogmoe_bot.domain.context_window.compaction import (
    Compaction,
    CompactionPlan,
    CompactionStatus,
    CompactionSummary,
    StaleCompactionClaimError,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    LeaseToken,
    TurnId,
)

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
"""@brief 确定性测试时钟 / Deterministic test clock."""


class _Clock:
    """@brief 可推进 UTC clock / Advanceable UTC clock."""

    def __init__(self) -> None:
        """@brief 初始化时钟 / Initialize the clock."""

        self.value = NOW + timedelta(seconds=2)

    def now(self) -> datetime:
        """@brief 返回当前时间 / Return current time."""

        return self.value


class _Persistence:
    """@brief 记录 worker terminal calls 的内存 persistence / In-memory persistence recording worker terminal calls."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call records."""

        self.completed: list[tuple[Compaction, CompactionSummary, datetime]] = []
        self.retried: list[tuple[Compaction, datetime, datetime, str]] = []
        self.failed: list[tuple[Compaction, datetime, str]] = []
        self.recovered = 0

    async def claim_compactions(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[Compaction]:
        """@brief 测试不从 run loop 领取 / Tests do not claim from the run loop."""

        del now, limit, lease_for
        return ()

    async def complete_compaction(
        self,
        claim: Compaction,
        *,
        summary: CompactionSummary,
        completed_at: datetime,
    ) -> Compaction:
        """@brief 记录 completion / Record completion."""

        self.completed.append((claim, summary, completed_at))
        assert claim.claim_token is not None
        return claim.complete(
            token=claim.claim_token,
            summary=summary,
            completed_at=completed_at,
        )

    async def retry_compaction(
        self,
        claim: Compaction,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 记录 retry / Record retry."""

        self.retried.append((claim, failed_at, retry_at, error))

    async def fail_compaction(
        self,
        claim: Compaction,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 记录 final failure / Record final failure."""

        self.failed.append((claim, failed_at, error))

    async def recover_expired_compaction_leases(self, *, now: datetime) -> int:
        """@brief 记录 lease recovery / Record lease recovery."""

        del now
        self.recovered += 1
        return 0


class _Generator:
    """@brief 返回或抛出固定结果的 summary generator / Summary generator returning or raising a fixed result."""

    def __init__(self, result: CompactionSummary | Exception) -> None:
        """@brief 保存结果 / Store the result."""

        self.result = result

    async def summarize(self, segment: Compaction) -> CompactionSummary:
        """@brief 返回或抛出配置值 / Return or raise the configured value."""

        assert segment.status is CompactionStatus.PROCESSING
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _StalePersistence(_Persistence):
    """@brief 模拟 lease 已被另一 worker 回收 / Simulate a lease already recovered by another worker."""

    async def complete_compaction(
        self,
        claim: Compaction,
        *,
        summary: CompactionSummary,
        completed_at: datetime,
    ) -> Compaction:
        """@brief 拒绝 stale completion / Reject a stale completion."""

        del claim, summary, completed_at
        raise StaleCompactionClaimError("recovered")

    async def retry_compaction(
        self,
        claim: Compaction,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 拒绝 stale retry / Reject a stale retry."""

        del claim, failed_at, retry_at, error
        raise StaleCompactionClaimError("recovered")


class _OneShotClaimFailurePersistence(_Persistence):
    """@brief 首次 claim 失败后恢复的 persistence / Persistence recovering after one claim failure."""

    def __init__(self) -> None:
        """@brief 初始化故障计数与恢复通知 / Initialize failure count and recovery notification."""

        super().__init__()
        self.claim_calls = 0
        self.recovered_polling = asyncio.Event()

    async def claim_compactions(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[Compaction]:
        """@brief 首次抛出瞬态错误，随后恢复空轮询 / Fail once, then resume empty polling.

        @param now 当前时间 / Current time.
        @param limit claim 上限 / Claim limit.
        @param lease_for claim 租约 / Claim lease.
        @return 恢复后为空 / Empty after recovery.
        @raise RuntimeError 首次模拟数据库故障 / Simulated database failure on the first call.
        """

        del now, limit, lease_for
        self.claim_calls += 1
        if self.claim_calls == 1:
            raise RuntimeError("temporary compaction claim failure")
        self.recovered_polling.set()
        return ()


class _OneShotFinalizeFailurePersistence(_Persistence):
    """@brief 模拟一次完成确认故障 / Simulate one completion-acknowledgement failure."""

    def __init__(self, claim: Compaction) -> None:
        """@brief 保存唯一 claim 与恢复通知 / Store the sole claim and recovery notification."""

        super().__init__()
        self._claim = claim
        self.claim_calls = 0
        self.recovered_polling = asyncio.Event()

    async def claim_compactions(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[Compaction]:
        """@brief 首轮返回 claim，随后确认 consumer 仍存活 / Return a claim once, then prove the consumer survived."""

        del now, limit, lease_for
        self.claim_calls += 1
        if self.claim_calls == 1:
            return (self._claim,)
        self.recovered_polling.set()
        return ()

    async def complete_compaction(
        self,
        claim: Compaction,
        *,
        summary: CompactionSummary,
        completed_at: datetime,
    ) -> Compaction:
        """@brief 模拟数据库确认的瞬态故障 / Simulate a transient database acknowledgement failure."""

        del claim, summary, completed_at
        raise RuntimeError("temporary compaction completion failure")


class _OverclaimingPersistence(_Persistence):
    """@brief 模拟违反单 claim 上限的 persistence / Persistence double violating the single-claim limit."""

    def __init__(self, claim: Compaction) -> None:
        """@brief 保存超额 claim 与观测事件 / Store an excess claim and an observation event."""

        super().__init__()
        self._claim = claim
        self.overclaimed = asyncio.Event()

    async def claim_compactions(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[Compaction]:
        """@brief 首次返回两个 claims / Return two claims on the first poll."""

        del now, lease_for
        if self.overclaimed.is_set():
            return ()
        assert limit == 1
        self.overclaimed.set()
        return (self._claim, self._claim)


class _PeriodicRecoveryPersistence(_Persistence):
    """@brief 记录独立 recovery owner 与并行 claims / Record the independent recovery owner and concurrent claims."""

    def __init__(self, *, fail_first: bool) -> None:
        """@brief 初始化调用记录与可选首轮故障 / Initialize call records and an optional first-pass failure.

        @param fail_first 是否让首次回收失败 / Whether the first recovery call fails.
        """

        super().__init__()
        self.fail_first = fail_first
        self.claim_calls = 0
        self.periodic_recovery = asyncio.Event()

    async def claim_compactions(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[Compaction]:
        """@brief 记录 recovery 任务运行时 claim 仍继续 / Record claims continuing while the recovery task runs.

        @param now 当前时间 / Current instant.
        @param limit claim 上限 / Claim limit.
        @param lease_for claim 租约 / Claim lease.
        @return 始终为空 / Always empty.
        """

        del now, lease_for
        assert limit == 1
        self.claim_calls += 1
        return ()

    async def recover_expired_compaction_leases(self, *, now: datetime) -> int:
        """@brief 记录周期回收并可注入首轮故障 / Record periodic recovery and optionally fail its first pass.

        @param now 当前时间 / Current instant.
        @return 测试中无需回收的行 / No rows need recovery in this test.
        @raise RuntimeError 配置时首轮注入 / Injected on the first pass when configured.
        """

        del now
        self.recovered += 1
        if self.recovered >= 2:
            self.periodic_recovery.set()
        if self.fail_first and self.recovered == 1:
            raise RuntimeError("temporary compaction recovery failure")
        return 0


def _claim(*, attempt_count: int = 1) -> Compaction:
    """@brief 构造指定 attempt_count 的 processing Segment / Build a processing segment with a selected attempt count."""

    draft = CompactionPlan.create(
        conversation_id=ConversationId("assistant-user:7"),
        owner_user_id=7,
        epoch_floor_sequence=0,
        from_sequence=1,
        through_sequence=2,
        anchor_turn_id=TurnId.new(),
        predecessor_compaction_id=None,
        projection_version=1,
        source_snapshot=(
            {"role": "user", "content": "remember this"},
            {"role": "assistant", "content": "I will"},
        ),
        source_row_count=2,
        source_token_count=TokenCount(8),
        created_at=NOW,
    )
    segment = Compaction.pending(draft)
    for ordinal in range(attempt_count):
        token = LeaseToken.new()
        claim_time = NOW + timedelta(seconds=ordinal * 2)
        segment = segment.claim(
            token=token,
            claimed_at=claim_time,
            lease_for=timedelta(minutes=1),
        )
        if ordinal + 1 < attempt_count:
            segment = segment.retry(
                token=token,
                failed_at=claim_time + timedelta(microseconds=1),
                retry_at=claim_time + timedelta(seconds=1),
                error="retry",
            )
    return segment


def _worker(
    persistence: _Persistence,
    generator: _Generator,
    *,
    max_attempts: int = 3,
) -> CompactionWorker:
    """@brief 构造 deterministic worker / Build a deterministic worker."""

    return CompactionWorker(
        persistence=persistence,
        generator=generator,
        worker_count=1,
        polling_policy=AdaptivePollingPolicy(0.001, 0.004, jitter_ratio=0.0),
        attempt_timeout=timedelta(seconds=10),
        lease_for=timedelta(seconds=20),
        retry_policy=FullJitterCompactionRetryPolicy(
            max_attempts=max_attempts,
            initial_delay=timedelta(seconds=1),
            max_delay=timedelta(seconds=2),
            jitter=lambda lower, upper: upper,
        ),
        clock=_Clock(),
    )


def test_success_completes_with_the_current_fencing_claim() -> None:
    """@brief 成功摘要只经 persistence completion 提交 / Successful summaries commit only through persistence completion."""

    persistence = _Persistence()
    summary = CompactionSummary("summary", TokenCount(2), "fake:model")
    asyncio.run(_worker(persistence, _Generator(summary)).process_claim(_claim()))
    assert persistence.completed[0][1] == summary
    assert persistence.retried == []
    assert persistence.failed == []


def test_transient_failure_schedules_bounded_retry() -> None:
    """@brief 未耗尽 provider failure 安排 retry / A non-exhausted provider failure schedules retry."""

    persistence = _Persistence()
    worker = _worker(
        persistence,
        _Generator(RetryableCompactionError("busy", retry_after=timedelta(seconds=5))),
    )
    asyncio.run(worker.process_claim(_claim()))
    assert persistence.retried[0][3] == "busy"
    assert persistence.retried[0][2] > persistence.retried[0][1]
    assert persistence.completed == []


def test_exhausted_provider_uses_deterministic_fallback() -> None:
    """@brief provider 尝试耗尽后完成本地 fallback，避免永久阻塞 / Exhausted provider attempts complete a local fallback instead of blocking forever."""

    persistence = _Persistence()
    worker = _worker(
        persistence,
        _Generator(RetryableCompactionError("still unavailable")),
        max_attempts=2,
    )
    asyncio.run(worker.process_claim(_claim(attempt_count=2)))
    assert persistence.completed[0][1].route_key == "deterministic.extractive:v1"
    assert persistence.retried == []
    assert persistence.failed == []


def test_corrupt_source_fails_final_without_provider_retry() -> None:
    """@brief source corruption 直接终结，不伪造 fallback / Source corruption fails finally without fabricating a fallback."""

    persistence = _Persistence()
    worker = _worker(persistence, _Generator(CompactionSourceError("digest drift")))
    asyncio.run(worker.process_claim(_claim()))
    assert persistence.failed[0][2] == "digest drift"
    assert persistence.completed == []
    assert persistence.retried == []


def test_stale_terminal_results_are_discarded_without_failing_the_service() -> None:
    """@brief lease recovery 后的旧 completion/failure 是正常 fencing 竞态 / Old completions and failures after lease recovery are normal fencing races."""

    success = _StalePersistence()
    asyncio.run(
        _worker(
            success,
            _Generator(CompactionSummary("summary", TokenCount(2), "fake:model")),
        ).process_claim(_claim())
    )
    retry = _StalePersistence()
    asyncio.run(
        _worker(
            retry,
            _Generator(RetryableCompactionError("provider unavailable")),
        ).process_claim(_claim())
    )


def test_run_recovers_expired_leases_and_stops_structurally() -> None:
    """@brief work loop 启动先恢复 lease，并响应 structured stop / Work loop recovers leases on startup and honors structured stop."""

    async def scenario() -> None:
        """@brief 运行短生命周期 / Run a short lifecycle."""

        persistence = _Persistence()
        worker = _worker(
            persistence,
            _Generator(CompactionSummary("unused", TokenCount(1), "fake:model")),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        while persistence.recovered == 0:
            await asyncio.sleep(0)
        stop.set()
        await task
        assert persistence.recovered == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("fail_first", [False, True], ids=["steady", "retry"])
def test_run_recovers_periodically_without_blocking_claims(fail_first: bool) -> None:
    """@brief 单独 recovery owner 按 lease cadence 重试且不阻断 claim / A sole recovery owner retries on the lease cadence without blocking claims.

    @param fail_first 是否注入首轮 recovery 故障 / Whether to inject a first-pass recovery failure.
    """

    async def scenario() -> None:
        """@brief 观察启动与第二次周期回收 / Observe startup and the second periodic recovery pass."""

        persistence = _PeriodicRecoveryPersistence(fail_first=fail_first)
        worker = CompactionWorker(
            persistence=persistence,
            generator=_Generator(
                CompactionSummary("unused", TokenCount(1), "fake:model")
            ),
            worker_count=1,
            polling_policy=AdaptivePollingPolicy(
                0.001,
                0.002,
                jitter_ratio=0.0,
            ),
            attempt_timeout=timedelta(milliseconds=1),
            lease_for=timedelta(milliseconds=4),
            clock=_Clock(),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))

        await asyncio.wait_for(persistence.periodic_recovery.wait(), timeout=1)
        assert not task.done()
        assert persistence.claim_calls > 0
        stop.set()
        await task
        assert persistence.recovered >= 2

    asyncio.run(scenario())


def test_run_survives_a_transient_claim_polling_failure() -> None:
    """@brief 单次 claim 故障不会终结整个 BotRuntime 服务 / One claim failure does not terminate the BotRuntime service."""

    async def scenario() -> None:
        """@brief 注入一次数据库轮询故障并观察恢复 / Inject one polling failure and observe recovery."""

        persistence = _OneShotClaimFailurePersistence()
        worker = _worker(
            persistence,
            _Generator(CompactionSummary("unused", TokenCount(1), "fake:model")),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(persistence.recovered_polling.wait(), timeout=1)
        assert not task.done()
        stop.set()
        await task
        assert persistence.claim_calls >= 2

    asyncio.run(scenario())


def test_run_survives_a_transient_completion_failure() -> None:
    """@brief 完成确认故障保留 lease 且不终结服务 / A completion-ack failure leaves the lease and does not terminate the service."""

    async def scenario() -> None:
        """@brief 注入一次完成确认故障并观察继续轮询 / Inject one completion failure and observe continued polling."""

        persistence = _OneShotFinalizeFailurePersistence(_claim())
        worker = _worker(
            persistence,
            _Generator(CompactionSummary("summary", TokenCount(2), "fake:model")),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(persistence.recovered_polling.wait(), timeout=1)
        assert not task.done()
        stop.set()
        await task
        assert persistence.claim_calls >= 2

    asyncio.run(scenario())


def test_run_rejects_a_compaction_batch_larger_than_requested() -> None:
    """@brief 超额 compaction batch 不得绕过 worker 容量 / An oversized compaction batch cannot bypass worker capacity."""

    async def scenario() -> None:
        """@brief 运行一次超额 claim poll / Run one oversized claim poll."""

        persistence = _OverclaimingPersistence(_claim())
        worker = _worker(
            persistence,
            _Generator(CompactionSummary("unused", TokenCount(1), "fake:model")),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))

        await asyncio.wait_for(persistence.overclaimed.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

        assert persistence.completed == []
        assert persistence.retried == []
        assert persistence.failed == []

    asyncio.run(scenario())
