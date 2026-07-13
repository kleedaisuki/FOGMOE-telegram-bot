"""@brief PostgreSQL Telegram 命令授权决定存储 / PostgreSQL store for Telegram command-authorization decisions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from fogmoe_bot.application.telegram.authorization import (
    GROUP_MEMORY_RESET_CAPABILITY,
    GroupAdministratorDecision,
)
from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.domain.conversation.identity import UpdateId
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresGroupAdministratorDecisionStore:
    """@brief first-writer-wins 持久化群管理员观测 / Persist group-administrator observations with first-writer-wins semantics."""

    async def read(
        self,
        update_id: UpdateId,
    ) -> GroupAdministratorDecision | None:
        """@brief 读取已冻结决定 / Read a frozen decision.

        @param update_id durable Update / Durable Update.
        @return 已有决定或 None / Existing decision or None.
        """

        row = await db_connection.fetch_one(
            "SELECT source_update_id, resource_id, actor_user_id, allowed, observed_at "
            "FROM conversation.command_authorization_decisions "
            "WHERE source_update_id = %s AND capability = %s",
            (int(update_id), GROUP_MEMORY_RESET_CAPABILITY),
        )
        return _map_decision(row) if row is not None else None

    async def freeze(
        self,
        decision: GroupAdministratorDecision,
    ) -> GroupAdministratorDecision:
        """@brief 冻结并返回规范决定 / Freeze and return the canonical decision.

        @param decision 候选决定 / Candidate decision.
        @return 首个规范决定 / First canonical decision.
        @raise IdempotencyConflictError Update 来源不存在 / The source Update does not exist.
        """

        async with db_connection.transaction() as connection:
            row = await db_connection.fetch_one(
                "SELECT conversation_id FROM conversation.inbound_updates "
                "WHERE update_id = %s FOR UPDATE",
                (int(decision.update_id),),
                connection=connection,
            )
            if row is None:
                raise IdempotencyConflictError(
                    f"Authorization source Update {int(decision.update_id)} does not exist"
                )
            await db_connection.execute(
                "INSERT INTO conversation.command_authorization_decisions "
                "(source_update_id, capability, resource_id, actor_user_id, allowed, "
                "observed_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (source_update_id, capability) DO NOTHING",
                (
                    int(decision.update_id),
                    GROUP_MEMORY_RESET_CAPABILITY,
                    decision.chat_id,
                    decision.actor_user_id,
                    decision.allowed,
                    decision.observed_at,
                ),
                connection=connection,
            )
            canonical = await db_connection.fetch_one(
                "SELECT source_update_id, resource_id, actor_user_id, allowed, observed_at "
                "FROM conversation.command_authorization_decisions "
                "WHERE source_update_id = %s AND capability = %s",
                (int(decision.update_id), GROUP_MEMORY_RESET_CAPABILITY),
                connection=connection,
            )
            if canonical is None:
                raise RuntimeError("Authorization decision insert returned no row")
            return _map_decision(canonical)


def _map_decision(row: object) -> GroupAdministratorDecision:
    """@brief 映射授权决定行 / Map an authorization-decision row.

    @param row 五列数据库行 / Five-column database row.
    @return 类型化决定 / Typed decision.
    """

    values = cast(Sequence[object], row)
    if len(values) != 5:
        raise RuntimeError(f"Expected 5 authorization columns, received {len(values)}")
    observed_at = values[4]
    if not isinstance(observed_at, datetime):
        raise TypeError("Authorization observed_at must be a datetime")
    allowed = values[3]
    if not isinstance(allowed, bool):
        raise TypeError("Authorization allowed must be a Boolean")
    return GroupAdministratorDecision(
        update_id=UpdateId(int(str(values[0]))),
        chat_id=int(str(values[1])),
        actor_user_id=int(str(values[2])),
        allowed=allowed,
        observed_at=observed_at,
    )


__all__ = ["PostgresGroupAdministratorDecisionStore"]
