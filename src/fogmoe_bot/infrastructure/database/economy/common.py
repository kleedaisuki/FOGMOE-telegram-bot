"""@brief PostgreSQL 经济共享账户与回执 primitives / Shared PostgreSQL account and receipt primitives for economy."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.economy import AccountBalance
from fogmoe_bot.infrastructure.database import connection as db_connection


async def _lock_account(
    user_id: int,
    connection: AsyncConnection,
) -> AccountBalance | None:
    """@brief 使用 ``FOR UPDATE`` 锁定账户 / Lock an account with ``FOR UPDATE``.

    @param user_id 用户 ID / User ID.
    @param connection 当前事务 / Current transaction.
    @return 账户快照；不存在为 None / Account snapshot, or None.
    """

    row = await db_connection.fetch_one(
        "SELECT id, coins, coins_paid, user_plan FROM identity.users "
        "WHERE id = %s FOR UPDATE",
        (user_id,),
        connection=connection,
    )
    if row is None:
        return None
    return AccountBalance(
        cast(int, row[0]),
        cast(int, row[1]),
        cast(int, row[2]),
        cast(str, row[3]),
    )


async def _credit_free(
    user_id: int,
    amount: int,
    connection: AsyncConnection,
) -> None:
    """@brief 增加已锁定账户免费金币 / Credit free coins to an already locked account.

    @param user_id 用户 ID / User ID.
    @param amount 正整数金币 / Positive coins.
    @param connection 当前事务 / Current transaction.
    @return None / None.
    """

    if amount <= 0:
        raise ValueError("Coin credit must be positive")
    changed = await db_connection.execute(
        "UPDATE identity.users SET coins = coins + %s WHERE id = %s",
        (amount, user_id),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Locked economy account disappeared")


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

    row = await db_connection.fetch_one(
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

    await db_connection.execute(
        "INSERT INTO economy.operation_receipts "
        "(idempotency_key, operation_kind, user_id, result) "
        "VALUES (%s, %s, %s, CAST(%s AS JSONB))",
        (idempotency_key, operation_kind, user_id, json.dumps(dict(result))),
        connection=connection,
    )


def _plan_after_spend(user_id: int, paid: int, admin_user_id: int) -> str:
    """@brief 计算扣费后账户计划 / Calculate the post-spend account plan.

    @param user_id 用户 ID / User ID.
    @param paid 剩余付费币 / Remaining paid coins.
    @param admin_user_id 管理员用户 ID / Administrator user ID.
    @return 新账户计划 / New account plan.
    """

    if user_id == admin_user_id:
        return "admin"
    return "paid" if paid > 0 else "free"
