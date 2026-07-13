"""@brief Retrieval 作用域的数据库并发原语 / Database concurrency primitives for Retrieval scopes."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.retrieval import RetrievalScope
from fogmoe_bot.infrastructure.database import connection as db_connection


async def lock_retrieval_scope(
    connection: AsyncConnection,
    scope: RetrievalScope,
) -> None:
    """@brief 获取一个事务级检索作用域写锁 / Acquire a transaction-level Retrieval-scope write lock.

    @param connection 当前事务 / Current transaction.
    @param scope 隔离域 / Isolation scope.
    @return None / None.
    @note 投影与遗忘必须共同使用该锁，才能消除删除后的异步复活。/
        Projection and forgetting must share this lock to prevent asynchronous resurrection.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"retrieval-scope:{scope.kind}:{scope.scope_id}",),
        connection=connection,
    )


__all__ = ["lock_retrieval_scope"]
