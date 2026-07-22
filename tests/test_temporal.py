"""@brief 跨领域时间值测试 / Tests for cross-domain temporal values."""

from datetime import UTC, datetime, timedelta

import pytest

from fogmoe_bot.domain.temporal import (
    AmbiguousLocalTimeError,
    NonexistentLocalTimeError,
    TemporalValueError,
    TimeZoneId,
    UtcInterval,
    ensure_utc,
)


def test_utc_invariant_rejects_naive_values() -> None:
    """@brief Domain 不再猜测 naive 时间 / The domain no longer guesses the meaning of naive time."""

    with pytest.raises(TemporalValueError, match="timezone-aware"):
        ensure_utc(datetime(2030, 1, 1))


def test_half_open_interval_includes_only_its_lower_boundary() -> None:
    """@brief 相邻区间不会重复命中共同端点 / Adjacent intervals do not both contain their shared boundary."""

    start = datetime(2030, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    interval = UtcInterval(start, end)

    assert interval.contains(start)
    assert interval.contains(end - timedelta(microseconds=1))
    assert not interval.contains(end)
    with pytest.raises(TemporalValueError, match="later than start"):
        UtcInterval(end, end)


def test_iana_zone_strictly_rejects_dst_gap_and_overlap() -> None:
    """@brief 用户输入的 DST gap/fold 必须显式消歧 / User-entered DST gaps and folds require explicit disambiguation."""

    new_york = TimeZoneId("America/New_York")

    with pytest.raises(NonexistentLocalTimeError, match="does not exist"):
        new_york.resolve_local(datetime(2026, 3, 8, 2, 30))
    with pytest.raises(AmbiguousLocalTimeError, match="explicit UTC offset"):
        new_york.resolve_local(datetime(2026, 11, 1, 1, 30))


def test_calendar_occurrence_has_deterministic_dst_policy() -> None:
    """@brief 内部日历 recurrence 稳定处理 gap 与 fold / Internal calendar recurrence handles gaps and folds deterministically."""

    new_york = TimeZoneId("America/New_York")
    gap = new_york.resolve_calendar_occurrence(datetime(2026, 3, 8, 2, 30))
    fold = new_york.resolve_calendar_occurrence(datetime(2026, 11, 1, 1, 30))

    assert new_york.localize(gap).isoformat() == "2026-03-08T03:30:00-04:00"
    assert fold.isoformat() == "2026-11-01T05:30:00+00:00"


def test_unknown_iana_zone_is_rejected() -> None:
    """@brief 未知时区不降级为服务器本地时区 / An unknown zone never falls back to server-local time."""

    with pytest.raises(TemporalValueError, match="Unknown IANA"):
        TimeZoneId("Mars/Olympus_Mons")
