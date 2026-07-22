"""@brief Billing 领域共享校验 / Shared validation for the billing domain."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final

_CODE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_.-]*$")
"""@brief 产品与权益代码允许的保守字符集 / Conservative alphabet for product and entitlement codes."""


def normalize_code(value: str, *, field: str, maximum_length: int = 96) -> str:
    """@brief 规范化产品或权益代码 / Normalize a product or entitlement code.

    @param value 原始代码 / Raw code.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @param maximum_length 最大代码长度 / Maximum code length.
    @return 小写且经过校验的代码 / Lowercase validated code.
    @raise TypeError 输入不是字符串时抛出 / Raised when input is not a string.
    @raise ValueError 输入为空、过长或包含非法字符时抛出 /
        Raised when input is blank, oversized, or contains invalid characters.
    """

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip().lower()
    if not 1 <= len(normalized) <= maximum_length:
        raise ValueError(f"{field} must contain 1-{maximum_length} characters")
    if _CODE_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field} contains unsupported characters")
    return normalized


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


def normalize_reference(value: str, *, field: str, maximum_length: int = 256) -> str:
    """@brief 校验外部支付参考号 / Validate an external payment reference.

    @param value 原始参考号 / Raw reference.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @param maximum_length 最大参考号长度 / Maximum reference length.
    @return 去除首尾空白的参考号 / Trimmed reference.
    @raise TypeError 输入不是字符串时抛出 / Raised when input is not a string.
    @raise ValueError 参考号为空或过长时抛出 / Raised when reference is blank or oversized.
    """

    return normalize_text(
        value,
        field=field,
        minimum_length=1,
        maximum_length=maximum_length,
    )


def require_positive_identity(value: int, *, field: str) -> int:
    """@brief 校验正整数身份标识 / Validate a positive integral identity.

    @param value 原始身份标识 / Raw identity.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @return 已校验的身份标识 / Validated identity.
    @raise TypeError 身份标识不是严格整数时抛出 / Raised when identity is not a strict integer.
    @raise ValueError 身份标识不为正时抛出 / Raised when identity is not positive.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value <= 0:
        raise ValueError(f"{field} must be positive")
    return value


def normalize_instant(value: datetime, *, field: str) -> datetime:
    """@brief 校验并归一化为 UTC 时刻 / Validate and normalize an instant to UTC.

    @param value 原始时刻 / Raw instant.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @return UTC 时刻 / UTC instant.
    @raise TypeError 输入不是 datetime 时抛出 / Raised when input is not a datetime.
    @raise ValueError 时刻缺少时区时抛出 / Raised when instant is timezone-naive.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
