"""@brief 后台调度领域服务与端口 / Background-scheduling domain service and ports."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .models import JobKind, ScheduleClaim, ScheduledJob


logger = logging.getLogger(__name__)


class Clock(Protocol):
    """@brief 调度时钟端口 / Scheduling clock port."""

    def now(self) -> datetime:
        """@brief 返回当前 UTC 时刻 / Return the current UTC time.

        @return UTC aware datetime / UTC-aware datetime.
        """

        ...


class ScheduleRepository(Protocol):
    """@brief 调度持久化端口 / Scheduling persistence port."""

    async def recover_stale(self, now: datetime) -> int:
        """@brief 回收崩溃遗留的执行中任务 / Recover jobs stranded by a crashed worker."""

        ...

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[ScheduleClaim[Any]]:
        """@brief 原子领取到期任务 / Atomically claim due jobs."""

        ...

    async def mark_executed(self, claim: ScheduleClaim[Any]) -> None:
        """@brief 将一次性任务标记完成 / Mark a one-shot job executed."""

        ...

    async def reschedule(
        self,
        claim: ScheduleClaim[Any],
        *,
        last_run_at: datetime,
        next_run_at: datetime,
    ) -> None:
        """@brief 将周期任务推进到下一次运行 / Advance a recurring job."""

        ...

    async def mark_failed(self, claim: ScheduleClaim[Any], error: str) -> None:
        """@brief 标记任务失败 / Mark a job failed."""

        ...


class ScheduledJobHandler(Protocol):
    """@brief 可按任务类型横向注册的处理器 / Horizontally registerable job handler."""

    @property
    def kind(self) -> JobKind:
        """@brief 返回处理器支持的任务类型 / Return the supported job kind."""

        ...

    async def handle(self, job: ScheduledJob[Any]) -> None:
        """@brief 执行已领取任务 / Execute a claimed job."""

        ...


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """@brief 一次守护 tick 的执行报告 / Execution report for one daemon tick.

    @param claimed 领取数量 / Number of claimed jobs.
    @param succeeded 成功数量 / Number of successful jobs.
    @param failed 失败数量 / Number of failed jobs.
    @param skipped 是否因上一轮仍运行而跳过 / Whether the tick was skipped because the prior tick is active.
    """

    claimed: int
    succeeded: int
    failed: int
    skipped: bool = False


class SystemClock:
    """@brief 系统 UTC 时钟 / System UTC clock."""

    def now(self) -> datetime:
        """@brief 返回当前 UTC 时刻 / Return the current UTC time.

        @return UTC aware datetime / UTC-aware datetime.
        """

        return datetime.now(timezone.utc)


class ScheduleDispatcher:
    """@brief 领取并分派后台任务的领域服务 / Domain service that claims and dispatches background jobs."""

    def __init__(
        self,
        *,
        repository: ScheduleRepository,
        handlers: Sequence[ScheduledJobHandler],
        clock: Clock | None = None,
        batch_size: int = 5,
        max_concurrency: int = 3,
        stale_after: timedelta = timedelta(minutes=30),
    ) -> None:
        """@brief 创建调度分派器 / Create a schedule dispatcher.

        @param repository 调度持久化端口 / Scheduling persistence port.
        @param handlers 按类型注册的任务处理器 / Job handlers registered by kind.
        @param clock 可替换时钟 / Replaceable clock.
        @param batch_size 每轮最大领取数 / Maximum claims per tick.
        @param max_concurrency 单轮最大并发执行数 / Maximum concurrent executions per tick.
        @param stale_after 执行中任务的崩溃回收阈值 / Recovery threshold for stranded executing jobs.
        """

        if batch_size < 1:
            raise ValueError("batch_size must be at least one")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        handler_map = {handler.kind: handler for handler in handlers}
        if len(handler_map) != len(handlers):
            raise ValueError("Duplicate scheduled-job handlers are not allowed")
        self._repository = repository
        self._handlers: Mapping[JobKind, ScheduledJobHandler] = handler_map
        self._clock = clock or SystemClock()
        self._batch_size = batch_size
        self._max_concurrency = max_concurrency
        self._stale_after = stale_after
        self._tick_lock = asyncio.Lock()

    async def tick(self) -> DispatchReport:
        """@brief 执行一次短生命周期守护轮询 / Run one bounded daemon polling tick.

        @return 类型化执行报告 / Typed dispatch report.
        """

        if self._tick_lock.locked():
            return DispatchReport(0, 0, 0, skipped=True)

        async with self._tick_lock:
            now = self._clock.now()
            recovered = await self._repository.recover_stale(now)
            if recovered:
                logger.warning("Recovered %s stale scheduled jobs", recovered)
            claims = tuple(
                await self._repository.claim_due(
                    now=now,
                    limit=self._batch_size,
                    lease_for=self._stale_after,
                )
            )
            if not claims:
                return DispatchReport(0, 0, 0)

            semaphore = asyncio.Semaphore(self._max_concurrency)

            async def execute(claim: ScheduleClaim[Any]) -> bool:
                """@brief 在并发上限内执行单个任务 / Execute one job under the concurrency limit."""

                async with semaphore:
                    return await self._execute(claim)

            results = await asyncio.gather(*(execute(claim) for claim in claims))
            succeeded = sum(results)
            return DispatchReport(
                claimed=len(claims),
                succeeded=succeeded,
                failed=len(claims) - succeeded,
            )

    async def _execute(self, claim: ScheduleClaim[Any]) -> bool:
        """@brief 执行并终结一个任务状态 / Execute a job and finalize its state.

        @param claim 带 fencing token 的领取凭证 / Claim carrying a fencing token.
        @return 成功返回 True / True on success.
        """

        job = claim.job
        handler = self._handlers.get(job.kind)
        if handler is None:
            error = f"No handler registered for scheduled job kind: {job.kind.value}"
            await self._repository.mark_failed(claim, error)
            logger.error(error)
            return False

        try:
            await handler.handle(job)
            next_run_at = job.recurrence.next_after(job.run_at, self._clock.now())
            if next_run_at is None:
                await self._repository.mark_executed(claim)
            else:
                await self._repository.reschedule(
                    claim,
                    last_run_at=job.run_at,
                    next_run_at=next_run_at,
                )
            return True
        except Exception as exc:
            logger.exception("Scheduled job %s failed: %s", job.schedule_id, exc)
            error = str(exc)[:500] or exc.__class__.__name__
            await self._repository.mark_failed(claim, error)
            return False
