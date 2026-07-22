"""@brief 定时 Assistant CRUD 用例 / Scheduled-Assistant CRUD use cases."""

from __future__ import annotations

from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock
from fogmoe_bot.application.scheduling.assistant_ports import (
    ScheduleCatalog,
    ScheduleDefinition,
)
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    ScheduledAssistantTurn,
    ScheduleSnapshot,
)

MAX_ACTIVE_SCHEDULES_PER_SCOPE = 32
"""@brief 每个创建者与 Conversation 的 active schedule 上限 / Active-schedule cap per creator and Conversation."""

MAX_LISTED_SCHEDULES = 128
"""@brief 单次列出上限 / Per-list hard limit."""


class SchedulingService:
    """@brief 串联配额、scope ownership 与 catalog 的应用服务 / Application service coordinating quota, scope ownership, and the catalog."""

    def __init__(self, *, clock: UtcClock | None = None) -> None:
        """@brief 创建 Scheduling 应用服务 / Create the Scheduling application service.

        @param clock 可替换 UTC 时钟 / Replaceable UTC clock.
        """

        self._clock = clock or SystemUtcClock()

    async def create(
        self,
        definition: ScheduleDefinition,
        *,
        catalog: ScheduleCatalog,
    ) -> ScheduledAssistantTurn:
        """@brief 在 scope 配额内创建 schedule / Create a schedule within its scope quota.

        @param definition 完整新定义 / Complete new definition.
        @param catalog 当前 mutation UoW 绑定的 catalog / Catalog bound to the current mutation UoW.
        @return 新 schedule / New schedule.
        @raise ValueError 首次 occurrence 不在未来或配额已满时抛出 /
            Raised when the first occurrence is not in the future or quota is exhausted.
        """

        now = self._clock.now()
        if definition.first_run_at <= now:
            raise ValueError("Schedule first occurrence must be in the future")
        conversation_id = str(definition.target.conversation_id)
        await catalog.lock_scope(definition.creator_user_id, conversation_id)
        active = await catalog.count_active(
            definition.creator_user_id,
            conversation_id,
        )
        if active >= MAX_ACTIVE_SCHEDULES_PER_SCOPE:
            raise ValueError(
                "Too many active schedules in this conversation "
                f"(max {MAX_ACTIVE_SCHEDULES_PER_SCOPE})"
            )
        return await catalog.create(definition, created_at=now)

    async def replace(
        self,
        schedule_id: int,
        definition: ScheduleDefinition,
        *,
        catalog: ScheduleCatalog,
    ) -> ScheduledAssistantTurn | None:
        """@brief 完整替换尚未领取的 schedule / Fully replace an unclaimed schedule.

        @param schedule_id 当前 schedule identity / Current schedule identity.
        @param definition 完整替代定义 / Complete replacement definition.
        @param catalog 当前 mutation UoW 绑定的 catalog / Catalog bound to the current mutation UoW.
        @return 更新后 schedule；不存在或正在处理时为 None / Updated schedule, or None when absent or processing.
        """

        if isinstance(schedule_id, bool) or schedule_id <= 0:
            raise ValueError("schedule_id must be positive")
        now = self._clock.now()
        if definition.first_run_at <= now:
            raise ValueError("Schedule first occurrence must be in the future")
        conversation_id = str(definition.target.conversation_id)
        await catalog.lock_scope(definition.creator_user_id, conversation_id)
        return await catalog.replace(
            schedule_id,
            definition,
            updated_at=now,
        )

    async def list(
        self,
        *,
        creator_user_id: int,
        conversation_id: str,
        limit: int,
        catalog: ScheduleCatalog,
    ) -> tuple[ScheduleSnapshot, ...]:
        """@brief 列出当前 creator-target scope / List the current creator-target scope.

        @return schedule snapshots / Schedule snapshots.
        """

        if not 1 <= limit <= MAX_LISTED_SCHEDULES:
            raise ValueError(
                f"Schedule list limit must be between 1 and {MAX_LISTED_SCHEDULES}"
            )
        return tuple(
            await catalog.list(
                creator_user_id=creator_user_id,
                conversation_id=conversation_id,
                limit=limit,
            )
        )

    async def cancel(
        self,
        *,
        schedule_id: int,
        creator_user_id: int,
        conversation_id: str,
        catalog: ScheduleCatalog,
    ) -> bool:
        """@brief 取消当前 scope 内 schedule / Cancel a schedule within the current scope.

        @return 本次是否完成取消 / Whether this call cancelled the schedule.
        """

        if isinstance(schedule_id, bool) or schedule_id <= 0:
            raise ValueError("schedule_id must be positive")
        await catalog.lock_scope(creator_user_id, conversation_id)
        return await catalog.cancel(
            schedule_id=schedule_id,
            creator_user_id=creator_user_id,
            conversation_id=conversation_id,
            cancelled_at=self._clock.now(),
        )


__all__ = [
    "MAX_ACTIVE_SCHEDULES_PER_SCOPE",
    "MAX_LISTED_SCHEDULES",
    "SchedulingService",
]
