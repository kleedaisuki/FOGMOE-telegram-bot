"""@brief PostgreSQL 群消息规范投影 / PostgreSQL canonical group-message projection."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import cast

from sqlalchemy.engine.row import RowMapping

from fogmoe_bot.application.chat.group_messages import (
    MAX_GROUP_CONTEXT_MESSAGES,
    GroupMessage,
    GroupMessageKind,
    GroupMessageObservation,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresGroupMessageProjection:
    """@brief 以 Telegram Update 序号收敛规范群消息 / Converge canonical group messages by Telegram Update sequence."""

    async def project(self, observation: GroupMessageObservation) -> None:
        """@brief 按 ``(group_id,message_id)`` 幂等插入或推进编辑 / Idempotently insert or advance an edit by ``(group_id,message_id)``.

        @param observation 已校验观察 / Validated observation.
        @return None / None.
        @note 相同或更旧 Update 不覆盖较新编辑；SQL 单语句即完整事务边界。/
        Equal or older Updates never overwrite a newer edit; one SQL statement is the full transaction boundary.
        """

        await db_connection.execute(
            "INSERT INTO conversation.group_message_projection "
            "(group_id, message_id, message_thread_id, user_id, sender_name, "
            "sender_username, message_type, content, created_at, source_update_id, "
            "content_encoding, is_edited, is_canonical, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'plain', %s, TRUE, %s) "
            "ON CONFLICT (group_id, message_id) WHERE is_canonical DO UPDATE SET "
            "message_thread_id = EXCLUDED.message_thread_id, user_id = EXCLUDED.user_id, "
            "sender_name = EXCLUDED.sender_name, sender_username = EXCLUDED.sender_username, "
            "message_type = EXCLUDED.message_type, content = EXCLUDED.content, "
            "source_update_id = EXCLUDED.source_update_id, "
            "content_encoding = 'plain', is_edited = EXCLUDED.is_edited, "
            "created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at "
            "WHERE group_message_projection.source_update_id IS NULL OR "
            "EXCLUDED.source_update_id > group_message_projection.source_update_id",
            (
                observation.group_id,
                observation.message_id,
                observation.message_thread_id,
                observation.sender_user_id,
                observation.sender_name,
                observation.sender_username,
                observation.kind.value,
                observation.content,
                observation.created_at,
                observation.source_update_id,
                observation.edited,
                observation.updated_at,
            ),
        )

    async def fetch_before(
        self,
        group_id: int,
        *,
        message_thread_id: int | None,
        before_message_id: int | None,
        limit: int,
    ) -> tuple[GroupMessage, ...]:
        """@brief 读取消息之前的规范上下文并按时间正序返回 / Read canonical context before a message and return chronological order.

        @param group_id 群 chat ID / Group chat identifier.
        @param message_thread_id 可选 Topic ID / Optional topic identifier.
        @param before_message_id 可选排他上界 / Optional exclusive message-ID bound.
        @param limit 最大消息数 / Maximum messages.
        @return 最旧到最新的消息 / Messages ordered oldest to newest.
        """

        if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id == 0:
            raise ValueError("group_id must be a non-zero integer")
        if before_message_id is not None and (
            isinstance(before_message_id, bool)
            or not isinstance(before_message_id, int)
            or before_message_id <= 0
        ):
            raise ValueError("before_message_id must be positive when present")
        if message_thread_id is not None and (
            isinstance(message_thread_id, bool)
            or not isinstance(message_thread_id, int)
            or message_thread_id <= 0
        ):
            raise ValueError("message_thread_id must be positive when present")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_GROUP_CONTEXT_MESSAGES
        ):
            raise ValueError(
                f"limit must be between 1 and {MAX_GROUP_CONTEXT_MESSAGES}"
            )
        boundary_sql = (
            "" if before_message_id is None else "AND projection.message_id < %s "
        )
        parameters: tuple[object, ...] = (
            (group_id, message_thread_id, limit)
            if before_message_id is None
            else (group_id, message_thread_id, before_message_id, limit)
        )
        rows = await db_connection.fetch_all(
            "SELECT projection.group_id, projection.message_id, projection.user_id, "
            "projection.message_thread_id, "
            "projection.message_type, projection.content, projection.content_encoding, "
            "projection.created_at, projection.is_edited, "
            "COALESCE(projection.sender_name, identity.name) AS sender_name, "
            "projection.sender_username "
            "FROM conversation.group_message_projection AS projection "
            "LEFT JOIN identity.users AS identity ON identity.id = projection.user_id "
            "WHERE projection.group_id = %s "
            "AND projection.message_thread_id IS NOT DISTINCT FROM %s "
            "AND projection.is_canonical "
            f"{boundary_sql}"
            "ORDER BY projection.message_id DESC, projection.id DESC LIMIT %s",
            parameters,
            mapping=True,
        )
        return tuple(_message(row) for row in reversed(rows))


def _message(row: RowMapping) -> GroupMessage:
    """@brief 将查询行转换为读取模型 / Convert a query row to the read model."""

    content = "" if row["content"] is None else str(row["content"])
    if str(row["content_encoding"]) == "base64":
        content = _decode_legacy(content)
    created_at = row["created_at"]
    if not isinstance(created_at, datetime):
        raise TypeError("group-message created_at must be a datetime")
    user_id = row["user_id"]
    sender_name = row["sender_name"]
    sender_username = row["sender_username"]
    message_thread_id = row["message_thread_id"]
    return GroupMessage(
        group_id=_integer(row, "group_id"),
        message_id=_integer(row, "message_id"),
        sender_user_id=None if user_id is None else int(cast(int, user_id)),
        sender_name=None if sender_name is None else str(sender_name),
        kind=GroupMessageKind(str(row["message_type"])),
        content=content,
        created_at=created_at,
        edited=_boolean(row, "is_edited"),
        sender_username=(None if sender_username is None else str(sender_username)),
        message_thread_id=(
            None if message_thread_id is None else int(cast(int, message_thread_id))
        ),
    )


def _decode_legacy(value: str) -> str:
    """@brief 尽力解码旧 non-text base64 / Best-effort decode legacy non-text base64."""

    try:
        return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
    except UnicodeDecodeError, UnicodeEncodeError, ValueError:
        return value


def _integer(row: RowMapping, key: str) -> int:
    """@brief 从数据库行读取严格整数 / Read a strict integer from a database row."""

    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"group-message {key} must be an integer")
    return value


def _boolean(row: RowMapping, key: str) -> bool:
    """@brief 从数据库行读取布尔值 / Read a boolean from a database row."""

    value = row[key]
    if not isinstance(value, bool):
        raise TypeError(f"group-message {key} must be a bool")
    return value
