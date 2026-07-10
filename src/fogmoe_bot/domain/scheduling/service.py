"""@brief 后台调度领域服务与端口 / Background-scheduling domain service and ports."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .models import JobKind, MaintenanceTask, ScheduleClaim, ScheduledJob


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


class MaintenanceTaskHandler(Protocol):
    """@brief 周期维护任务的类型化处理器 / Typed handler for a periodic maintenance task."""

    @property
    def task(self) -> MaintenanceTask:
        """@brief 返回维护任务定义 / Return the maintenance-task definition.

        @return 维护任务定义 / Maintenance-task definition.
        """

        ...

    async def handle(self) -> None:
        """@brief 执行一次维护任务 / Execute one maintenance task occurrence.

        @return None / None.
        """

        ...


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
        stale_after: timedelta = timedelta(minutes=30),
    ) -> None:
        """@brief 创建调度分派器 / Create a schedule dispatcher.

        @param repository 调度持久化端口 / Scheduling persistence port.
        @param handlers 按类型注册的任务处理器 / Job handlers registered by kind.
        @param clock 可替换时钟 / Replaceable clock.
        @param batch_size 单次领取的最大任务数 / Maximum jobs in a single claim.
        @param stale_after 执行中任务的崩溃回收阈值 / Recovery threshold for stranded executing jobs.
        """

        if batch_size < 1:
            raise ValueError("batch_size must be at least one")
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        handler_map = {handler.kind: handler for handler in handlers}
        if len(handler_map) != len(handlers):
            raise ValueError("Duplicate scheduled-job handlers are not allowed")
        self._repository = repository
        self._handlers: Mapping[JobKind, ScheduledJobHandler] = handler_map
        self._clock = clock or SystemClock()
        self._batch_size = batch_size
        self._stale_after = stale_after

    async def claim_due(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[ScheduleClaim[Any], ...]:
        """@brief 快速回收并领取任务，不执行 handler / Recover and claim jobs without running handlers.

        @param limit 可选领取上限 / Optional claim limit.
        @return 带 fencing token 的领取凭证 / Claims carrying fencing tokens.
        @note 供独立 worker 使用；调用方必须最终调用 execute_claim /
        Used by independent workers; callers must eventually invoke execute_claim.
        """

        claim_limit = self._batch_size if limit is None else min(self._batch_size, limit)
        if claim_limit < 1:
            return ()
        now = self._clock.now()
        recovered = await self._repository.recover_stale(now)
        if recovered:
            logger.warning("Recovered %s stale scheduled jobs", recovered)
        return tuple(
            await self._repository.claim_due(
                now=now,
                limit=claim_limit,
                lease_for=self._stale_after,
            )
        )

    async def execute_claim(self, claim: ScheduleClaim[Any]) -> bool:
        """@brief 执行已领取任务 / Execute a previously claimed job.

        @param claim 带 fencing token 的领取凭证 / Claim carrying a fencing token.
        @return 成功时返回 True / True on success.
        """

        return await self._execute(claim)

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
