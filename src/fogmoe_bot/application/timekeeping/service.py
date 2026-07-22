"""@brief 时区感知的时间查询与输入解析 / Time-zone-aware clock queries and input parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fogmoe_bot.application.runtime.clock import SystemUtcClock, UtcClock
from fogmoe_bot.domain.temporal import TimeZoneId, UtcInterval, ensure_utc


@dataclass(frozen=True, slots=True)
class TimeReading:
    """@brief 一个瞬间在指定时区的读数 / Reading of one instant in a requested time zone.

    @param instant_utc 唯一 UTC 瞬间 / Unique UTC instant.
    @param local_datetime 对应本地日期时间 / Corresponding local date and time.
    @param time_zone IANA 时区 / IANA time zone.
    """

    instant_utc: datetime
    local_datetime: datetime
    time_zone: TimeZoneId

    def __post_init__(self) -> None:
        """@brief 校验 UTC 与本地投影一致 / Validate consistency between the UTC instant and local projection.

        @return None / None.
        @raise ValueError 本地投影与瞬间不一致时抛出 / Raised when the local projection does not represent the instant.
        """

        instant = ensure_utc(self.instant_utc)
        expected_local = self.time_zone.localize(instant)
        if self.local_datetime != expected_local:
            raise ValueError("Time reading local projection does not match its UTC instant")
        object.__setattr__(self, "instant_utc", instant)

    @property
    def iso_weekday(self) -> int:
        """@brief 返回 ISO 星期序号 / Return the ISO weekday number.

        @return 周一为 1、周日为 7 / Monday is 1 and Sunday is 7.
        """

        return self.local_datetime.isoweekday()


class TimeService:
    """@brief 统一当前时间、时区和 ISO 输入语义 / Unify current-time, time-zone, and ISO-input semantics."""

    def __init__(
        self,
        *,
        default_time_zone: TimeZoneId,
        clock: UtcClock | None = None,
    ) -> None:
        """@brief 创建时间应用服务 / Create the time application service.

        @param default_time_zone 未显式指定时使用的 IANA 时区 / IANA zone used when none is explicit.
        @param clock 可替换的 UTC 时钟 / Replaceable UTC clock.
        """

        self._default_time_zone = default_time_zone
        self._clock = clock or SystemUtcClock()

    @property
    def default_time_zone(self) -> TimeZoneId:
        """@brief 返回配置的默认时区 / Return the configured default time zone.

        @return 默认 IANA 时区 / Default IANA time zone.
        """

        return self._default_time_zone

    def time_zone(self, name: str | None = None) -> TimeZoneId:
        """@brief 解析可选时区名 / Resolve an optional time-zone name.

        @param name 显式 IANA 名；None 使用默认值 / Explicit IANA name, or None for the default.
        @return 已验证的时区值 / Validated time-zone value.
        """

        return self._default_time_zone if name is None else TimeZoneId(name)

    def now(self, time_zone: str | None = None) -> TimeReading:
        """@brief 读取当前瞬间及其本地投影 / Read the current instant and its local projection.

        @param time_zone 可选 IANA 时区名 / Optional IANA time-zone name.
        @return 原子形成的时间读数 / Time reading formed from one clock sample.
        """

        instant = ensure_utc(self._clock.now())
        zone = self.time_zone(time_zone)
        return TimeReading(instant, zone.localize(instant), zone)

    def resolve(self, value: str, *, time_zone: str | None = None) -> datetime:
        """@brief 把 ISO 8601 文本解析为唯一 UTC 瞬间 / Resolve ISO 8601 text to one UTC instant.

        @param value 带 offset 的瞬间或不带 offset 的本地日期时间 / Offset-aware instant or offset-free local datetime.
        @param time_zone naive 输入使用的 IANA 时区 / IANA zone used for a naive input.
        @return UTC aware datetime / UTC-aware datetime.
        @raise ValueError 文本非法或本地时间不唯一时抛出 / Raised for malformed or non-unique local time.
        @note 带 offset 的输入已经唯一，``time_zone`` 只用于 naive 输入。/
            An input carrying an offset is already unique; ``time_zone`` is used only for naive input.
        """

        raw = value.strip()
        if not raw:
            raise ValueError("ISO datetime cannot be empty")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"Invalid ISO 8601 datetime: {raw}") from error
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return ensure_utc(parsed)
        return self.time_zone(time_zone).resolve_local(parsed)

    def interval(
        self,
        start: str,
        end: str,
        *,
        time_zone: str | None = None,
    ) -> UtcInterval:
        """@brief 解析左闭右开时间区间 / Parse a half-open temporal interval.

        @param start 包含的 ISO 起点 / Inclusive ISO lower bound.
        @param end 不包含的 ISO 终点 / Exclusive ISO upper bound.
        @param time_zone naive 端点使用的 IANA 时区 / IANA zone for naive bounds.
        @return 规范 UTC 区间 / Canonical UTC interval.
        """

        return UtcInterval(
            self.resolve(start, time_zone=time_zone),
            self.resolve(end, time_zone=time_zone),
        )


__all__ = ["TimeReading", "TimeService"]
