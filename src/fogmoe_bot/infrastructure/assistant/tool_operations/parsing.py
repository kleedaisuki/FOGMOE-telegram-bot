"""Assistant 工具 operation 共用的参数解析 / Shared argument parsing for tool operations."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.temporal import ensure_utc


def required_connection(connection: AsyncConnection | None) -> AsyncConnection:
    """要求 caller 已打开 mutation transaction / Require a caller transaction."""

    if connection is None:
        raise RuntimeError("Mutating tool requires an active transaction")
    return connection


def required_text(values: JsonObject, key: str) -> str:
    """读取并裁剪必需文本 / Read and trim required text."""

    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def optional_text(values: JsonObject, key: str) -> str | None:
    """读取并裁剪可选文本 / Read and trim optional text."""

    value = values.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def optional_int(values: JsonObject, key: str) -> int | None:
    """读取非布尔可选整数 / Read an optional non-boolean integer."""

    value = values.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def bounded_int(
    values: JsonObject,
    key: str,
    *,
    minimum: int,
    maximum: int | None = None,
    default: int | None = None,
) -> int:
    """读取有界非布尔整数 / Read a bounded non-boolean integer."""

    raw = values.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{key} must be an integer")
    if raw < minimum or (maximum is not None and raw > maximum):
        raise ValueError(f"{key} is outside its allowed range")
    return raw


def iso_instant(value: object) -> str | None:
    """将可选时间序列化为 aware UTC ISO 文本 / Serialize an instant as UTC ISO text."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    return str(value)
