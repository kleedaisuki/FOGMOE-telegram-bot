"""@brief 后台调度分派器测试 / Background schedule-dispatcher tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fogmoe_bot.domain.scheduling import (
    JobKind,
    Recurrence,
    ScheduleClaim,
    ScheduleDispatcher,
    ScheduledJob,
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

    async def recover_stale(self, now: datetime) -> int:
        self.recovered += 1
        return 0

    async def claim_due(self, *, now: datetime, limit: int, lease_for: timedelta):
        return tuple(
            ScheduleClaim(job, f"token-{job.schedule_id}", now + lease_for)
            for job in self.jobs[:limit]
        )

    async def mark_executed(self, claim: ScheduleClaim[Any]) -> None:
        self.executed.append(claim.job.schedule_id)

    async def reschedule(
        self,
        claim: ScheduleClaim[Any],
        *,
        last_run_at: datetime,
        next_run_at: datetime,
    ) -> None:
        self.rescheduled.append((claim.job.schedule_id, last_run_at, next_run_at))

    async def mark_failed(self, claim: ScheduleClaim[Any], error: str) -> None:
        self.failed.append((claim.job.schedule_id, error))


class _Handler:
    """@brief 记录已处理任务的测试 handler / Test handler that records handled jobs."""

    kind = JobKind("test.job")

    def __init__(self) -> None:
        self.handled: list[int] = []

    async def handle(self, job: ScheduledJob[Any]) -> None:
        self.handled.append(job.schedule_id)


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

    report = asyncio.run(dispatcher.tick())

    assert report.claimed == report.succeeded == 1
    assert report.failed == 0
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

    report = asyncio.run(dispatcher.tick())

    assert report.failed == 1
    assert repository.failed == [
        (2, "No handler registered for scheduled job kind: missing.job")
    ]
