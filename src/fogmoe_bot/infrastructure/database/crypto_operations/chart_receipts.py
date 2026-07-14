"""@brief 群组图表的窄 PostgreSQL receipt 原语 / Narrow PostgreSQL receipt primitives for group charts.

图表绑定只需要幂等 receipt 和事务级 advisory lock。这里刻意不导入旧的账户、余额或
预测结构，保证 `/chart` 的持久化边界不可能写入游戏金币。
/ Chart binding needs only idempotency receipts and transaction-scoped advisory locks.  This
module deliberately imports no legacy account, balance, or prediction structures, ensuring the
`/chart` persistence boundary cannot write game tokens.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure.database import connection as db_connection


async def lock_chart_receipt(key: str, connection: AsyncConnection) -> None:
    """@brief 串行化同一图表幂等键 / Serialize one chart idempotency key.

    @param key 图表命令幂等键 / Chart-command idempotency key.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await advisory_lock(f"chart-receipt:{key}", connection)


async def advisory_lock(value: str, connection: AsyncConnection) -> None:
    """@brief 获取事务级 PostgreSQL advisory lock / Acquire a transaction-scoped PostgreSQL advisory lock.

    @param value 锁值 / Lock value.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (value,),
        connection=connection,
    )


async def load_chart_receipt(
    key: str,
    *,
    operation_kind: str,
    actor_id: int,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 读取并验证图表幂等 receipt / Load and validate a chart idempotency receipt.

    @param key 图表命令幂等键 / Chart-command idempotency key.
    @param operation_kind 本次操作种类 / Current operation kind.
    @param actor_id 本次操作者 ID / Current actor identifier.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已验证结果映射或 None / Validated result mapping or None.
    @raise ValueError 同一键试图改变操作语义或 receipt 形状非法时抛出 /
        Raised when a key changes operation semantics or receipt shape is invalid.
    """

    row = await db_connection.fetch_one(
        "SELECT operation_kind, actor_id, result "
        "FROM crypto.operation_receipts WHERE idempotency_key = %s",
        (key,),
        connection=connection,
    )
    if row is None:
        return None
    if str(row[0]) != operation_kind or int(row[1]) != actor_id:
        raise ValueError("Chart idempotency key changed meaning")
    raw_result: object = row[2]
    decoded: object = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    if not isinstance(decoded, Mapping):
        raise ValueError("Invalid chart operation receipt")
    return cast(Mapping[str, Any], decoded)


async def save_chart_receipt(
    key: str,
    operation_kind: str,
    actor_id: int,
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 在调用者事务中保存图表 receipt / Save a chart receipt in the caller-owned transaction.

    @param key 图表命令幂等键 / Chart-command idempotency key.
    @param operation_kind 操作种类 / Operation kind.
    @param actor_id 操作者 ID / Actor identifier.
    @param result 可 JSON 序列化的结果映射 / JSON-serializable result mapping.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

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


__all__ = [
    "advisory_lock",
    "load_chart_receipt",
    "lock_chart_receipt",
    "save_chart_receipt",
]
