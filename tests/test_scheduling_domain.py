"""@brief Scheduled Assistant 应用服务测试 / Scheduled Assistant application-service tests."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from fogmoe_bot.application.scheduling.assistant_ports import ScheduleDefinition
from fogmoe_bot.application.scheduling.service import (
    MAX_ACTIVE_SCHEDULES_PER_SCOPE,
    MAX_LISTED_SCHEDULES,
    SchedulingService,
)
from fogmoe_bot.domain.conversation.identity import ConversationId, DeliveryStreamId
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    FixedInterval,
    ScheduleSnapshot,
    ScheduleStatus,
    ScheduleTarget,
    ScheduledAssistantTurn,
)
from fogmoe_bot.domain.temporal import TimeZoneId


NOW = datetime(2026, 7, 22, 8, tzinfo=UTC)
"""@brief 应用服务的固定当前时刻 / Fixed current instant for application-service tests."""


class _FixedClock:
    """@brief 返回固定 UTC 时刻的时钟 / Clock returning a fixed UTC instant."""

    def now(self) -> datetime:
        """@brief 返回 NOW / Return NOW.

        @return 固定 UTC 时刻 / Fixed UTC instant.
        """

        return NOW


def _target() -> ScheduleTarget:
    """@brief 构造私聊调度目标 / Build a private scheduling target.

    @return 完整目标 / Complete target.
    """

    return ScheduleTarget(
        conversation_id=ConversationId("assistant-user:42"),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
        chat_id=42,
        is_group=False,
    )


def _definition(*, first_run_at: datetime | None = None) -> ScheduleDefinition:
    """@brief 构造完整替换式 schedule 定义 / Build a complete replacement-style schedule definition.

    @param first_run_at 可选首次 occurrence / Optional first occurrence.
    @return 调度定义 / Schedule definition.
    """

    return ScheduleDefinition(
        creator_user_id=42,
        target=_target(),
        trigger_reason="status timer",
        context_snapshot="created from chat",
        instruction="Send a status update",
        cadence=FixedInterval(timedelta(hours=2)),
        first_run_at=first_run_at or NOW + timedelta(hours=1),
        time_zone=TimeZoneId("Asia/Shanghai"),
    )


def _stored(
    definition: ScheduleDefinition,
    *,
    schedule_id: int,
    created_at: datetime,
) -> ScheduledAssistantTurn:
    """@brief 把定义投影为已持久化聚合 / Project a definition into a persisted aggregate.

    @param definition 应用定义 / Application definition.
    @param schedule_id 持久化 ID / Persisted ID.
    @param created_at 创建时刻 / Creation instant.
    @return 领域聚合 / Domain aggregate.
    """

    return ScheduledAssistantTurn(
        schedule_id=schedule_id,
        creator_user_id=definition.creator_user_id,
        target=definition.target,
        trigger_reason=definition.trigger_reason,
        context_snapshot=definition.context_snapshot,
        instruction=definition.instruction,
        cadence=definition.cadence,
        next_run_at=definition.first_run_at,
        created_at=created_at,
        time_zone=definition.time_zone,
        misfire_policy=definition.misfire_policy,
        misfire_grace=definition.misfire_grace,
    )


class _Catalog:
    """@brief 记录调用顺序的 catalog 替身 / Catalog double recording call order."""

    def __init__(self) -> None:
        """@brief 初始化可控结果与调用日志 / Initialize controllable results and call log."""

        self.active_count = 0
        self.replace_result: ScheduledAssistantTurn | None = None
        self.list_result: Sequence[ScheduleSnapshot] = ()
        self.cancel_result = True
        self.events: list[tuple[object, ...]] = []

    async def lock_scope(self, creator_user_id: int, conversation_id: str) -> None:
        """@brief 记录 scope 锁 / Record a scope lock."""

        self.events.append(("lock", creator_user_id, conversation_id))

    async def count_active(self, creator_user_id: int, conversation_id: str) -> int:
        """@brief 记录并返回 active 计数 / Record and return the active count."""

        self.events.append(("count", creator_user_id, conversation_id))
        return self.active_count

    async def create(
        self,
        definition: ScheduleDefinition,
        *,
        created_at: datetime,
    ) -> ScheduledAssistantTurn:
        """@brief 记录创建并分配新 ID / Record creation and allocate a new ID."""

        self.events.append(("create", definition, created_at))
        return _stored(definition, schedule_id=101, created_at=created_at)

    async def replace(
        self,
        schedule_id: int,
        definition: ScheduleDefinition,
        *,
        updated_at: datetime,
    ) -> ScheduledAssistantTurn | None:
        """@brief 记录完整替换 / Record a complete replacement."""

        self.events.append(("replace", schedule_id, definition, updated_at))
        if self.replace_result is not None:
            return self.replace_result
        return _stored(definition, schedule_id=schedule_id, created_at=updated_at)

    async def list(
        self,
        *,
        creator_user_id: int,
        conversation_id: str,
        limit: int,
    ) -> Sequence[ScheduleSnapshot]:
        """@brief 记录 scope 查询 / Record a scoped query."""

        self.events.append(("list", creator_user_id, conversation_id, limit))
        return self.list_result

    async def cancel(
        self,
        *,
        schedule_id: int,
        creator_user_id: int,
        conversation_id: str,
        cancelled_at: datetime,
    ) -> bool:
        """@brief 记录 scope 内取消 / Record cancellation within a scope."""

        self.events.append(
            (
                "cancel",
                schedule_id,
                creator_user_id,
                conversation_id,
                cancelled_at,
            )
        )
        return self.cancel_result


def _snapshot(schedule: ScheduledAssistantTurn) -> ScheduleSnapshot:
    """@brief 构造 pending 查询快照 / Build a pending query snapshot.

    @param schedule 聚合 / Schedule aggregate.
    @return pending snapshot / Pending snapshot.
    """

    return ScheduleSnapshot(
        schedule=schedule,
        status=ScheduleStatus.PENDING,
        attempt_count=0,
        last_accepted_for=None,
        last_accepted_at=None,
        last_error=None,
        terminal_at=None,
    )


def test_create_locks_scope_checks_quota_and_persists_with_clock_time() -> None:
    """@brief create 在同一 scope 锁内检查配额并持久化 / Create checks quota and persists while holding the same scope lock."""

    async def scenario() -> None:
        """@brief 执行创建用例 / Exercise the create use case."""

        catalog = _Catalog()
        definition = _definition()
        created = await SchedulingService(clock=_FixedClock()).create(
            definition,
            catalog=catalog,
        )

        assert created.schedule_id == 101
        assert created.created_at == NOW
        assert catalog.events == [
            ("lock", 42, "assistant-user:42"),
            ("count", 42, "assistant-user:42"),
            ("create", definition, NOW),
        ]

    asyncio.run(scenario())


def test_create_rejects_nonfuture_definition_before_locking_and_enforces_quota() -> (
    None
):
    """@brief 非未来发生项无 I/O 失败，配额检查则在 scope 锁内 / A nonfuture occurrence fails without I/O while quota is checked under the scope lock."""

    async def scenario() -> None:
        """@brief 覆盖时间与配额失败 / Cover temporal and quota failures."""

        service = SchedulingService(clock=_FixedClock())
        past_catalog = _Catalog()
        with pytest.raises(ValueError, match="future"):
            await service.create(
                _definition(first_run_at=NOW),
                catalog=past_catalog,
            )
        assert past_catalog.events == []

        full_catalog = _Catalog()
        full_catalog.active_count = MAX_ACTIVE_SCHEDULES_PER_SCOPE
        with pytest.raises(ValueError, match="Too many active schedules"):
            await service.create(_definition(), catalog=full_catalog)
        assert full_catalog.events == [
            ("lock", 42, "assistant-user:42"),
            ("count", 42, "assistant-user:42"),
        ]

    asyncio.run(scenario())


def test_replace_is_a_locked_complete_replacement_and_requires_future_cursor() -> None:
    """@brief replace 保留 ID、完整替换定义并拒绝过期 cursor / Replace preserves identity, fully replaces the definition, and rejects an expired cursor."""

    async def scenario() -> None:
        """@brief 执行 replace 语义 / Exercise replacement semantics."""

        service = SchedulingService(clock=_FixedClock())
        catalog = _Catalog()
        definition = _definition(first_run_at=NOW + timedelta(days=1))
        replaced = await service.replace(55, definition, catalog=catalog)

        assert replaced is not None
        assert replaced.schedule_id == 55
        assert replaced.instruction == definition.instruction
        assert catalog.events == [
            ("lock", 42, "assistant-user:42"),
            ("replace", 55, definition, NOW),
        ]

        untouched = _Catalog()
        with pytest.raises(ValueError, match="future"):
            await service.replace(
                55,
                _definition(first_run_at=NOW),
                catalog=untouched,
            )
        assert untouched.events == []

    asyncio.run(scenario())


def test_list_and_cancel_remain_within_creator_conversation_scope() -> None:
    """@brief list/cancel 不能越过 creator-conversation 边界 / List and cancel cannot cross the creator-conversation boundary."""

    async def scenario() -> None:
        """@brief 执行查询与取消用例 / Exercise query and cancellation use cases."""

        service = SchedulingService(clock=_FixedClock())
        catalog = _Catalog()
        stored = _stored(_definition(), schedule_id=9, created_at=NOW)
        catalog.list_result = (_snapshot(stored),)

        listed = await service.list(
            creator_user_id=42,
            conversation_id="assistant-user:42",
            limit=10,
            catalog=catalog,
        )
        cancelled = await service.cancel(
            schedule_id=9,
            creator_user_id=42,
            conversation_id="assistant-user:42",
            catalog=catalog,
        )

        assert listed == (_snapshot(stored),)
        assert cancelled is True
        assert catalog.events == [
            ("list", 42, "assistant-user:42", 10),
            ("lock", 42, "assistant-user:42"),
            ("cancel", 9, 42, "assistant-user:42", NOW),
        ]

        with pytest.raises(ValueError, match=f"between 1 and {MAX_LISTED_SCHEDULES}"):
            await service.list(
                creator_user_id=42,
                conversation_id="assistant-user:42",
                limit=MAX_LISTED_SCHEDULES + 1,
                catalog=catalog,
            )

    asyncio.run(scenario())
