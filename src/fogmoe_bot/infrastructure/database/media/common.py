"""@brief 媒体持久化共享时间原语 / Shared time primitive for media persistence."""

from datetime import UTC, datetime


def utc(value: datetime) -> datetime:
    """@brief 规范化 aware UTC 时间 / Normalize an aware UTC instant.

    @param value 待规范化时刻 / Instant to normalize.
    @return UTC 时刻 / UTC instant.
    @raise ValueError 时间缺少时区时抛出 / Raised when the instant lacks a timezone.
    """

    if value.tzinfo is None:
        raise ValueError("media repository requires aware datetimes")
    return value.astimezone(UTC)
