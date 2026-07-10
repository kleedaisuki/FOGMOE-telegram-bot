"""@brief 后台调度领域测试 / Background-scheduling domain tests."""

from datetime import datetime, timezone

import pytest

from fogmoe_bot.domain.scheduling import Recurrence, RecurrenceUnit, ensure_utc


def test_recurrence_skips_missed_intervals_without_looping() -> None:
    """@brief 周期规则直接跳到未来一次 / Recurrence jumps directly to the next future occurrence."""
    recurrence = Recurrence(RecurrenceUnit.MINUTE, 5)
    previous = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, 0, 17, tzinfo=timezone.utc)

    assert recurrence.next_after(previous, now) == datetime(
        2026,
        1,
        1,
        0,
        20,
        tzinfo=timezone.utc,
    )


def test_one_shot_recurrence_has_no_next_occurrence() -> None:
    """@brief 一次性任务没有下一次运行 / One-shot jobs have no next occurrence."""
    recurrence = Recurrence()
    now = datetime.now(timezone.utc)

    assert recurrence.next_after(now, now) is None


def test_recurrence_rejects_non_positive_interval() -> None:
    """@brief 非正周期间隔被领域不变量拒绝 / Domain invariants reject non-positive intervals."""
    with pytest.raises(ValueError, match="at least one"):
        Recurrence(RecurrenceUnit.HOUR, 0)


def test_legacy_naive_datetime_is_interpreted_as_utc() -> None:
    """@brief 旧 naive 时间按 UTC 兼容 / Legacy naive datetimes retain the UTC convention."""
    value = datetime(2026, 1, 1, 0, 0)

    assert ensure_utc(value).tzinfo is timezone.utc
