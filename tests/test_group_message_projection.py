"""@brief 群消息投影 mapper、observer 与 reader 测试 / Tests for the group-message mapper, observer, and reader."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fogmoe_bot.application.chat.group_messages import (
    GroupMessageKind,
    GroupMessageObservation,
)
from fogmoe_bot.application.conversation.router import RoutedOperation
from fogmoe_bot.application.runtime import AggregateKey
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.infrastructure.database import (
    group_message_projection as postgres_module,
)
from fogmoe_bot.infrastructure.database.group_message_projection import (
    PostgresGroupMessageProjection,
)
from fogmoe_bot.presentation.telegram.group_message_observer import (
    GroupMessageIngressObserver,
    TelegramObserverPipeline,
    extract_group_message_observation,
)


NOW = datetime(2026, 7, 12, tzinfo=UTC)


def _update(
    update_id: int,
    *,
    message_id: int = 10,
    edited: bool = False,
    content: dict[str, object] | None = None,
) -> InboundUpdate:
    """@brief 构造 durable 群 Update / Build a durable group Update."""

    message: dict[str, object] = {
        "message_id": message_id,
        "date": int(NOW.timestamp()),
        "chat": {"id": -1001, "type": "supergroup", "title": "Klee Lab"},
        "from": {"id": 42, "is_bot": False, "first_name": "Klee"},
        **(content or {"text": "hello"}),
    }
    if edited:
        message["edit_date"] = int(NOW.timestamp()) + 5
    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:42"),
        payload={
            "update_id": update_id,
            "edited_message" if edited else "message": message,
        },
        received_at=NOW,
    )


class _Projection:
    """@brief 以内存 canonical key 模拟 DB 端口 / Simulate the DB port with an in-memory canonical key."""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, int], GroupMessageObservation] = {}

    async def project(self, observation: GroupMessageObservation) -> None:
        """@brief 仅接受更大的 Update ID / Accept only a greater Update ID."""

        key = (observation.group_id, observation.message_id)
        current = self.rows.get(key)
        if current is None or observation.source_update_id > current.source_update_id:
            self.rows[key] = observation

    async def fetch_before(
        self,
        group_id: int,
        *,
        message_thread_id: int | None,
        before_message_id: int | None,
        limit: int,
    ) -> tuple[()]:
        """@brief 本测试不读取 / This test performs no reads."""

        del group_id, message_thread_id, before_message_id, limit
        return ()


def test_safe_mapper_extracts_text_edits_and_media_without_sdk_objects() -> None:
    """@brief mapper 直接从 JSON 抽取文本、编辑与媒体 / The mapper extracts text, edits, and media directly from JSON."""

    text = extract_group_message_observation(_update(1))
    edited = extract_group_message_observation(
        _update(2, edited=True, content={"text": "edited"})
    )
    sticker = extract_group_message_observation(
        _update(
            3,
            message_id=11,
            content={
                "sticker": {
                    "file_id": "opaque",
                    "file_unique_id": "stable",
                    "emoji": "🔥",
                }
            },
        )
    )

    assert text is not None and text.kind is GroupMessageKind.TEXT
    assert text.content == "hello" and text.group_id == -1001
    assert edited is not None and edited.edited and edited.content == "edited"
    assert edited.updated_at > edited.created_at
    assert sticker is not None and sticker.kind is GroupMessageKind.STICKER
    assert sticker.content == "🔥"

    malformed = _update(4)
    malformed.payload["update_id"] = 999
    assert extract_group_message_observation(malformed) is None


def test_observer_replay_converges_to_one_canonical_message() -> None:
    """@brief inbox replay 与乱序旧 Update 收敛到一个 canonical row / Inbox replay and an older out-of-order Update converge to one canonical row."""

    async def scenario() -> None:
        projection = _Projection()
        observer = GroupMessageIngressObserver(projection)
        original = _update(10, content={"text": "old"})
        edit = _update(11, edited=True, content={"text": "new"})
        for update in (original, original, edit, original):
            operation = await observer.operation(update, primary_route=None)
            assert operation is not None
            await operation.call()
        assert len(projection.rows) == 1
        canonical = projection.rows[(-1001, 10)]
        assert canonical.source_update_id == 11
        assert canonical.content == "new"

    asyncio.run(scenario())


def test_pipeline_commits_projection_before_other_observer_effects() -> None:
    """@brief 组合接点先写 DB 投影再运行原 observer / The composite hook writes the DB projection before the existing observer."""

    async def scenario() -> None:
        order: list[str] = []
        projection = _Projection()

        class _RecordingProjection(GroupMessageIngressObserver):
            async def operation(
                self,
                update: InboundUpdate,
                *,
                primary_route: str | None,
            ) -> RoutedOperation | None:
                operation = await super().operation(
                    update,
                    primary_route=primary_route,
                )
                assert operation is not None

                async def call() -> None:
                    order.append("projection")
                    await operation.call()

                return RoutedOperation(operation.name, operation.key, call)

        class _OtherObserver:
            @property
            def name(self) -> str:
                return "other"

            async def operation(
                self,
                update: InboundUpdate,
                *,
                primary_route: str | None,
            ) -> RoutedOperation:
                del update, primary_route

                async def call() -> None:
                    order.append("other")

                return RoutedOperation(
                    "other",
                    AggregateKey.of("other-group", -1001),
                    call,
                )

        pipeline = TelegramObserverPipeline(
            (_RecordingProjection(projection), _OtherObserver())
        )
        operation = await pipeline.operation(_update(1), primary_route="primary")
        assert operation is not None
        await operation.call()
        assert order == ["projection", "other"]

    asyncio.run(scenario())


def test_postgres_reader_filters_canonical_rows_and_decodes_legacy_base64(
    monkeypatch: Any,
) -> None:
    """@brief reader 只查 canonical row 且显式解码 legacy base64 / The reader selects canonical rows and explicitly decodes legacy base64."""

    async def scenario() -> None:
        captured: list[str] = []

        async def fake_fetch_all(
            sql: str,
            params: object,
            *,
            mapping: bool,
        ) -> list[dict[str, object]]:
            del params
            assert mapping is True
            captured.append(sql)
            return [
                {
                    "group_id": -1001,
                    "message_id": 9,
                    "user_id": 42,
                    "message_thread_id": None,
                    "message_type": "sticker",
                    "content": base64.b64encode("🔥".encode()).decode(),
                    "content_encoding": "base64",
                    "created_at": NOW,
                    "is_edited": False,
                    "sender_name": "Klee",
                    "sender_username": "klee",
                }
            ]

        monkeypatch.setattr(postgres_module.db_connection, "fetch_all", fake_fetch_all)
        messages = await PostgresGroupMessageProjection().fetch_before(
            -1001,
            message_thread_id=None,
            before_message_id=10,
            limit=5,
        )
        assert len(messages) == 1 and messages[0].content == "🔥"
        assert "projection.is_canonical" in captured[0]
        assert "projection.message_id <" in captured[0]

    asyncio.run(scenario())


def test_migration_is_lossless_and_removes_the_legacy_relation_name() -> None:
    """@brief 0035 声明原位 rename、duplicate 保留与可逆 encoding / 0035 declares in-place rename, duplicate preservation, and reversible encoding."""

    migration = (
        Path(__file__).resolve().parents[1]
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0035_group_message_projection.sql"
    ).read_text(encoding="utf-8")
    assert "ALTER TABLE conversation.chat_records_group" in migration
    assert "RENAME TO group_message_projection" in migration
    assert "row_number() OVER" in migration
    assert "WHERE is_canonical" in migration
    assert "ON CONFLICT (group_id, message_id)" not in migration
    assert "RENAME TO chat_records_group" in migration

    admin_source = (
        Path(__file__).resolve().parents[1]
        / "src/fogmoe_bot/infrastructure/admin/announcements.py"
    ).read_text(encoding="utf-8")
    assert "conversation.group_message_projection" in admin_source
    assert "conversation.chat_records_group" not in admin_source
