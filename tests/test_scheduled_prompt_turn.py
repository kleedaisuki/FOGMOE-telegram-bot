"""@brief Scheduled Assistant occurrence 纯构造测试 / Pure-construction tests for scheduled Assistant occurrences."""

from datetime import UTC, datetime, timedelta

from fogmoe_bot.application.assistant.inference_command import (
    DurableAssistantInferenceCommand,
    DurableAssistantUser,
    DurableUserProfile,
)
from fogmoe_bot.application.scheduling.occurrence import (
    SCHEDULED_READ_TOOL_NAMES,
    occurrence_key,
    prepare_scheduled_occurrence,
)
from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.domain.conversation.identity import ConversationId, DeliveryStreamId
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    OneShot,
    ScheduledAssistantTurn,
    ScheduleTarget,
)
from fogmoe_bot.domain.temporal import TimeZoneId

CREATED_AT = datetime(2026, 7, 10, 8, tzinfo=UTC)
"""@brief 测试 schedule 创建时刻 / Test schedule creation instant."""

RUN_AT = datetime(2026, 7, 11, 11, 30, 0, 123456, tzinfo=UTC)
"""@brief 测试计划发生时刻 / Test planned occurrence instant."""

OBSERVED_AT = RUN_AT + timedelta(seconds=9)
"""@brief worker 实际观察时刻 / Actual worker observation instant."""


def _profile() -> DurableUserProfile:
    """@brief 构造可识别的私有 Profile / Build a recognizable private profile.

    @return 测试 Profile / Test profile.
    """

    return DurableUserProfile(
        revision=3,
        observed_through_event_id=71,
        prompt_version=2,
        route_key="profile:test:klee",
        created_at=CREATED_AT,
        updated_at=CREATED_AT + timedelta(minutes=1),
        claims=(),
    )


def _user() -> DurableAssistantUser:
    """@brief 构造携带私有上下文的用户 / Build a user carrying private context.

    @return acceptance-time 用户快照 / Acceptance-time user snapshot.
    """

    return DurableAssistantUser(
        user_id=42,
        username="klee",
        display_name="Klee",
        coins=19,
        plan=AccountPlan.PAID,
        permission=1,
        profile=_profile(),
        personal_info="CS PhD student",
        diary_exists=True,
    )


def _target(*, group: bool) -> ScheduleTarget:
    """@brief 构造私聊或群 Topic 目标 / Build a private or group-topic target.

    @param group 是否为群目标 / Whether to build a group target.
    @return 完整的冻结投递目标 / Complete frozen delivery target.
    """

    if group:
        return ScheduleTarget(
            conversation_id=ConversationId("assistant-group:-1001:thread:23"),
            delivery_stream_id=DeliveryStreamId(
                "telegram:primary:chat:-1001:thread:23"
            ),
            chat_id=-1001,
            is_group=True,
            message_thread_id=23,
        )
    return ScheduleTarget(
        conversation_id=ConversationId("assistant-user:42"),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
        chat_id=42,
        is_group=False,
    )


def _schedule(*, group: bool, schedule_id: int = 7) -> ScheduledAssistantTurn:
    """@brief 构造定时 Assistant 回合 / Build a scheduled Assistant turn.

    @param group 是否为群目标 / Whether the target is a group.
    @param schedule_id 不复用的 schedule ID / Never-reused schedule ID.
    @return 领域 schedule / Domain schedule.
    """

    return ScheduledAssistantTurn(
        schedule_id=schedule_id,
        creator_user_id=42,
        target=_target(group=group),
        trigger_reason="timer",
        context_snapshot="previous context",
        instruction="Send the reminder",
        cadence=OneShot(),
        next_run_at=RUN_AT,
        created_at=CREATED_AT,
        time_zone=TimeZoneId("Asia/Shanghai"),
    )


def _command(
    schedule: ScheduledAssistantTurn,
) -> DurableAssistantInferenceCommand:
    """@brief 构造 occurrence 并解析 durable command / Prepare an occurrence and parse its durable command.

    @param schedule 计划定义 / Schedule definition.
    @return 严格 durable Assistant command / Strict durable Assistant command.
    """

    prepared = prepare_scheduled_occurrence(
        schedule,
        user=_user(),
        observed_at=OBSERVED_AT,
    )
    return DurableAssistantInferenceCommand.from_json(prepared.activity.request)


def test_occurrence_identity_is_deterministic_and_scoped_by_planned_instant() -> None:
    """@brief 重放同一 occurrence 收敛而不同时刻分离 / Replays converge while distinct planned instants remain separate."""

    schedule = _schedule(group=False)
    first = prepare_scheduled_occurrence(
        schedule,
        user=_user(),
        observed_at=OBSERVED_AT,
    )
    replay = prepare_scheduled_occurrence(
        schedule,
        user=_user(),
        observed_at=OBSERVED_AT + timedelta(seconds=5),
    )
    later_schedule = ScheduledAssistantTurn(
        schedule_id=schedule.schedule_id,
        creator_user_id=schedule.creator_user_id,
        target=schedule.target,
        trigger_reason=schedule.trigger_reason,
        instruction=schedule.instruction,
        cadence=schedule.cadence,
        next_run_at=RUN_AT + timedelta(hours=1),
        created_at=schedule.created_at,
        time_zone=schedule.time_zone,
        context_snapshot=schedule.context_snapshot,
    )
    later = prepare_scheduled_occurrence(
        later_schedule,
        user=_user(),
        observed_at=OBSERVED_AT + timedelta(hours=1),
    )

    assert occurrence_key(7, RUN_AT) == "7:2026-07-11T11:30:00.123456Z"
    assert first.turn.source.kind == "schedule.prompt"
    assert first.turn.source.key == occurrence_key(7, RUN_AT)
    assert first.turn.turn_id == replay.turn.turn_id
    assert first.message.message_id == replay.message.message_id
    assert first.activity.activity_id == replay.activity.activity_id
    assert first.turn.turn_id != later.turn.turn_id


def test_private_occurrence_preserves_target_and_private_profile() -> None:
    """@brief 私聊 occurrence 保留完整目标与用户快照 / A private occurrence preserves its exact target and user snapshot."""

    schedule = _schedule(group=False)
    prepared = prepare_scheduled_occurrence(
        schedule,
        user=_user(),
        observed_at=OBSERVED_AT,
    )
    command = DurableAssistantInferenceCommand.from_json(prepared.activity.request)

    assert prepared.turn.conversation_id == schedule.target.conversation_id
    assert command.conversation_id == "assistant-user:42"
    assert command.delivery_stream_id == "telegram:primary:chat:42:thread:0"
    assert command.chat_id == 42
    assert command.message_thread_id is None
    assert command.scope.is_group is False
    assert command.scope.group_id is None
    assert command.user == _user()
    assert command.allow_tools is True
    assert command.allowed_tools == SCHEDULED_READ_TOOL_NAMES
    assert prepared.message.content["content_kind"] == "scheduled_prompt"
    assert prepared.message.content["source"] == {
        "kind": "schedule.prompt",
        "schedule_id": 7,
        "scheduled_for": "2026-07-11T11:30:00Z",
    }
    assert "Send the reminder" in str(prepared.message.content["text"])


def test_group_occurrence_preserves_topic_but_removes_private_context() -> None:
    """@brief 群 occurrence 保留 Topic 并清除三类私有上下文 / A group occurrence preserves its topic and clears all three private-context fields."""

    schedule = _schedule(group=True)
    command = _command(schedule)

    assert command.conversation_id == "assistant-group:-1001:thread:23"
    assert command.delivery_stream_id == "telegram:primary:chat:-1001:thread:23"
    assert command.chat_id == -1001
    assert command.message_thread_id == 23
    assert command.scope.is_group is True
    assert command.scope.group_id == -1001
    assert command.scope.message_thread_id == 23
    assert command.user.profile is None
    assert command.user.personal_info == ""
    assert command.user.diary_exists is False
    assert command.allow_tools is True
    assert command.allowed_tools == SCHEDULED_READ_TOOL_NAMES
