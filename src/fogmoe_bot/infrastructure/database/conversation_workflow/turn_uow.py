"""@brief Turn/message 共享事务原语 / Shared Turn-and-message transaction primitives."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.conversation.errors import (
    ConcurrentTurnUpdateError,
    IdempotencyConflictError,
    TurnNotFoundError,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    MessageSequence,
    TurnId,
    TurnSource,
    UpdateId,
)
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageAppendResult,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.turn import (
    TERMINAL_TURN_STATES,
    ConversationTurn,
    TurnState,
)
from fogmoe_bot.infrastructure.database import db

from .common import (
    _datetime,
    _encode_json,
    _integer,
    _json_object,
    _optional_datetime,
    _optional_text,
    _row_values,
    _text,
    _uuid,
)

_TURN_COLUMNS = (
    "turn_id, conversation_id, source_kind, source_key, source_update_id, "
    "state, version, inference_attempts, delivery_attempts, next_retry_at, "
    "last_error, created_at, updated_at"
)
"""@brief Conversation Turn 规范 SELECT 列 / Canonical Conversation-Turn SELECT columns."""

_TURN_SELECT = "SELECT " + _TURN_COLUMNS + " FROM conversation.conversation_turns"
"""@brief Conversation Turn 规范 SELECT 前缀 / Canonical Conversation-Turn SELECT prefix."""


async def _load_turn_for_mutation(
    turn_id: TurnId,
    *,
    connection: AsyncConnection,
) -> ConversationTurn:
    """@brief 锁定并读取一个回合 / Lock and load one turn."""

    row = await db.fetch_one(
        _TURN_SELECT + " WHERE turn_id = CAST(%s AS UUID) FOR UPDATE",
        (str(turn_id),),
        connection=connection,
    )
    if row is None:
        raise TurnNotFoundError(f"Conversation turn {turn_id} does not exist")
    return _map_turn(row)


async def _persist_turn(
    updated: ConversationTurn,
    *,
    expected_version: int,
    connection: AsyncConnection,
) -> None:
    """@brief 以 expected version 持久化回合 / Persist a turn at an expected version."""

    completed_at = updated.updated_at if updated.state in TERMINAL_TURN_STATES else None
    rowcount = await db.execute(
        "UPDATE conversation.conversation_turns "
        "SET state = %s, version = %s, inference_attempts = %s, "
        "delivery_attempts = %s, next_retry_at = %s, last_error = %s, "
        "updated_at = %s, completed_at = %s "
        "WHERE turn_id = CAST(%s AS UUID) AND version = %s",
        (
            updated.state.value,
            updated.version,
            updated.inference_attempts,
            updated.delivery_attempts,
            updated.next_retry_at,
            updated.last_error,
            updated.updated_at,
            completed_at,
            str(updated.turn_id),
            expected_version,
        ),
        connection=connection,
    )
    if rowcount != 1:
        raise ConcurrentTurnUpdateError(
            f"Turn {updated.turn_id} changed while applying version {expected_version}"
        )


async def _append_message(
    draft: MessageDraft,
    *,
    connection: AsyncConnection,
) -> MessageAppendResult:
    """@brief 在调用方事务内幂等追加消息 / Idempotently append a message in the caller transaction."""

    existing_row = await _find_message(draft, connection=connection)
    if existing_row is not None:
        existing = _map_message(existing_row)
        _validate_message_idempotency(existing, draft)
        return MessageAppendResult(existing, False)

    await db.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (str(draft.conversation_id),),
        connection=connection,
    )
    existing_row = await _find_message(draft, connection=connection)
    if existing_row is not None:
        existing = _map_message(existing_row)
        _validate_message_idempotency(existing, draft)
        return MessageAppendResult(existing, False)

    sequence_row = await db.fetch_one(
        "SELECT COALESCE(MAX(sequence), 0) + 1 "
        "FROM conversation.conversation_messages WHERE conversation_id = %s",
        (str(draft.conversation_id),),
        connection=connection,
    )
    if sequence_row is None:
        raise RuntimeError("Could not allocate a conversation message sequence")
    sequence = _integer(sequence_row[0])
    row = await db.fetch_one(
        "INSERT INTO conversation.conversation_messages "
        "(message_id, conversation_id, sequence, turn_id, source_update_id, "
        "role, content, idempotency_key, created_at) "
        "VALUES (CAST(%s AS UUID), %s, %s, CAST(%s AS UUID), %s, %s, "
        "CAST(%s AS JSONB), %s, %s) "
        "RETURNING message_id, conversation_id, sequence, turn_id, source_update_id, "
        "role, content, idempotency_key, created_at",
        (
            str(draft.message_id),
            str(draft.conversation_id),
            sequence,
            str(draft.turn_id) if draft.turn_id else None,
            int(draft.source_update_id) if draft.source_update_id else None,
            draft.role.value,
            _encode_json(draft.content),
            draft.idempotency_key,
            draft.created_at,
        ),
        connection=connection,
    )
    if row is None:
        raise RuntimeError("Conversation message insert returned no row")
    return MessageAppendResult(_map_message(row), True)


async def _require_existing_message(
    draft: MessageDraft,
    *,
    operation: str,
    connection: AsyncConnection,
) -> MessageAppendResult:
    """@brief 读取已提交组合操作的规范消息 / Load a committed operation's canonical message."""

    row = await _find_message(draft, connection=connection)
    if row is None:
        raise ConcurrentTurnUpdateError(
            f"Turn advanced through {operation} without its atomic message"
        )
    existing = _map_message(row)
    _validate_message_idempotency(existing, draft)
    return MessageAppendResult(existing, False)


async def _find_message(
    draft: MessageDraft,
    *,
    connection: AsyncConnection,
) -> object | None:
    """@brief 通过实体 ID 或幂等键寻找消息 / Find a message by entity ID or idempotency key."""

    row: object | None = await db.fetch_one(
        "SELECT message_id, conversation_id, sequence, turn_id, source_update_id, "
        "role, content, idempotency_key, created_at "
        "FROM conversation.conversation_messages "
        "WHERE message_id = CAST(%s AS UUID) "
        "OR (conversation_id = %s AND idempotency_key = %s) LIMIT 1",
        (str(draft.message_id), str(draft.conversation_id), draft.idempotency_key),
        connection=connection,
    )
    return row


def _map_turn(row: object) -> ConversationTurn:
    """@brief 将数据库行映射为回合 / Map a database row to a Turn."""

    values = _row_values(row, 13)
    return ConversationTurn(
        turn_id=TurnId.parse(_uuid(values[0])),
        conversation_id=ConversationId(_text(values[1])),
        source=TurnSource(
            kind=_text(values[2]),
            key=_text(values[3]),
            update_id=UpdateId(_integer(values[4])) if values[4] is not None else None,
        ),
        state=TurnState(_text(values[5])),
        version=_integer(values[6]),
        inference_attempts=_integer(values[7]),
        delivery_attempts=_integer(values[8]),
        next_retry_at=_optional_datetime(values[9]),
        last_error=_optional_text(values[10]),
        created_at=_datetime(values[11]),
        updated_at=_datetime(values[12]),
    )


def _map_message(row: object) -> ConversationMessage:
    """@brief 将数据库行映射为追加式消息 / Map a database row to an append-only message."""

    values = _row_values(row, 9)
    draft = MessageDraft(
        message_id=ConversationMessageId.parse(_uuid(values[0])),
        conversation_id=ConversationId(_text(values[1])),
        turn_id=TurnId.parse(_uuid(values[3])) if values[3] is not None else None,
        source_update_id=(
            UpdateId(_integer(values[4])) if values[4] is not None else None
        ),
        role=MessageRole(_text(values[5])),
        content=_json_object(values[6]),
        idempotency_key=_text(values[7]),
        created_at=_datetime(values[8]),
    )
    return ConversationMessage(
        draft=draft,
        sequence=MessageSequence(_integer(values[2])),
    )


def _validate_message_idempotency(
    existing: ConversationMessage,
    draft: MessageDraft,
) -> None:
    """@brief 验证重复消息的语义一致 / Verify semantic equality for a duplicate message."""

    canonical = existing.draft
    if (
        canonical.message_id != draft.message_id
        or canonical.conversation_id != draft.conversation_id
        or canonical.turn_id != draft.turn_id
        or canonical.source_update_id != draft.source_update_id
        or canonical.role != draft.role
        or canonical.content != draft.content
        or canonical.idempotency_key != draft.idempotency_key
    ):
        raise IdempotencyConflictError(
            f"Message {draft.message_id} or idempotency key was reused with different semantics"
        )


def _validate_message_for_turn(
    turn: ConversationTurn,
    draft: MessageDraft,
    *,
    expected_role: MessageRole,
) -> None:
    """@brief 验证组合 UoW 的消息所有权 / Validate message ownership for a composite unit of work."""

    if draft.turn_id != turn.turn_id:
        raise ValueError("Composite message must belong to the target turn")
    if draft.conversation_id != turn.conversation_id:
        raise ValueError("Composite message must belong to the target conversation")
    if draft.role is not expected_role:
        raise ValueError(f"Composite message role must be {expected_role.value}")
    if (
        expected_role is MessageRole.USER
        and draft.source_update_id != turn.source.update_id
    ):
        raise ValueError(
            "User message must reference the Turn's optional source Update"
        )
    if (
        expected_role is MessageRole.ASSISTANT
        and draft.source_update_id is not None
        and draft.source_update_id != turn.source.update_id
    ):
        raise ValueError("Assistant message cannot reference another source Update")
