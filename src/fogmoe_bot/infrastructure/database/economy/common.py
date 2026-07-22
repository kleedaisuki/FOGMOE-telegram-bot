"""@brief PostgreSQL 经济共享账户与回执 primitives / Shared PostgreSQL account and receipt primitives for economy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure.database import db


async def _registered_user_exists(
    user_id: int,
    connection: AsyncConnection,
) -> bool:
    """@brief 仅检查身份账户存在，不把余额投影当作货币真相 / Check identity existence without treating its balance projection as monetary truth.

    @param user_id 用户 ID / User ID.
    @param connection 当前事务 / Current transaction.
    @return 身份账户存在时为 True / True when the identity account exists.
    @note 金币可用性必须由 ``bank.account_balances`` 的稳定锁序检查；这里故意不对
        ``identity.users`` 做 ``FOR UPDATE``，避免与账本投影触发器形成反向锁依赖。/
        Token availability must be checked through ``bank.account_balances`` in its stable
        lock order.  This helper deliberately avoids ``FOR UPDATE`` on ``identity.users``
        so it cannot form an inverse lock dependency with the ledger-projection trigger.
    """

    row = await db.fetch_one(
        "SELECT 1 FROM identity.users WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return row is not None


async def _lock_operation_key(
    key: str,
    connection: AsyncConnection,
) -> None:
    """@brief 为同一经济业务键取得事务级串行锁 / Acquire a transaction-scoped serialization lock for one economy business key.

    @param key 稳定业务或幂等键 / Stable business or idempotency key.
    @param connection 当前事务 / Current transaction.
    @return None / None.
    @raise ValueError 键为空时抛出 / Raised when the key is blank.
    @note 哈希冲突最多让不相关操作顺序化，绝不会把两个回执混为一谈。/
        A hash collision can only serialize unrelated operations; it can never merge their
        receipts.
    """

    normalized = key.strip()
    if not normalized:
        raise ValueError("Economy operation lock key cannot be blank")
    await db.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"economy:operation:{normalized}",),
        connection=connection,
    )


async def _load_result(
    idempotency_key: str,
    connection: AsyncConnection,
    *,
    expected_kind: str | None = None,
    expected_user_id: int | None = None,
) -> Mapping[str, Any] | None:
    """@brief 读取通用幂等回执 / Read a generic idempotency receipt.

    @param idempotency_key 幂等键 / Idempotency key.
    @param connection 当前事务 / Current transaction.
    @param expected_kind 可选预期操作类型 / Optional expected operation kind.
    @param expected_user_id 可选预期 actor / Optional expected actor.
    @return JSON 结果；未执行为 None / JSON result, or None.
    """

    row = await db.fetch_one(
        "SELECT operation_kind, user_id, result FROM economy.operation_receipts "
        "WHERE idempotency_key = %s",
        (idempotency_key,),
        connection=connection,
    )
    if row is None:
        return None
    if expected_kind is not None and cast(str, row[0]) != expected_kind:
        raise ValueError("Economy idempotency key changed operation kind")
    if expected_user_id is not None and cast(int, row[1]) != expected_user_id:
        raise ValueError("Economy idempotency key changed actor")
    value: object = row[2]
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise ValueError("Invalid economy operation receipt")
    return cast(Mapping[str, Any], decoded)


async def _save_result(
    idempotency_key: str,
    operation_kind: str,
    user_id: int,
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 与业务写同事务保存回执 / Save a receipt in the same transaction as business writes.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation_kind 操作类型 / Operation kind.
    @param user_id 用户 ID / User ID.
    @param result JSON 可序列化结果 / JSON-serializable result.
    @param connection 当前事务 / Current transaction.
    @return None / None.
    """

    await db.execute(
        "INSERT INTO economy.operation_receipts "
        "(idempotency_key, operation_kind, user_id, result) "
        "VALUES (%s, %s, %s, CAST(%s AS JSONB))",
        (idempotency_key, operation_kind, user_id, json.dumps(dict(result))),
        connection=connection,
    )
