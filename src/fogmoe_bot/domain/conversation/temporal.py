"""会话工作流时间不变量 / Conversation workflow temporal invariants."""

from datetime import datetime, timezone


def ensure_utc(value: datetime) -> datetime:
    """@brief 强制使用 aware UTC 时间 / Require and normalize an aware UTC timestamp.

    @param value 输入时间 / Input timestamp.
    @return 转换为 UTC 的 aware datetime / Aware datetime converted to UTC.
    @raise ValueError 输入为 naive datetime 时抛出 / Raised for a naive datetime.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Conversation workflow timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)
