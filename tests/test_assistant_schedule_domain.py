"""@brief 助手定时回合新领域模型测试 / Tests for the new assistant-turn scheduling domain model."""

from datetime import UTC, datetime, time, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from fogmoe_bot.domain.conversation.identity import ConversationId, DeliveryStreamId
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    CalendarDaily,
    CalendarWeekly,
    FixedInterval,
    MisfirePolicy,
    OneShot,
    ScheduleClaim,
    ScheduleSnapshot,
    ScheduleStatus,
    ScheduleTarget,
    ScheduledAssistantTurn,
    StaleScheduleClaimError,
    next_occurrence,
)
from fogmoe_bot.domain.temporal import TemporalValueError, TimeZoneId


def _target(*, group: bool = False, thread_id: int | None = None) -> ScheduleTarget:
    """@brief 构造稳定测试目标 / Build a stable target for tests.

    @param group 是否构造群聊目标 / Whether to build a group target.
    @param thread_id 可选 forum topic 标识 / Optional forum-topic identifier.
    @return 已校验目标 / Validated target.
    """

    chat_id = -100_123 if group else 123
    scope = f"telegram:{chat_id}:{thread_id or 0}"
    return ScheduleTarget(
        conversation_id=ConversationId(scope),
        delivery_stream_id=DeliveryStreamId(scope),
        chat_id=chat_id,
        is_group=group,
        message_thread_id=thread_id,
    )


def _one_shot_schedule(
    *,
    next_run_at: datetime | None = None,
    created_at: datetime | None = None,
) -> ScheduledAssistantTurn:
    """@brief 构造有效的一次性计划 / Build a valid one-shot schedule.

    @param next_run_at 可选计划发生项 / Optional scheduled occurrence.
    @param created_at 可选创建瞬间 / Optional creation instant.
    @return 已校验计划 / Validated schedule.
    """

    created = created_at or datetime(2030, 1, 1, tzinfo=UTC)
    occurrence = next_run_at or created + timedelta(hours=1)
    return ScheduledAssistantTurn(
        schedule_id=7,
        creator_user_id=42,
        target=_target(),
        trigger_reason="assistant.schedule.due",
        instruction="Give Klee a concise status update.",
        cadence=OneShot(),
        next_run_at=occurrence,
        created_at=created,
        time_zone=TimeZoneId("Asia/Shanghai"),
    )


def test_schedule_target_preserves_delivery_scope_and_group_identity() -> None:
    """@brief group_id 是显式目标的派生视图 / group_id is a derived view of the explicit target."""

    group = _target(group=True, thread_id=19)
    private = _target()

    assert group.group_id == -100_123
    assert group.message_thread_id == 19
    assert private.group_id is None

    with pytest.raises(ValueError, match="only valid for group"):
        _target(thread_id=19)
    with pytest.raises(ValueError, match="positive"):
        _target(group=True, thread_id=0)
    with pytest.raises(ValueError, match="cannot be zero"):
        ScheduleTarget(ConversationId("c"), DeliveryStreamId("d"), 0, False)
    with pytest.raises(ValueError, match="Group chat_id must be negative"):
        ScheduleTarget(ConversationId("c"), DeliveryStreamId("d"), 123, True)
    with pytest.raises(ValueError, match="Private chat_id must be positive"):
        ScheduleTarget(ConversationId("c"), DeliveryStreamId("d"), -123, False)


def test_one_shot_has_exactly_one_unconsumed_occurrence() -> None:
    """@brief 严格下界避免同一 one-shot 被再次接受 / The exclusive bound prevents reaccepting a one-shot."""

    occurrence = datetime(2030, 1, 1, 12, tzinfo=UTC)
    cadence = OneShot()

    assert (
        next_occurrence(
            cadence,
            current=occurrence,
            after=occurrence - timedelta(microseconds=1),
        )
        == occurrence
    )
    assert next_occurrence(cadence, current=occurrence, after=occurrence) is None


def test_fixed_interval_skips_a_century_with_one_arithmetic_jump() -> None:
    """@brief fixed cadence 的结果由整除直接给出 / Integer division directly determines a fixed-cadence jump."""

    cadence = FixedInterval(timedelta(minutes=17))
    current = datetime(2000, 1, 1, tzinfo=UTC)
    after = datetime(2100, 1, 1, tzinfo=UTC)
    expected_steps = (after - current) // cadence.every + 1

    assert cadence.next_after(current=current, after=after) == (
        current + cadence.every * expected_steps
    )


def test_fixed_duration_and_calendar_daily_diverge_across_dst() -> None:
    """@brief 24h 与本地每天 09:00 在 DST 切换后语义不同 / Fixed 24h and local 09:00 daily diverge after a DST transition."""

    new_york = TimeZoneId("America/New_York")
    current = datetime(2026, 3, 7, 14, tzinfo=UTC)  # 09:00 EST

    fixed_next = FixedInterval(timedelta(days=1)).next_after(
        current=current,
        after=current,
    )
    calendar_next = CalendarDaily(time(9), new_york).next_after(
        current=current,
        after=current,
    )

    assert fixed_next == datetime(2026, 3, 8, 14, tzinfo=UTC)
    assert new_york.localize(fixed_next).hour == 10
    assert calendar_next == datetime(2026, 3, 8, 13, tzinfo=UTC)
    assert new_york.localize(calendar_next).hour == 9


def test_calendar_daily_uses_stable_gap_and_overlap_policy() -> None:
    """@brief daily cadence 继承 TimeZoneId 的 gap/fold 决策 / Daily cadence inherits TimeZoneId's gap/fold decisions."""

    new_york = TimeZoneId("America/New_York")
    gap_rule = CalendarDaily(time(2, 30), new_york)
    gap_previous = datetime(2026, 3, 7, 7, 30, tzinfo=UTC)
    gap = gap_rule.next_after(current=gap_previous, after=gap_previous)

    fold_rule = CalendarDaily(time(1, 30), new_york)
    fold_previous = datetime(2026, 10, 31, 5, 30, tzinfo=UTC)
    fold = fold_rule.next_after(current=fold_previous, after=fold_previous)

    assert new_york.localize(gap).isoformat() == "2026-03-08T03:30:00-04:00"
    assert gap == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
    assert fold == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert new_york.localize(fold).fold == 0


def test_calendar_weekly_checks_only_active_weeks() -> None:
    """@brief biweekly 规则在同周与跨 inactive week 时都保持锚点 / A biweekly rule preserves its anchor within and across inactive weeks."""

    new_york = TimeZoneId("America/New_York")
    cadence = CalendarWeekly(
        local_time=time(9),
        time_zone=new_york,
        weekdays=frozenset({1, 3}),
        interval=2,
    )
    monday = datetime(2026, 1, 5, 14, tzinfo=UTC)
    wednesday = datetime(2026, 1, 7, 14, tzinfo=UTC)

    assert cadence.next_after(current=monday, after=monday) == wednesday
    assert cadence.next_after(current=monday, after=wednesday) == datetime(
        2026,
        1,
        19,
        14,
        tzinfo=UTC,
    )


def test_calendar_cadences_reject_ambiguous_rules_and_corrupt_cursors() -> None:
    """@brief calendar 配置与 cursor 不一致会在领域边界失败 / Misaligned calendar rules and cursors fail at the domain boundary."""

    zone = TimeZoneId("UTC")
    with pytest.raises(ValueError, match="timezone-naive"):
        CalendarDaily(time(9, tzinfo=UTC), zone)
    with pytest.raises(ValueError, match="positive"):
        CalendarDaily(time(9), zone, interval=0)
    with pytest.raises(ValueError, match="cannot be empty"):
        CalendarWeekly(time(9), zone, frozenset())
    with pytest.raises(ValueError, match="ISO values"):
        CalendarWeekly(time(9), zone, frozenset({0, 8}))
    with pytest.raises(TypeError, match="integers"):
        CalendarWeekly(time(9), zone, frozenset({True}))

    weekly = CalendarWeekly(time(9), zone, frozenset({1, 3}))
    tuesday = datetime(2026, 1, 6, 9, tzinfo=UTC)
    with pytest.raises(ValueError, match="configured weekdays"):
        weekly.next_after(current=tuesday, after=tuesday)

    daily = CalendarDaily(time(9), zone)
    misaligned = datetime(2026, 1, 5, 9, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="not aligned"):
        daily.next_after(current=misaligned, after=misaligned)


@pytest.mark.parametrize("naive_field", ["current", "after"])
def test_recurrence_algorithms_reject_naive_timestamps(naive_field: str) -> None:
    """@brief recurrence 边界从不猜测 naive 时间 / Recurrence boundaries never guess a naive timestamp's meaning."""

    values = {
        "current": datetime(2030, 1, 1, tzinfo=UTC),
        "after": datetime(2030, 1, 2, tzinfo=UTC),
    }
    values[naive_field] = values[naive_field].replace(tzinfo=None)

    with pytest.raises(TemporalValueError, match="timezone-aware"):
        next_occurrence(
            FixedInterval(timedelta(hours=1)),
            current=values["current"],
            after=values["after"],
        )


def test_scheduled_turn_normalizes_text_and_aware_timestamps_to_utc() -> None:
    """@brief 聚合保留瞬间语义而不保留偶然 offset / The aggregate preserves instants rather than incidental offsets."""

    plus_eight = timezone(timedelta(hours=8))
    schedule = ScheduledAssistantTurn(
        schedule_id=1,
        creator_user_id=2,
        target=_target(),
        trigger_reason="  daily brief  ",
        instruction="  Summarize today's work.  ",
        cadence=OneShot(),
        next_run_at=datetime(2030, 1, 2, 9, tzinfo=plus_eight),
        created_at=datetime(2030, 1, 1, 9, tzinfo=plus_eight),
        time_zone=TimeZoneId("Asia/Shanghai"),
        context_snapshot="   ",
        misfire_policy=MisfirePolicy.SKIP,
        misfire_grace=timedelta(minutes=15),
    )

    assert schedule.trigger_reason == "daily brief"
    assert schedule.instruction == "Summarize today's work."
    assert schedule.context_snapshot is None
    assert schedule.next_run_at == datetime(2030, 1, 2, 1, tzinfo=UTC)
    assert schedule.created_at == datetime(2030, 1, 1, 1, tzinfo=UTC)
    assert schedule.next_occurrence(after=schedule.next_run_at) is None


def test_scheduled_turn_enforces_chronology_calendar_zone_and_alignment() -> None:
    """@brief 聚合不会容纳不能由 cadence 重建的 next_run_at / The aggregate rejects next_run_at values its cadence cannot reconstruct."""

    new_york = TimeZoneId("America/New_York")
    created = datetime(2026, 3, 1, tzinfo=UTC)
    valid_values = {
        "schedule_id": 1,
        "creator_user_id": 2,
        "target": _target(),
        "trigger_reason": "daily",
        "instruction": "Run the daily brief.",
        "cadence": CalendarDaily(time(9), new_york),
        "next_run_at": datetime(2026, 3, 7, 14, tzinfo=UTC),
        "created_at": created,
        "time_zone": new_york,
    }
    valid = ScheduledAssistantTurn(**valid_values)
    assert valid.next_run_at == datetime(2026, 3, 7, 14, tzinfo=UTC)

    with pytest.raises(ValueError, match="same time zone"):
        ScheduledAssistantTurn(**(valid_values | {"time_zone": TimeZoneId("UTC")}))
    with pytest.raises(ValueError, match="not aligned"):
        ScheduledAssistantTurn(
            **(valid_values | {"next_run_at": datetime(2026, 3, 7, 14, 1, tzinfo=UTC)})
        )
    with pytest.raises(ValueError, match="earlier than created_at"):
        _one_shot_schedule(
            next_run_at=created, created_at=created + timedelta(seconds=1)
        )
    with pytest.raises(ValueError, match="positive"):
        ScheduledAssistantTurn(**(valid_values | {"schedule_id": 0}))
    with pytest.raises(ValueError, match="misfire_grace"):
        ScheduledAssistantTurn(**(valid_values | {"misfire_grace": timedelta()}))
    with pytest.raises(ValueError, match="requires misfire_grace"):
        ScheduledAssistantTurn(
            **(valid_values | {"misfire_policy": MisfirePolicy.SKIP})
        )


def test_schedule_claim_requires_due_schedule_non_nil_token_and_live_lease() -> None:
    """@brief claim 把 due 条件与 fencing lease 固化为值 / A claim makes due and fencing-lease conditions explicit values."""

    schedule = _one_shot_schedule()
    plus_eight = timezone(timedelta(hours=8))
    claimed_at = schedule.next_run_at.astimezone(plus_eight)
    claim = ScheduleClaim(
        schedule=schedule,
        attempt_count=1,
        token=uuid4(),
        claimed_at=claimed_at,
        lease_expires_at=claimed_at + timedelta(minutes=1),
    )

    assert claim.claimed_at == schedule.next_run_at
    assert claim.lease_expires_at.tzinfo is UTC

    with pytest.raises(ValueError, match="nil UUID"):
        ScheduleClaim(
            schedule, 1, UUID(int=0), claimed_at, claimed_at + timedelta(minutes=1)
        )
    with pytest.raises(ValueError, match="attempt_count"):
        ScheduleClaim(
            schedule, 0, uuid4(), claimed_at, claimed_at + timedelta(minutes=1)
        )
    with pytest.raises(ValueError, match="before it is due"):
        ScheduleClaim(
            schedule,
            1,
            uuid4(),
            claimed_at - timedelta(microseconds=1),
            claimed_at + timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="expire after"):
        ScheduleClaim(schedule, 1, uuid4(), claimed_at, claimed_at)
    with pytest.raises(TemporalValueError, match="timezone-aware"):
        ScheduleClaim(
            schedule,
            1,
            uuid4(),
            claimed_at.replace(tzinfo=None),
            claimed_at + timedelta(minutes=1),
        )


def test_schedule_status_has_the_exact_persisted_state_vocabulary() -> None:
    """@brief 状态机词汇是封闭且可持久化的 / The state-machine vocabulary is closed and persistable."""

    assert {status.value for status in ScheduleStatus} == {
        "pending",
        "processing",
        "retry_wait",
        "completed",
        "cancelled",
        "expired",
        "failed_final",
    }
    assert issubclass(StaleScheduleClaimError, RuntimeError)


def test_schedule_snapshot_normalizes_acceptance_and_terminal_state() -> None:
    """@brief 查询快照同时表达 occurrence 与实际接受瞬间 / A query snapshot exposes both occurrence and actual acceptance instants."""

    schedule = _one_shot_schedule()
    plus_eight = timezone(timedelta(hours=8))
    accepted_for = schedule.next_run_at.astimezone(plus_eight)
    accepted_at = accepted_for + timedelta(seconds=2)
    terminal_at = accepted_at + timedelta(seconds=3)
    snapshot = ScheduleSnapshot(
        schedule=schedule,
        status=ScheduleStatus.COMPLETED,
        attempt_count=1,
        last_accepted_for=accepted_for,
        last_accepted_at=accepted_at,
        last_error="   ",
        terminal_at=terminal_at,
    )

    assert snapshot.last_accepted_for == schedule.next_run_at
    assert snapshot.last_accepted_at == schedule.next_run_at + timedelta(seconds=2)
    assert snapshot.last_error is None
    assert snapshot.terminal_at == schedule.next_run_at + timedelta(seconds=5)


def test_schedule_snapshot_rejects_partial_acceptance_and_terminal_mismatch() -> None:
    """@brief snapshot 不允许半组 acceptance 字段或伪终态 / A snapshot rejects half-present acceptance fields and false terminal states."""

    schedule = _one_shot_schedule()
    with pytest.raises(ValueError, match="present together"):
        ScheduleSnapshot(
            schedule,
            ScheduleStatus.PROCESSING,
            1,
            schedule.next_run_at,
            None,
            None,
            None,
        )
    with pytest.raises(ValueError, match="exactly for terminal"):
        ScheduleSnapshot(
            schedule,
            ScheduleStatus.COMPLETED,
            1,
            schedule.next_run_at,
            schedule.next_run_at,
            None,
            None,
        )
    with pytest.raises(ValueError, match="exactly for terminal"):
        ScheduleSnapshot(
            schedule,
            ScheduleStatus.PENDING,
            0,
            None,
            None,
            None,
            schedule.created_at,
        )
    with pytest.raises(ValueError, match="cannot be negative"):
        ScheduleSnapshot(
            schedule,
            ScheduleStatus.PENDING,
            -1,
            None,
            None,
            None,
            None,
        )
