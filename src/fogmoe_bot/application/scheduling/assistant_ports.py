"""@brief 定时 Assistant bounded context 的应用端口 / Application ports for the scheduled-Assistant bounded context."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from fogmoe_bot.application.assistant.inference_command import DurableAssistantUser
from fogmoe_bot.application.conversation.workflow import PreparedTurnAcceptance
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    Cadence,
    MisfirePolicy,
    ScheduleClaim,
    ScheduledAssistantTurn,
    ScheduleSnapshot,
    ScheduleTarget,
)
from fogmoe_bot.domain.temporal import TimeZoneId, ensure_utc


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleDefinition:
    """@brief 创建或完整替换 schedule 的定义 / Definition for creating or fully replacing a schedule.

    @param creator_user_id 创建者 / Creator user identifier.
    @param target 冻结投递目标 / Frozen delivery target.
    @param trigger_reason 触发原因 / Trigger reason.
    @param instruction Assistant 指令 / Assistant instruction.
    @param cadence 明确周期变体 / Explicit cadence variant.
    @param first_run_at 首次 UTC occurrence / First UTC occurrence.
    @param time_zone 冻结 IANA 时区 / Frozen IANA time zone.
    @param context_snapshot 创建时上下文 / Context captured at creation.
    @param misfire_policy 过期策略 / Misfire policy.
    @param misfire_grace 允许迟到窗口 / Allowed lateness window.
    """

    creator_user_id: int
    target: ScheduleTarget
    trigger_reason: str
    instruction: str
    cadence: Cadence
    first_run_at: datetime
    time_zone: TimeZoneId
    context_snapshot: str | None = None
    misfire_policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE
    misfire_grace: timedelta | None = None

    def __post_init__(self) -> None:
        """@brief 校验新定义边界 / Validate new-definition bounds.

        @return None / None.
        @raise ValueError 创建者、文本或 grace 非法时抛出 / Raised for an invalid creator, text, or grace.
        """

        if isinstance(self.creator_user_id, bool) or self.creator_user_id <= 0:
            raise ValueError("Schedule creator_user_id must be positive")
        reason = self.trigger_reason.strip()
        instruction = self.instruction.strip()
        context = (
            None if self.context_snapshot is None else self.context_snapshot.strip()
        )
        if not reason or len(reason) > 200:
            raise ValueError("Schedule trigger_reason must contain 1-200 characters")
        if not instruction or len(instruction) > 20_000:
            raise ValueError("Schedule instruction must contain 1-20000 characters")
        if context == "":
            context = None
        if context is not None and len(context) > 20_000:
            raise ValueError("Schedule context_snapshot cannot exceed 20000 characters")
        grace = self.misfire_grace
        if grace is not None and grace <= timedelta():
            raise ValueError("Schedule misfire_grace must be positive")
        if self.misfire_policy is MisfirePolicy.SKIP and grace is None:
            raise ValueError("SKIP misfire policy requires a grace window")
        object.__setattr__(self, "trigger_reason", reason)
        object.__setattr__(self, "instruction", instruction)
        object.__setattr__(self, "context_snapshot", context)
        object.__setattr__(self, "first_run_at", ensure_utc(self.first_run_at))


class ScheduleCatalog(Protocol):
    """@brief 用户 CRUD 所需 schedule catalog / Schedule catalog required by user CRUD."""

    async def lock_scope(self, creator_user_id: int, conversation_id: str) -> None:
        """@brief 串行化一个创建者与目标 scope 的 mutation / Serialize mutations for one creator-target scope."""

        ...

    async def count_active(self, creator_user_id: int, conversation_id: str) -> int:
        """@brief 统计 active schedules / Count active schedules."""

        ...

    async def create(
        self,
        definition: ScheduleDefinition,
        *,
        created_at: datetime,
    ) -> ScheduledAssistantTurn:
        """@brief 创建永不复用 identity 的 schedule / Create a schedule whose identity is never reused."""

        ...

    async def replace(
        self,
        schedule_id: int,
        definition: ScheduleDefinition,
        *,
        updated_at: datetime,
    ) -> ScheduledAssistantTurn | None:
        """@brief 完整替换当前 scope 内尚未处理的 schedule / Fully replace an unclaimed schedule in the current scope."""

        ...

    async def list(
        self,
        *,
        creator_user_id: int,
        conversation_id: str,
        limit: int,
    ) -> Sequence[ScheduleSnapshot]:
        """@brief 列出当前创建者与目标 scope 的 schedules / List schedules in the current creator-target scope."""

        ...

    async def cancel(
        self,
        *,
        schedule_id: int,
        creator_user_id: int,
        conversation_id: str,
        cancelled_at: datetime,
    ) -> bool:
        """@brief 取消当前或未来 occurrence / Cancel the current or future occurrence."""

        ...


class ScheduleQueue(Protocol):
    """@brief fenced schedule queue 端口 / Fenced schedule-queue port."""

    async def recover_expired(self, *, now: datetime) -> int:
        """@brief 回收崩溃遗留 lease / Recover leases stranded by crashes."""

        ...

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[ScheduleClaim]:
        """@brief 原子领取到期 occurrence / Atomically claim due occurrences."""

        ...

    async def retry(
        self,
        claim: ScheduleClaim,
        *,
        retry_at: datetime,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 以当前 token 安排重试 / Schedule a retry with the current token."""

        ...

    async def fail_final(
        self,
        claim: ScheduleClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 以当前 token 终结不可恢复 occurrence / Finally fail an unrecoverable occurrence with the current token."""

        ...

    async def skip_misfire(
        self,
        claim: ScheduleClaim,
        *,
        next_run_at: datetime | None,
        skipped_at: datetime,
    ) -> None:
        """@brief 不产生 Turn 地推进或终结过期 occurrence / Advance or expire a misfired occurrence without creating a Turn."""

        ...


class ScheduledOccurrenceAcceptance(Protocol):
    """@brief Schedule cursor 与 Conversation Turn 的跨聚合 UoW / Cross-aggregate UoW for the schedule cursor and Conversation Turn."""

    async def accept(
        self,
        claim: ScheduleClaim,
        prepared: PreparedTurnAcceptance,
        *,
        next_run_at: datetime | None,
        accepted_at: datetime,
    ) -> None:
        """@brief 原子接受 Turn 并推进 schedule / Atomically accept the Turn and advance the schedule."""

        ...


class ScheduledAssistantProfileReader(Protocol):
    """@brief 读取 occurrence 创建者快照 / Read the occurrence creator snapshot."""

    async def read(self, user_id: int) -> DurableAssistantUser | None:
        """@brief 读取 acceptance-time 用户 / Read the acceptance-time user."""

        ...


__all__ = [
    "ScheduleCatalog",
    "ScheduleDefinition",
    "ScheduleQueue",
    "ScheduledAssistantProfileReader",
    "ScheduledOccurrenceAcceptance",
]
