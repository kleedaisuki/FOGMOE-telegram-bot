"""@brief 群消息规范投影的真实 PostgreSQL 契约 / Real-PostgreSQL contract for the canonical group-message projection."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fogmoe_bot.domain.chat.group_messages import (
    GROUP_ATTACHMENT_MARKER,
    GroupContextQuery,
    GroupConversationScope,
    GroupMessageIdentity,
    GroupMessageKind,
    GroupMessageObservation,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.group_message_projection import (
    PostgresGroupMessageProjection,
)


def _user_id() -> int:
    """@brief 生成测试 BIGINT 用户 ID / Generate a test BIGINT user identifier."""

    return 8_500_000_000_000_000_000 + int(uuid4().hex[:10], 16)


def _identity(
    group_id: int,
    message_id: int,
    *,
    message_thread_id: int | None = None,
) -> GroupMessageIdentity:
    """@brief 构造群消息完整身份 / Build a complete group-message identity.

    @param group_id 群 ID / Group identifier.
    @param message_id 消息 ID / Message identifier.
    @param message_thread_id 可选 Topic ID / Optional topic identifier.
    @return 完整身份 / Complete identity.
    """

    return GroupMessageIdentity(
        GroupConversationScope(group_id, message_thread_id),
        message_id,
    )


def test_projection_replay_edit_order_and_context_window_are_canonical() -> None:
    """@brief replay、乱序 edit 与上下文窗口收敛到 canonical rows / Replay, out-of-order edits, and context windows converge to canonical rows."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        user_id = _user_id()
        group_id = -user_id
        now = datetime.now(UTC)
        projection = PostgresGroupMessageProjection()
        try:
            await db.execute(
                "INSERT INTO identity.users "
                "(id, tg_uid, provider, name) "
                "VALUES (%s, %s, 'telegram', %s)",
                (user_id, user_id, f"group-projection-{uuid4().hex}"),
            )
            original = GroupMessageObservation(
                100,
                _identity(group_id, 1),
                user_id,
                GroupMessageKind.TEXT,
                "original",
                now,
                now,
                False,
            )
            await projection.project(original)
            await projection.project(original)
            await projection.project(
                GroupMessageObservation(
                    101,
                    _identity(group_id, 1),
                    user_id,
                    GroupMessageKind.TEXT,
                    "edited",
                    now,
                    now + timedelta(seconds=1),
                    True,
                )
            )
            await projection.project(
                GroupMessageObservation(
                    103,
                    _identity(group_id, 3, message_thread_id=23),
                    None,
                    GroupMessageKind.TEXT,
                    "topic message",
                    now + timedelta(seconds=3),
                    now + timedelta(seconds=3),
                    False,
                    sender_name="Unregistered Speaker",
                    sender_username="visitor",
                )
            )
            await projection.project(
                GroupMessageObservation(
                    99,
                    _identity(group_id, 1),
                    user_id,
                    GroupMessageKind.TEXT,
                    "stale",
                    now,
                    now,
                    False,
                )
            )
            await projection.project(
                GroupMessageObservation(
                    102,
                    _identity(group_id, 2),
                    None,
                    GroupMessageKind.PHOTO,
                    GROUP_ATTACHMENT_MARKER,
                    now + timedelta(seconds=2),
                    now + timedelta(seconds=2),
                    False,
                )
            )

            rows = await db.fetch_all(
                "SELECT message_id, source_update_id, content, is_edited "
                "FROM conversation.group_message_projection "
                "WHERE group_id = %s AND is_canonical ORDER BY message_id",
                (group_id,),
            )
            assert [
                (int(row[0]), int(row[1]), str(row[2]), bool(row[3])) for row in rows
            ] == [
                (1, 101, "edited", True),
                (2, 102, GROUP_ATTACHMENT_MARKER, False),
                (3, 103, "topic message", False),
            ]
            context = await projection.fetch_before(
                GroupContextQuery(
                    GroupConversationScope(group_id),
                    before_message_id=4,
                    limit=10,
                )
            )
            assert [message.message_id for message in context] == [1, 2]
            assert context[0].content == "edited"
            assert context[0].sender_name is not None
            assert context[1].sender_user_id is None
            topic_context = await projection.fetch_before(
                GroupContextQuery(
                    GroupConversationScope(group_id, 23),
                    before_message_id=4,
                    limit=10,
                )
            )
            assert [message.message_id for message in topic_context] == [3]
            assert topic_context[0].sender_name == "Unregistered Speaker"
            assert topic_context[0].sender_username == "visitor"
            old_relation = await db.fetch_one(
                "SELECT to_regclass('conversation.chat_records_group')"
            )
            assert old_relation is not None and old_relation[0] is None
        finally:
            await db.execute(
                "DELETE FROM conversation.group_message_projection WHERE group_id = %s",
                (group_id,),
            )
            await db.execute(
                "DELETE FROM identity.users WHERE id = %s",
                (user_id,),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
