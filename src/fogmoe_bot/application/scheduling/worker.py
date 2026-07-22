"""@brief 专用 Scheduled-Assistant worker / Dedicated Scheduled-Assistant worker."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import timedelta

from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock
from fogmoe_bot.application.scheduling.assistant_ports import (
    ScheduleQueue,
    ScheduledAssistantProfileReader,
    ScheduledOccurrenceAcceptance,
)
from fogmoe_bot.application.scheduling.occurrence import prepare_scheduled_occurrence
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    MisfirePolicy,
    ScheduleClaim,
    StaleScheduleClaimError,
)
from fogmoe_bot.domain.temporal import ensure_utc


logger = logging.getLogger(__name__)


class ScheduleWorker:
    """@brief 领取、判定 misfire 并原子接受 scheduled Turns / Claim, classify misfires, and atomically accept scheduled Turns."""

    def __init__(
        self,
        *,
        queue: ScheduleQueue,
        acceptance: ScheduledOccurrenceAcceptance,
        profiles: ScheduledAssistantProfileReader,
        worker_count: int,
        poll_interval: float,
        lease_for: timedelta,
        attempt_timeout: timedelta,
        max_attempts: int,
        retry_base: float,
        retry_cap: float,
        clock: UtcClock | None = None,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        """@brief 创建有界 worker / Create a bounded worker.

        @raise ValueError 并发、timeout、lease 或 retry 设置非法时抛出 /
            Raised for invalid concurrency, timeout, lease, or retry settings.
        """

        if worker_count < 1:
            raise ValueError("worker_count must be positive")
        if poll_interval <= 0.0:
            raise ValueError("poll_interval must be positive")
        if attempt_timeout <= timedelta() or lease_for <= attempt_timeout:
            raise ValueError("lease_for must be longer than a positive attempt_timeout")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if retry_base <= 0.0 or retry_cap < retry_base:
            raise ValueError("retry bounds must be positive and ordered")
        self._queue = queue
        self._acceptance = acceptance
        self._profiles = profiles
        self._worker_count = worker_count
        self._poll_interval = poll_interval
        self._lease_for = lease_for
        self._attempt_timeout = attempt_timeout
        self._max_attempts = max_attempts
        self._retry_base = retry_base
        self._retry_cap = retry_cap
        self._clock = clock or SystemUtcClock()
        self._jitter = jitter

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行至收到停止信号 / Run until a stop signal is received.

        @param stop_event 进程停止信号 / Process stop signal.
        @return None / None.
        """

        while not stop_event.is_set():
            handled = await self.process_once()
            if handled:
                continue
            try:
                async with asyncio.timeout(self._poll_interval):
                    await stop_event.wait()
            except TimeoutError:
                pass

    async def process_once(self) -> int:
        """@brief 回收 leases 并并发处理一批 claims / Recover leases and concurrently process one claim batch.

        @return 本轮 claim 数 / Number of claims in this batch.
        """

        now = ensure_utc(self._clock.now())
        recovered = await self._queue.recover_expired(now=now)
        if recovered:
            logger.warning("Recovered %s expired schedule leases", recovered)
        claims = tuple(
            await self._queue.claim_due(
                now=now,
                limit=self._worker_count,
                lease_for=self._lease_for,
            )
        )
        if not claims:
            return 0
        async with asyncio.TaskGroup() as tasks:
            for claim in claims:
                tasks.create_task(
                    self._execute(claim),
                    name=f"schedule-{claim.schedule.schedule_id}",
                )
        return len(claims)

    async def _execute(self, claim: ScheduleClaim) -> None:
        """@brief 处理并 fenced 终结一个 claim / Process and fenced-finalize one claim.

        @param claim 当前领取 / Current claim.
        @return None / None.
        """

        try:
            async with asyncio.timeout(self._attempt_timeout.total_seconds()):
                await self._execute_within_timeout(claim)
        except StaleScheduleClaimError:
            logger.warning(
                "Schedule claim became stale: schedule_id=%s",
                claim.schedule.schedule_id,
            )
        except (LookupError, ValueError, TypeError) as error:
            await self._final_failure(claim, error)
        except Exception as error:
            await self._retry_or_fail(claim, error)

    async def _execute_within_timeout(self, claim: ScheduleClaim) -> None:
        """@brief 在 attempt timeout 内接受或跳过 occurrence / Accept or skip an occurrence within its attempt timeout."""

        schedule = claim.schedule
        now = ensure_utc(self._clock.now())
        if (
            schedule.misfire_grace is not None
            and now > schedule.next_run_at + schedule.misfire_grace
            and schedule.misfire_policy is MisfirePolicy.SKIP
        ):
            await self._queue.skip_misfire(
                claim,
                next_run_at=schedule.next_occurrence(after=now),
                skipped_at=now,
            )
            return
        user = await self._profiles.read(schedule.creator_user_id)
        if user is None:
            raise LookupError(
                f"Scheduled Assistant creator not found: {schedule.creator_user_id}"
            )
        prepared = prepare_scheduled_occurrence(
            schedule,
            user=user,
            observed_at=now,
        )
        await self._acceptance.accept(
            claim,
            prepared,
            next_run_at=schedule.next_occurrence(after=now),
            accepted_at=now,
        )

    async def _retry_or_fail(self, claim: ScheduleClaim, error: Exception) -> None:
        """@brief 对瞬态失败执行 bounded full-jitter retry / Apply bounded full-jitter retry to a transient failure."""

        if claim.attempt_count >= self._max_attempts:
            await self._final_failure(claim, error)
            return
        failed_at = ensure_utc(self._clock.now())
        exponent = min(claim.attempt_count - 1, 30)
        ceiling = min(self._retry_cap, self._retry_base * (2**exponent))
        delay = max(0.0, self._jitter(0.0, ceiling))
        try:
            await self._queue.retry(
                claim,
                retry_at=failed_at + timedelta(seconds=delay),
                failed_at=failed_at,
                error=_error_text(error),
            )
        except StaleScheduleClaimError:
            logger.warning(
                "Schedule claim became stale before retry: schedule_id=%s",
                claim.schedule.schedule_id,
            )

    async def _final_failure(self, claim: ScheduleClaim, error: Exception) -> None:
        """@brief fenced 写入最终失败 / Persist a final failure with fencing."""

        try:
            await self._queue.fail_final(
                claim,
                failed_at=ensure_utc(self._clock.now()),
                error=_error_text(error),
            )
        except StaleScheduleClaimError:
            logger.warning(
                "Schedule claim became stale before final failure: schedule_id=%s",
                claim.schedule.schedule_id,
            )


def _error_text(error: Exception) -> str:
    """@brief 构造有界错误摘要 / Build a bounded error summary.

    @param error 失败异常 / Failure exception.
    @return 非空有界文本 / Non-empty bounded text.
    """

    return (str(error).strip() or error.__class__.__name__)[:1_000]


__all__ = ["ScheduleWorker"]
