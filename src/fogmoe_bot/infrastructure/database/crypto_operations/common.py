"""Shared SQL primitives for Crypto feature adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.economy import AccountBalance
from fogmoe_bot.infrastructure.database import connection as db_connection


@dataclass(frozen=True, slots=True)
class LockedAccount:
    """An account snapshot protected by ``FOR UPDATE``."""

    balance: AccountBalance
    display_name: str


async def lock_account(
    user_id: int,
    connection: AsyncConnection,
) -> LockedAccount | None:
    """Lock one account using the repository-wide account-first order."""

    row = await db_connection.fetch_one(
        "SELECT id, coins, coins_paid, user_plan, name FROM identity.users "
        "WHERE id = %s FOR UPDATE",
        (user_id,),
        connection=connection,
    )
    if row is None:
        return None
    return LockedAccount(
        AccountBalance(
            int(row[0]),
            int(row[1]),
            int(row[2]),
            str(row[3]),
        ),
        str(row[4]),
    )


async def write_balance(
    balance: AccountBalance,
    admin_user_id: int,
    connection: AsyncConnection,
) -> None:
    """Persist an already-locked account balance and derived plan."""

    plan = (
        "admin"
        if balance.user_id == admin_user_id
        else ("paid" if balance.paid > 0 else "free")
    )
    changed = await db_connection.execute(
        "UPDATE identity.users SET coins = %s, coins_paid = %s, user_plan = %s "
        "WHERE id = %s",
        (balance.free, balance.paid, plan, balance.user_id),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Locked Crypto account disappeared")


async def lock_receipt(key: str, connection: AsyncConnection) -> None:
    """Serialize uses of one Crypto idempotency key."""

    await advisory_lock(f"crypto-receipt:{key}", connection)


async def advisory_lock(value: str, connection: AsyncConnection) -> None:
    """Acquire a transaction-scoped PostgreSQL advisory lock."""

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (value,),
        connection=connection,
    )


async def load_receipt(
    key: str,
    *,
    operation_kind: str,
    actor_id: int,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """Read a receipt and reject reuse with different semantics."""

    row = await db_connection.fetch_one(
        "SELECT operation_kind, actor_id, result "
        "FROM crypto.operation_receipts WHERE idempotency_key = %s",
        (key,),
        connection=connection,
    )
    if row is None:
        return None
    if str(row[0]) != operation_kind or int(row[1]) != actor_id:
        raise ValueError("Crypto idempotency key changed meaning")
    value: object = row[2]
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise ValueError("Invalid Crypto operation receipt")
    return cast(Mapping[str, Any], decoded)


async def save_receipt(
    key: str,
    operation_kind: str,
    actor_id: int,
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """Save a Crypto receipt in the caller-owned business transaction."""

    await db_connection.execute(
        "INSERT INTO crypto.operation_receipts "
        "(idempotency_key, operation_kind, actor_id, result) "
        "VALUES (%s, %s, %s, CAST(%s AS JSONB))",
        (
            key,
            operation_kind,
            actor_id,
            json.dumps(dict(result), ensure_ascii=False),
        ),
        connection=connection,
    )


def aware_timestamp(value: datetime) -> datetime:
    """Interpret a legacy ``TIMESTAMP`` in the process-local timezone."""

    if value.tzinfo is None:
        return value.astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def db_timestamp(value: datetime) -> datetime:
    """Produce the local-naive value used by the legacy PostgreSQL columns."""

    return value.astimezone().replace(tzinfo=None)


def integer(value: object) -> int:
    """Strictly parse a database integer value."""

    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"Expected database integer, got {type(value).__name__}")
    return int(value)
