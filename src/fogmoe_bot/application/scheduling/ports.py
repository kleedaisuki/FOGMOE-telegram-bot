"""@brief 调度应用端口 / Scheduling application ports."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol, TypeVar

from fogmoe_bot.domain.scheduling import (
    JobKind,
    MaintenanceTask,
    ScheduleClaim,
    ScheduledJob,
)


PayloadT = TypeVar("PayloadT")


class ScheduleRepositoryPort(Protocol[PayloadT]):
    """@brief 调度持久化端口 / Scheduling persistence port."""

    async def recover_stale(self, now: datetime) -> int:
        """@brief 回收崩溃遗留任务 / Recover jobs stranded by a crashed worker.

        @param now 当前 UTC 时刻 / Current UTC time.
        @return 回收任务数 / Number of recovered jobs.
        """

        ...

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[ScheduleClaim[PayloadT]]:
        """@brief 原子领取到期任务 / Atomically claim due jobs.

        @param now 当前 UTC 时刻 / Current UTC time.
        @param limit 最大领取数量 / Maximum number of claims.
        @param lease_for 租约持续时间 / Lease duration.
        @return 带 fencing token 的领取凭证 / Claims carrying fencing tokens.
        """

        ...

    async def mark_executed(self, claim: ScheduleClaim[PayloadT]) -> None:
        """@brief 将一次性任务标记完成 / Mark a one-shot job executed.

        @param claim 当前领取凭证 / Current claim.
        @return None / None.
        @raise StaleScheduleClaimError claim token 已被回收或替换 / The claim token was recovered or replaced.
        """

        ...

    async def reschedule(
        self,
        claim: ScheduleClaim[PayloadT],
        *,
        last_run_at: datetime,
        next_run_at: datetime,
    ) -> None:
        """@brief 推进周期任务 / Advance a recurring job.

        @param claim 当前领取凭证 / Current claim.
        @param last_run_at 本次计划时刻 / Current scheduled occurrence.
        @param next_run_at 下一计划时刻 / Next scheduled occurrence.
        @return None / None.
        @raise StaleScheduleClaimError claim token 已被回收或替换 / The claim token was recovered or replaced.
        """

        ...

    async def mark_failed(self, claim: ScheduleClaim[PayloadT], error: str) -> None:
        """@brief 标记任务失败 / Mark a job failed.

        @param claim 当前领取凭证 / Current claim.
        @param error 有界错误描述 / Bounded error description.
        @return None / None.
        @raise StaleScheduleClaimError claim token 已被回收或替换 / The claim token was recovered or replaced.
        """

        ...


class ScheduledJobHandler(Protocol[PayloadT]):
    """@brief 按任务类型注册的处理器端口 / Job-kind handler port."""

    @property
    def kind(self) -> JobKind:
        """@brief 返回支持的任务类型 / Return the supported job kind.

        @return 稳定任务类型 / Stable job kind.
        """

        ...

    async def handle(self, job: ScheduledJob[PayloadT]) -> None:
        """@brief 执行一个已领取任务 / Execute a claimed job.

        @param job 已领取任务 / Claimed job.
        @return None / None.
        """

        ...


class MaintenanceTaskHandler(Protocol):
    """@brief 进程内周期维护处理器端口 / In-process maintenance handler port."""

    @property
    def task(self) -> MaintenanceTask:
        """@brief 返回维护任务定义 / Return the maintenance-task definition.

        @return 维护任务定义 / Maintenance-task definition.
        """

        ...

    async def handle(self) -> None:
        """@brief 执行一次维护任务 / Execute one maintenance occurrence.

        @return None / None.
        """

        ...


__all__ = [
    "MaintenanceTaskHandler",
    "ScheduledJobHandler",
    "ScheduleRepositoryPort",
]
