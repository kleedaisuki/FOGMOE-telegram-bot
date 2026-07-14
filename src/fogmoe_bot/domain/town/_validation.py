"""@brief 小镇领域共享校验 / Shared validation for the town domain."""

from __future__ import annotations

from datetime import UTC, datetime


def normalize_instant(value: datetime, *, field: str) -> datetime:
    """@brief 校验并归一化为 UTC 时刻 / Validate and normalize an instant to UTC.

    @param value 原始时刻 / Raw instant.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @return UTC 时刻 / UTC instant.
    @raise TypeError 输入不是 ``datetime`` 时抛出 / Raised when input is not a ``datetime``.
    @raise ValueError 时刻缺少时区时抛出 / Raised when instant is timezone-naive.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def normalize_text(
    value: str,
    *,
    field: str,
    minimum_length: int,
    maximum_length: int,
) -> str:
    """@brief 去除文本首尾空白并校验长度 / Trim text and validate its length.

    @param value 原始文本 / Raw text.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @param minimum_length 最小字符数 / Minimum character count.
    @param maximum_length 最大字符数 / Maximum character count.
    @return 规范文本 / Normalized text.
    @raise TypeError 输入不是字符串时抛出 / Raised when input is not a string.
    @raise ValueError 文本长度不合法时抛出 / Raised when text length is invalid.
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
    @param field 出错信息中的字段名称 / Field name for error messages.
    @return 经校验的幂等键 / Validated idempotency key.
    """

    return normalize_text(value, field=field, minimum_length=1, maximum_length=200)
