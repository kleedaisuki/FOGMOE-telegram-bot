"""@brief 数据库组合命令共享的 durable 来源校验 / Durable-source validation shared by database command UoWs."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.domain.conversation.identity import ConversationId, TurnSource
from fogmoe_bot.infrastructure.database import connection as db_connection


async def validate_telegram_command_source(
    source: TurnSource,
    conversation_id: ConversationId,
    *,
    operation: str,
    connection: AsyncConnection,
) -> None:
    """@brief 锁定并验证 Telegram inbox 来源 / Lock and validate a Telegram inbox source.

    @param source 命令来源 / Command source.
    @param conversation_id 预期 Conversation / Expected Conversation.
    @param operation 安全错误标签 / Safe operation label.
    @param connection 当前事务 / Current transaction.
    @return None / None.
    @raise IdempotencyConflictError 来源不存在或 Conversation 漂移 / Missing source or changed Conversation.
    """

    update_id = source.update_id
    if update_id is None:
        return
    row = await db_connection.fetch_one(
        "SELECT conversation_id FROM conversation.inbound_updates "
        "WHERE update_id = %s FOR UPDATE",
        (int(update_id),),
        connection=connection,
    )
    if row is None:
        raise IdempotencyConflictError(
            f"{operation} source Update {int(update_id)} does not exist"
        )
    if str(row[0]) != str(conversation_id):
        raise IdempotencyConflictError(
            f"{operation} source Update {int(update_id)} changed conversation identity"
        )


__all__ = ["validate_telegram_command_source"]
