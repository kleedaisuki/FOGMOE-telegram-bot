"""@brief 个人 RPG 领域共享校验 / Shared validation for the personal RPG domain."""

from __future__ import annotations

from datetime import UTC, date, datetime


def normalize_instant(value: datetime, *, field: str) -> datetime:
    """@brief 校验并规范化为 UTC 时刻 / Validate and normalize an instant to UTC.

    @param value 原始时刻 / Raw instant.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return UTC 时刻 / UTC instant.
    @raise TypeError 输入不是 ``datetime`` 时抛出 / Raised when the input is not a ``datetime``.
    @raise ValueError 时刻没有时区时抛出 / Raised when the instant is timezone-naive.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def normalize_day(value: date, *, field: str) -> date:
    """@brief 校验 UTC 业务日 / Validate a UTC business day.

    @param value 原始日期 / Raw date.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return 经校验日期 / Validated date.
    @raise TypeError 输入不是纯 ``date`` 时抛出 / Raised when the input is not a plain ``date``.
    @note ``datetime`` 是 ``date`` 的子类，故须显式拒绝，避免把时刻误作业务日。/
        ``datetime`` is a subclass of ``date`` and is explicitly rejected to avoid treating an instant as a business day.
    """

    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{field} must be a date")
    return value


def normalize_text(
    value: str,
    *,
    field: str,
    minimum_length: int,
    maximum_length: int,
) -> str:
    """@brief 去除首尾空白并校验文本长度 / Trim text and validate its length.

    @param value 原始文本 / Raw text.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @param minimum_length 最小字符数 / Minimum character count.
    @param maximum_length 最大字符数 / Maximum character count.
    @return 规范化文本 / Normalized text.
    @raise TypeError 输入不是字符串时抛出 / Raised when the input is not a string.
    @raise ValueError 文本长度不合法时抛出 / Raised when the text length is invalid.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not minimum_length <= len(normalized) <= maximum_length:
        raise ValueError(
            f"{field} must contain {minimum_length}-{maximum_length} characters"
        )
    return normalized


def normalize_idempotency_key(value: str, *, field: str) -> str:
    """@brief 规范化业务幂等键 / Normalize a business idempotency key.

    @param value 原始幂等键 / Raw idempotency key.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @return 经校验幂等键 / Validated idempotency key.
    """

    return normalize_text(value, field=field, minimum_length=1, maximum_length=200)
