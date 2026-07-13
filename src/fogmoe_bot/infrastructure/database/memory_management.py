"""@brief PostgreSQL Memory 遗忘与确认 outbox UoW / PostgreSQL UoW for memory forgetting and confirmation."""

from __future__ import annotations

from fogmoe_bot.application.memory import ForgetMemory, ForgetMemoryResult
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.command_source import (
    validate_telegram_command_source,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
    StandaloneOutboxWriter,
)
from fogmoe_bot.infrastructure.database.retrieval_scope import lock_retrieval_scope


class PostgresMemoryForgetUoW:
    """@brief 原子写入遗忘边界、删除 passages 并确认 / Atomically persist a forgetting boundary, delete passages, and confirm."""

    def __init__(self, outbox: StandaloneOutboxWriter | None = None) -> None:
        """@brief 注入 connection-bound outbox primitive / Inject the connection-bound outbox primitive.

        @param outbox 可选 outbox 替身 / Optional outbox substitute.
        """

        self._outbox = outbox or PostgresOutboxRepository()

    async def forget(self, command: ForgetMemory) -> ForgetMemoryResult:
        """@brief 幂等清除请求上界内的派生检索记忆 / Idempotently clear derived retrieval memory through the cutoff.

        @param command 已校验遗忘命令 / Validated forgetting command.
        @return 规范结果 / Canonical result.
        @note outbox 行同时充当命令回执；重放不会删除首次请求之后形成的新记忆。/
            The outbox row also acts as the command receipt; replay cannot delete memory formed after the original request.
        """

        async with db_connection.transaction() as connection:
            await validate_telegram_command_source(
                command.source,
                command.conversation_id,
                operation="Memory reset",
                connection=connection,
            )
            confirmation = (
                await self._outbox.enqueue_standalone_outbound_in_transaction(
                    connection,
                    command.confirmation,
                )
            )
            if not confirmation.inserted:
                return ForgetMemoryResult(0, False, confirmation)

            await lock_retrieval_scope(connection, command.scope)
            if command.scope.kind == "personal":
                user = await db_connection.fetch_one(
                    "SELECT 1 FROM identity.users WHERE id = %s",
                    (command.scope.scope_id,),
                    connection=connection,
                )
                if user is None:
                    return ForgetMemoryResult(0, True, confirmation)
            await db_connection.execute(
                "INSERT INTO retrieval.scope_forgetting_boundaries "
                "(scope_kind, scope_id, personal_user_id, forgotten_through, "
                "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (scope_kind, scope_id) DO UPDATE SET forgotten_through = "
                "GREATEST(retrieval.scope_forgetting_boundaries.forgotten_through, "
                "EXCLUDED.forgotten_through), updated_at = GREATEST("
                "retrieval.scope_forgetting_boundaries.updated_at, EXCLUDED.updated_at)",
                (
                    command.scope.kind,
                    command.scope.scope_id,
                    (
                        command.scope.scope_id
                        if command.scope.kind == "personal"
                        else None
                    ),
                    command.requested_at,
                    command.requested_at,
                    command.requested_at,
                ),
                connection=connection,
            )
            deleted = await db_connection.execute(
                "DELETE FROM retrieval.passages WHERE scope_kind = %s AND scope_id = %s "
                "AND occurred_at <= %s",
                (
                    command.scope.kind,
                    command.scope.scope_id,
                    command.requested_at,
                ),
                connection=connection,
            )
            await db_connection.execute(
                "DELETE FROM retrieval.source_projections AS projection "
                "USING conversation.conversation_turns AS turn "
                "WHERE projection.source_kind = 'conversation.turn' "
                "AND projection.source_id = turn.turn_id "
                "AND projection.scope_kind = %s AND projection.scope_id = %s "
                "AND turn.created_at <= %s",
                (
                    command.scope.kind,
                    command.scope.scope_id,
                    command.requested_at,
                ),
                connection=connection,
            )
            return ForgetMemoryResult(deleted, True, confirmation)


__all__ = ["PostgresMemoryForgetUoW"]
