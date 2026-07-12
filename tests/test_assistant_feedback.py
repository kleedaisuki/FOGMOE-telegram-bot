"""@brief Assistant standalone feedback adapter 测试 / Tests for the standalone Assistant feedback adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    OutboundMessageId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)
from fogmoe_bot.infrastructure.database.standalone_outbound import (
    PostgresStandaloneOutboundCapability,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定测试时刻 / Fixed test instant."""


class RecordingStandaloneRepository:
    """@brief 记录 standalone drafts 的仓储替身 / Repository double recording standalone drafts."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        """@brief 配置可选失败 / Configure an optional failure.

        @param failure 入队时抛出的异常 / Error raised while enqueuing.
        """

        self.failure = failure
        """@brief 可选仓储失败 / Optional repository failure."""
        self.drafts: list[OutboundDraft] = []
        """@brief 收到的草稿 / Received drafts."""

    async def enqueue_standalone_outbound(self, draft: OutboundDraft) -> object:
        """@brief 记录草稿或传播失败 / Record the draft or propagate failure.

        @param draft standalone outbox 草稿 / Standalone outbox draft.
        @return 未使用的测试回执 / Unused test receipt.
        """

        self.drafts.append(draft)
        if self.failure is not None:
            raise self.failure
        return object()


def _command() -> StandaloneOutboundCommand:
    """@brief 构造确定性反馈命令 / Build a deterministic feedback command.

    @return feedback command / Feedback command.
    """

    return StandaloneOutboundCommand(
        conversation_id=ConversationId("assistant-user:42"),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
        kind=SEND_TELEGRAM_MESSAGE,
        payload={"chat_id": 42, "text": "register first"},
        idempotency_key="update:99:assistant-feedback:user_not_registered",
        created_at=NOW,
    )


def test_feedback_capability_maps_command_to_deterministic_standalone_draft() -> None:
    """@brief typed command 精确映射为确定性、无 Turn 的草稿 / A typed command maps exactly to a deterministic, Turn-less draft."""

    repository = RecordingStandaloneRepository()
    capability = PostgresStandaloneOutboundCapability(repository=repository)
    command = _command()

    asyncio.run(capability.enqueue(command))

    assert len(repository.drafts) == 1
    draft = repository.drafts[0]
    assert draft.message_id == OutboundMessageId.for_conversation(
        command.conversation_id,
        command.idempotency_key,
    )
    assert draft.turn_id is None
    assert draft.conversation_id == command.conversation_id
    assert draft.delivery_stream_id == command.delivery_stream_id
    assert draft.kind == command.kind
    assert draft.payload == command.payload
    assert draft.idempotency_key == command.idempotency_key
    assert draft.created_at == command.created_at


def test_feedback_capability_propagates_repository_failure() -> None:
    """@brief 仓储失败原样传播，使 inbox 可以重试 / Repository failure propagates so the inbox can retry."""

    repository = RecordingStandaloneRepository(failure=RuntimeError("database down"))
    capability = PostgresStandaloneOutboundCapability(repository=repository)

    with pytest.raises(RuntimeError, match="database down"):
        asyncio.run(capability.enqueue(_command()))

    assert len(repository.drafts) == 1
