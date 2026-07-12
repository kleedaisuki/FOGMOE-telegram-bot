"""@brief 后台调度分派器测试 / Background schedule-dispatcher tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from fogmoe_bot.application.scheduling.dispatcher import ScheduleDispatcher
from fogmoe_bot.domain.scheduling import (
    JobKind,
    Recurrence,
    RecurrenceUnit,
    ScheduleClaim,
    ScheduledJob,
    StaleScheduleClaimError,
)


class _FixedClock:
    """@brief 固定时钟测试替身 / Fixed-clock test double."""

    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class _Repository:
    """@brief 内存仓储测试替身 / In-memory repository test double."""

    def __init__(self, jobs: tuple[ScheduledJob[Any], ...]) -> None:
        self.jobs = jobs
        self.recovered = 0
        self.executed: list[int] = []
        self.failed: list[tuple[int, str]] = []
        self.rescheduled: list[tuple[int, datetime, datetime]] = []
        self.stale_claims: set[str] = set()

    async def recover_stale(self, now: datetime) -> int:
        self.recovered += 1
        return 0

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[ScheduleClaim[Any], ...]:
        return tuple(
            ScheduleClaim(job, f"token-{job.schedule_id}", now + lease_for)
            for job in self.jobs[:limit]
        )

    async def mark_executed(self, claim: ScheduleClaim[Any]) -> None:
        if claim.token in self.stale_claims:
            raise StaleScheduleClaimError("claim was recovered")
        self.executed.append(claim.job.schedule_id)

    async def reschedule(
        self,
        claim: ScheduleClaim[Any],
        *,
        last_run_at: datetime,
        next_run_at: datetime,
    ) -> None:
        if claim.token in self.stale_claims:
            raise StaleScheduleClaimError("claim was recovered")
        self.rescheduled.append((claim.job.schedule_id, last_run_at, next_run_at))

    async def mark_failed(self, claim: ScheduleClaim[Any], error: str) -> None:
        if claim.token in self.stale_claims:
            raise StaleScheduleClaimError("claim was recovered")
        self.failed.append((claim.job.schedule_id, error))


class _Handler:
    """@brief 记录已处理任务的测试 handler / Test handler that records handled jobs."""

    kind = JobKind("test.job")

    def __init__(self) -> None:
        self.handled: list[int] = []

    async def handle(self, job: ScheduledJob[Any]) -> None:
        self.handled.append(job.schedule_id)


class _FailingFinalizationRepository(_Repository):
    """@brief 模拟 handler 成功后的瞬态 finalize 故障 / Simulate transient finalization failure after handler success."""

    async def mark_executed(self, claim: ScheduleClaim[Any]) -> None:
        """@brief 拒绝本次 finalize / Fail this finalization attempt.

        @param claim 当前 claim / Current claim.
        @return 不返回 / Does not return.
        @raise RuntimeError 模拟数据库瞬态故障 / Simulated transient database failure.
        """

        del claim
        raise RuntimeError("temporary finalization failure")

    async def reschedule(
        self,
        claim: ScheduleClaim[Any],
        *,
        last_run_at: datetime,
        next_run_at: datetime,
    ) -> None:
        """@brief 拒绝周期任务 finalize / Fail recurring-job finalization.

        @param claim 当前 claim / Current claim.
        @param last_run_at 当前计划时刻 / Current scheduled instant.
        @param next_run_at 下一计划时刻 / Next scheduled instant.
        @return 不返回 / Does not return.
        @raise RuntimeError 模拟数据库瞬态故障 / Simulated transient database failure.
        """

        del claim, last_run_at, next_run_at
        raise RuntimeError("temporary finalization failure")


def test_dispatcher_executes_and_finalizes_one_shot_job() -> None:
    """@brief 一次性任务由类型 handler 执行并完成 / A typed handler executes and completes a one-shot job."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = ScheduledJob(1, 42, JobKind("test.job"), now, now, Recurrence(), {"x": 1})
    repository = _Repository((job,))
    handler = _Handler()
    dispatcher = ScheduleDispatcher(
        repository=repository,
        handlers=(handler,),
        clock=_FixedClock(now),
    )

    claims = asyncio.run(dispatcher.claim_due())
    results = [asyncio.run(dispatcher.execute_claim(claim)) for claim in claims]

    assert results == [True]
    assert handler.handled == [1]
    assert repository.executed == [1]
    assert repository.recovered == 1


def test_dispatcher_fails_job_without_registered_handler() -> None:
    """@brief 未注册任务类型进入显式失败态 / An unregistered job kind enters an explicit failed state."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = ScheduledJob(2, 42, JobKind("missing.job"), now, now, Recurrence(), None)
    repository = _Repository((job,))
    dispatcher = ScheduleDispatcher(
        repository=repository,
        handlers=(),
        clock=_FixedClock(now),
    )

    claims = asyncio.run(dispatcher.claim_due())
    results = [asyncio.run(dispatcher.execute_claim(claim)) for claim in claims]

    assert results == [False]
    assert repository.failed == [
        (2, "No handler registered for scheduled job kind: missing.job")
    ]


def test_dispatcher_does_not_report_success_after_claim_is_recovered() -> None:
    """@brief 旧 token 被 lease recovery 替换后不得误报成功 / A lease-recovered old token must not be reported as successful."""

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = ScheduledJob(3, 42, JobKind("test.job"), now, now, Recurrence(), None)
    repository = _Repository((job,))
    handler = _Handler()
    dispatcher = ScheduleDispatcher(
        repository=repository,
        handlers=(handler,),
        clock=_FixedClock(now),
    )

    claim = asyncio.run(dispatcher.claim_due())[0]
    repository.stale_claims.add(claim.token)

    assert asyncio.run(dispatcher.execute_claim(claim)) is False
    assert handler.handled == [3]
    assert repository.executed == []
    assert repository.failed == []


@pytest.mark.parametrize(
    "recurrence",
    (Recurrence(), Recurrence(RecurrenceUnit.MINUTE, 5)),
)
def test_dispatcher_leaves_claim_for_recovery_when_success_finalization_fails(
    recurrence: Recurrence,
) -> None:
    """@brief handler 成功后的 finalize 故障不得误写永久失败 / A post-handler finalization failure must not write permanent failure."""

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    job = ScheduledJob(4, 42, JobKind("test.job"), now, now, recurrence, None)
    repository = _FailingFinalizationRepository((job,))
    handler = _Handler()
    dispatcher = ScheduleDispatcher(
        repository=repository,
        handlers=(handler,),
        clock=_FixedClock(now),
    )

    claim = asyncio.run(dispatcher.claim_due())[0]
    with pytest.raises(RuntimeError, match="temporary finalization failure"):
        asyncio.run(dispatcher.execute_claim(claim))

    assert handler.handled == [4]
    assert repository.executed == []
    assert repository.failed == []
