"""@brief 跨领域时间值与 UTC 不变量 / Cross-domain temporal values and UTC invariants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TemporalValueError(ValueError):
    """@brief 时间值违反领域不变量 / A temporal value violates domain invariants."""


class AmbiguousLocalTimeError(TemporalValueError):
    """@brief 本地墙钟时间对应两个瞬间 / A local wall-clock time maps to two instants."""


class NonexistentLocalTimeError(TemporalValueError):
    """@brief 本地墙钟时间落入 UTC offset 跳变空洞 / A local wall-clock time falls in an offset gap."""


@dataclass(frozen=True, slots=True)
class TimeZoneId:
    """@brief 经 IANA 数据库验证的时区标识 / Time-zone identifier validated by the IANA database.

    @param value IANA 时区名，例如 ``Asia/Shanghai`` / IANA zone name such as ``Asia/Shanghai``.
    """

    value: str

    def __post_init__(self) -> None:
        """@brief 规范并验证时区名 / Normalize and validate the time-zone name.

        @return None / None.
        @raise TemporalValueError 名称为空、过长或不存在时抛出 / Raised for an empty, oversized, or unknown name.
        """

        normalized = self.value.strip()
        if not normalized or len(normalized) > 64:
            raise TemporalValueError("Time-zone name must contain 1-64 characters")
        try:
            ZoneInfo(normalized)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise TemporalValueError(f"Unknown IANA time zone: {normalized}") from error
        object.__setattr__(self, "value", normalized)

    @property
    def zone(self) -> ZoneInfo:
        """@brief 返回标准库时区对象 / Return the standard-library zone object.

        @return 对应 IANA 规则的 ``ZoneInfo`` / ``ZoneInfo`` carrying the IANA rules.
        """

        return ZoneInfo(self.value)

    def localize(self, instant: datetime) -> datetime:
        """@brief 将唯一 UTC 瞬间投影为本地时间 / Project a unique UTC instant into local time.

        @param instant aware 瞬间 / Aware instant.
        @return 带 IANA 时区的本地 datetime / Local datetime carrying the IANA zone.
        """

        return ensure_utc(instant).astimezone(self.zone)

    def resolve_local(self, local_time: datetime) -> datetime:
        """@brief 严格解析本地墙钟时间 / Strictly resolve a local wall-clock time.

        @param local_time 不带时区的本地 datetime / Naive local datetime.
        @return 唯一对应的 UTC aware datetime / The unique corresponding UTC-aware datetime.
        @raise TemporalValueError 输入已带时区时抛出 / Raised when the input is already aware.
        @raise AmbiguousLocalTimeError 回拨区间对应两个瞬间时抛出 / Raised when an overlap maps to two instants.
        @raise NonexistentLocalTimeError 前拨空洞不存在该时间时抛出 / Raised when the time falls in a forward gap.
        @note 调用方必须让用户用显式 offset 消解歧义，不能静默猜测 ``fold``。/
            Callers must request an explicit offset instead of silently guessing ``fold``.
        """

        if local_time.tzinfo is not None or local_time.utcoffset() is not None:
            raise TemporalValueError("Local wall-clock input must be timezone-naive")
        candidates = _local_candidates(local_time, self)
        if not candidates:
            raise NonexistentLocalTimeError(
                f"Local time does not exist in {self.value}: {local_time.isoformat()}"
            )
        if len(candidates) > 1:
            raise AmbiguousLocalTimeError(
                f"Local time is ambiguous in {self.value}; include an explicit UTC offset: "
                f"{local_time.isoformat()}"
            )
        return next(iter(candidates))

    def resolve_calendar_occurrence(self, local_time: datetime) -> datetime:
        """@brief 以稳定 DST 策略解析周期发生项 / Resolve a recurring calendar occurrence with a stable DST policy.

        @param local_time 不带时区的计划墙钟时间 / Naive scheduled wall-clock time.
        @return UTC aware 发生时刻 / UTC-aware occurrence instant.
        @note 回拨重叠选择较早瞬间；前拨空洞按 offset 跳变量向前平移。/
            Overlaps choose the earlier instant; gaps shift forward by the offset transition.
        """

        if local_time.tzinfo is not None or local_time.utcoffset() is not None:
            raise TemporalValueError("Calendar occurrence input must be timezone-naive")
        candidates = _local_candidates(local_time, self)
        if candidates:
            return min(candidates)

        projected: list[tuple[datetime, datetime]] = []
        for fold in (0, 1):
            instant = ensure_utc(local_time.replace(tzinfo=self.zone, fold=fold))
            round_trip = instant.astimezone(self.zone).replace(tzinfo=None)
            if round_trip >= local_time:
                projected.append((round_trip, instant))
        if not projected:
            raise NonexistentLocalTimeError(
                f"Cannot advance nonexistent local time in {self.value}: "
                f"{local_time.isoformat()}"
            )
        return min(projected, key=lambda item: (item[0], item[1]))[1]


UTC_TIME_ZONE = TimeZoneId("UTC")
"""@brief UTC 的规范 IANA 标识 / Canonical IANA identifier for UTC."""


@dataclass(frozen=True, slots=True)
class UtcInterval:
    """@brief 左闭右开的 UTC 瞬间区间 / Half-open interval of UTC instants.

    @param start 包含的起点 / Inclusive lower bound.
    @param end 不包含的终点 / Exclusive upper bound.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        """@brief 规范区间端点并强制非空 / Normalize bounds and require a non-empty interval.

        @return None / None.
        @raise TemporalValueError 终点不晚于起点时抛出 / Raised when the upper bound is not later than the lower bound.
        """

        start = ensure_utc(self.start)
        end = ensure_utc(self.end)
        if end <= start:
            raise TemporalValueError("UTC interval end must be later than start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @classmethod
    def around(cls, anchor: datetime, radius: timedelta) -> UtcInterval:
        """@brief 构造定点附近的对称检索窗 / Build a symmetric search window around an instant.

        @param anchor 中心瞬间 / Center instant.
        @param radius 单侧半径 / Radius on either side.
        @return ``[anchor-radius, anchor+radius)`` / ``[anchor-radius, anchor+radius)``.
        @raise TemporalValueError 半径非正时抛出 / Raised for a non-positive radius.
        """

        if radius <= timedelta():
            raise TemporalValueError("UTC interval radius must be positive")
        instant = ensure_utc(anchor)
        return cls(instant - radius, instant + radius)

    def contains(self, instant: datetime) -> bool:
        """@brief 判断瞬间是否位于区间内 / Test whether an instant lies in the interval.

        @param instant 待判断 aware 瞬间 / Aware instant to test.
        @return 满足 ``start <= instant < end`` 时为 True / True for ``start <= instant < end``.
        """

        value = ensure_utc(instant)
        return self.start <= value < self.end


def ensure_utc(value: datetime) -> datetime:
    """@brief 强制使用 aware UTC 时间 / Require and normalize an aware UTC timestamp.

    @param value 输入时间 / Input timestamp.
    @return 转换为 UTC 的 aware datetime / Aware datetime converted to UTC.
    @raise TemporalValueError 输入为 naive datetime 时抛出 / Raised for a naive datetime.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        raise TemporalValueError("Domain timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _local_candidates(
    local_time: datetime, time_zone: TimeZoneId
) -> tuple[datetime, ...]:
    """@brief 枚举能往返保持墙钟值的唯一 UTC 瞬间 / Enumerate unique UTC instants that round-trip to a wall-clock value.

    @param local_time naive 本地时间 / Naive local time.
    @param time_zone IANA 时区 / IANA time zone.
    @return 按 UTC 排序且去重的候选 / Deduplicated candidates ordered by UTC.
    """

    candidates: set[datetime] = set()
    for fold in (0, 1):
        instant = ensure_utc(local_time.replace(tzinfo=time_zone.zone, fold=fold))
        round_trip = instant.astimezone(time_zone.zone).replace(tzinfo=None)
        if round_trip == local_time:
            candidates.add(instant)
    return tuple(sorted(candidates))


__all__ = [
    "AmbiguousLocalTimeError",
    "NonexistentLocalTimeError",
    "TemporalValueError",
    "TimeZoneId",
    "UTC_TIME_ZONE",
    "UtcInterval",
    "ensure_utc",
]
