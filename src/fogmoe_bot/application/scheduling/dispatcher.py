"""@brief 持久化调度分派器 / Persisted schedule dispatcher."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Generic, TypeVar

from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock
from fogmoe_bot.application.scheduling.ports import (
    ScheduledJobHandler,
    ScheduleRepositoryPort,
)
from fogmoe_bot.domain.scheduling import (
    JobKind,
    ScheduleClaim,
    StaleScheduleClaimError,
)


logger = logging.getLogger(__name__)

PayloadT = TypeVar("PayloadT")


class ScheduleDispatcher(Generic[PayloadT]):
    """@brief 领取、分派并终结持久化任务 / Claim, dispatch, and finalize persisted jobs."""

    def __init__(
        self,
        *,
        repository: ScheduleRepositoryPort[PayloadT],
        handlers: Sequence[ScheduledJobHandler[PayloadT]],
        clock: UtcClock | None = None,
        batch_size: int = 5,
        stale_after: timedelta = timedelta(minutes=30),
    ) -> None:
        """@brief 创建调度分派器 / Create a schedule dispatcher.

        @param repository 调度持久化端口 / Scheduling persistence port.
        @param handlers 按类型注册的处理器 / Handlers registered by job kind.
        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        @param batch_size 单次最大领取数 / Maximum claims per batch.
        @param stale_after 执行租约持续时间 / Execution lease duration.
        @raise ValueError 配置非法或处理器重复时抛出 / Raised for invalid configuration or duplicate handlers.
        """

        if batch_size < 1:
            raise ValueError("batch_size must be at least one")
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        handler_map = {handler.kind: handler for handler in handlers}
        if len(handler_map) != len(handlers):
            raise ValueError("Duplicate scheduled-job handlers are not allowed")
        self._repository = repository
        self._handlers: Mapping[JobKind, ScheduledJobHandler[PayloadT]] = handler_map
        self._clock = clock or SystemUtcClock()
        self._batch_size = batch_size
        self._stale_after = stale_after

    async def claim_due(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[ScheduleClaim[PayloadT], ...]:
        """@brief 回收陈旧租约并领取任务 / Recover stale leases and claim jobs.

        @param limit 可选领取上限 / Optional claim limit.
        @return 带 fencing token 的领取凭证 / Claims carrying fencing tokens.
        @note 调用方必须为每个返回值调用 execute_claim / Callers must execute every returned claim.
        """

        claim_limit = (
            self._batch_size if limit is None else min(self._batch_size, limit)
        )
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

    async def execute_claim(self, claim: ScheduleClaim[PayloadT]) -> bool:
        """@brief 执行并终结一个领取 / Execute and finalize one claim.

        @param claim 带 fencing token 的领取凭证 / Claim carrying a fencing token.
        @return 业务处理成功时返回 True / True when business handling succeeds.
        """

        job = claim.job
        handler = self._handlers.get(job.kind)
        if handler is None:
            error = f"No handler registered for scheduled job kind: {job.kind.value}"
            try:
                await self._repository.mark_failed(claim, error)
            except StaleScheduleClaimError:
                logger.warning(
                    "Scheduled job claim became stale before missing-handler finalization: schedule_id=%s",
                    job.schedule_id,
                )
            logger.error(error)
            return False

        try:
            await handler.handle(job)
            next_run_at = job.recurrence.next_after(job.run_at, self._clock.now())
        except StaleScheduleClaimError:
            logger.warning(
                "Scheduled job claim became stale during handling: schedule_id=%s",
                job.schedule_id,
            )
            return False
        except Exception as exc:
            logger.exception("Scheduled job %s failed: %s", job.schedule_id, exc)
            error = str(exc)[:500] or exc.__class__.__name__
            try:
                await self._repository.mark_failed(claim, error)
            except StaleScheduleClaimError:
                logger.warning(
                    "Scheduled job claim became stale before failure finalization: schedule_id=%s",
                    job.schedule_id,
                )
            return False

        try:
            if next_run_at is None:
                await self._repository.mark_executed(claim)
            else:
                await self._repository.reschedule(
                    claim,
                    last_run_at=job.run_at,
                    next_run_at=next_run_at,
                )
        except StaleScheduleClaimError:
            logger.warning(
                "Scheduled job claim became stale before finalization: schedule_id=%s",
                job.schedule_id,
            )
            return False
        return True


__all__ = ["ScheduleDispatcher"]
