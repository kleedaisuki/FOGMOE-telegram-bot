"""@brief User Profile 的数据库并发原语 / Database concurrency primitives for User Profile."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure.database import db


async def lock_user_profile(connection: AsyncConnection, user_id: int) -> None:
    """@brief 获取一个事务级 Profile owner 写锁 / Acquire a transaction-level Profile-owner write lock.

    @param connection 当前事务 / Current transaction.
    @param user_id Profile owner / Profile owner.
    @return None / None.
    @raise ValueError user_id 非正 / Non-positive user identifier.
    @note Evidence 投影、Dream completion 与清除命令共享该锁。/
        Evidence projection, Dream completion, and clearing share this lock.
    """

    if isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("Profile lock user_id must be positive")
    await db.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"user-profile:{user_id}",),
        connection=connection,
    )


__all__ = ["lock_user_profile"]
