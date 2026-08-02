"""@brief Codex 式 durable Agent 过程项测试 / Tests for Codex-style durable Agent progress items."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
from conversation_workflow_testkit import _TransactionContext

from fogmoe_bot.application.assistant.progress import (
    AssistantProgressItem,
    AssistantProgressKind,
    commentary_progress_item,
    tool_progress_item,
    tool_started_progress_item,
)
from fogmoe_bot.application.assistant.tool_runtime import ToolExecutionContext
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    InferenceActivityId,
    LeaseToken,
    OutboundMessageId,
    TurnId,
    TurnRevision,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceGenerationCause,
    InferenceGenerationFence,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_ASSISTANT_PROGRESS,
    OutboundDraft,
)
from fogmoe_bot.infrastructure.database import assistant_tool_effects, db
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    AssistantProgressOutboxWriter,
    AssistantToolOperations,
    PostgresAssistantToolStore,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 确定性测试时刻 / Deterministic test instant."""


class _ProgressOutbox:
    """@brief 记录 generation-fenced progress drafts / Record generation-fenced progress drafts."""

    def __init__(self) -> None:
        """@brief 初始化草稿日志 / Initialize the draft log."""

        self.drafts: list[OutboundDraft] = []

    async def enqueue_outbound_in_transaction(
        self,
        connection: object,
        draft: OutboundDraft,
    ) -> object:
        """@brief 记录同事务草稿 / Record a draft from the same transaction.

        @param connection 当前事务连接 / Current transaction connection.
        @param draft 稳定过程草稿 / Stable progress draft.
        @return 未使用占位值 / Unused placeholder.
        """

        assert connection is _CONNECTION
        self.drafts.append(draft)
        return object()


_CONNECTION = object()
"""@brief 模拟事务连接 / Fake transaction connection."""


def _context(*, revision: int = 0) -> ToolExecutionContext:
    """@brief 构造带 generation fence 的工具上下文 / Build a generation-fenced tool context.

    @param revision steer 输入版本 / Steer input revision.
    @return durable 工具上下文 / Durable tool context.
    """

    turn_id = TurnId.parse("00000000-0000-4000-8000-000000000081")
    return ToolExecutionContext(
        turn_id=turn_id,
        conversation_id=ConversationId("assistant-user:42"),
        delivery_stream_id=DeliveryStreamId("telegram:chat:42"),
        user_id=42,
        chat_id=42,
        is_group=False,
        group_id=None,
        message_id=9,
        generation_fence=InferenceGenerationFence(
            activity_id=InferenceActivityId.for_turn(turn_id),
            turn_id=turn_id,
            claim_token=LeaseToken.new(),
            attempt=revision + 1,
            input_revision=TurnRevision(revision),
            cause=(
                InferenceGenerationCause.INITIAL
                if revision == 0
                else InferenceGenerationCause.STEER
            ),
        ),
    )


def test_progress_items_are_bounded_public_and_stably_identified() -> None:
    """@brief commentary 与工具项有界、公开且身份稳定 /
    Commentary and tool items are bounded, public, and stably identified.
    """

    commentary = commentary_progress_item(
        step=2,
        text="  我先核对两处资料，再回来汇总。  ",
        created_at=NOW,
    )
    tool = tool_progress_item(
        invocation_id="generation:1:step:2:call:0",
        tool_name="google_search",
        succeeded=True,
        created_at=NOW,
    )
    tool_started = tool_started_progress_item(
        invocation_id="generation:1:step:2:call:0",
        tool_name="google_search",
        created_at=NOW,
    )

    assert commentary == AssistantProgressItem(
        item_id="step:2:commentary",
        kind=AssistantProgressKind.COMMENTARY,
        text="我先核对两处资料，再回来汇总。",
        created_at=NOW,
    )
    assert tool.item_id == "tool:generation:1:step:2:call:0:finished"
    assert tool.replaces_item_id == "tool:generation:1:step:2:call:0:started"
    assert tool.text == "✓ 网上资料查完啦"
    assert tool_started.item_id == "tool:generation:1:step:2:call:0:started"
    assert tool_started.text == "✦ 我去网上查查最新资料…"
    assert "arguments" not in tool.text
    with pytest.raises(ValueError, match="item_id"):
        AssistantProgressItem(
            item_id="../internal",
            kind=AssistantProgressKind.COMMENTARY,
            text="unsafe",
            created_at=NOW,
        )


def test_progress_publisher_fences_generation_and_replays_same_outbound_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief progress 发布受 generation fence 保护且重放身份不变 /
    Progress publication is generation-fenced and replay keeps the same outbound identity.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 发布同一 commentary 两次 / Publish the same commentary twice."""

        outbox = _ProgressOutbox()
        observed_fences: list[InferenceGenerationFence] = []

        async def fake_assert_current_generation(
            fence: InferenceGenerationFence,
            *,
            connection: object | None = None,
        ) -> None:
            """@brief 记录 fence 与事务连接 / Record the fence and transaction connection."""

            assert connection is _CONNECTION
            observed_fences.append(fence)

        monkeypatch.setattr(
            db,
            "transaction",
            lambda: _TransactionContext(_CONNECTION),
        )
        monkeypatch.setattr(
            assistant_tool_effects,
            "_assert_current_generation",
            fake_assert_current_generation,
        )
        store = PostgresAssistantToolStore(
            operations=cast(AssistantToolOperations, object()),
            progress_outbox=cast(AssistantProgressOutboxWriter, outbox),
        )
        context = _context()
        item = commentary_progress_item(step=0, text="我先查一下。", created_at=NOW)
        terminal = tool_progress_item(
            invocation_id="generation:1:step:0:call:0",
            tool_name="google_search",
            succeeded=True,
            created_at=NOW,
        )

        await store.publish_progress(context, item)
        await store.publish_progress(context, item)
        await store.publish_progress(context, terminal)

        assert observed_fences == [
            context.generation_fence,
            context.generation_fence,
            context.generation_fence,
        ]
        assert len(outbox.drafts) == 3
        first, replay, terminal_draft = outbox.drafts
        assert first.message_id == replay.message_id
        assert first.idempotency_key == replay.idempotency_key
        assert first.kind == SEND_TELEGRAM_ASSISTANT_PROGRESS
        assert first.turn_id == context.turn_id
        assert first.payload == {
            "chat_id": 42,
            "text": "我先查一下。",
            "disable_notification": True,
            "protect_content": False,
            "disable_web_page_preview": True,
            "reply_to_message_id": 9,
        }
        source_semantic_key = (
            "assistant.progress.generation.0."
            "tool:generation:1:step:0:call:0:started"
        )
        assert terminal_draft.payload == {
            "chat_id": 42,
            "text": "✓ 网上资料查完啦",
            "disable_web_page_preview": True,
            "source_outbound_id": str(
                OutboundMessageId.for_turn(context.turn_id, source_semantic_key)
            ),
        }

    asyncio.run(scenario())


def test_progress_publisher_requires_a_generation_fence() -> None:
    """@brief 无 generation fence 时拒绝 durable progress / Durable progress rejects a missing generation fence."""

    context = _context()
    unfenced = ToolExecutionContext(
        turn_id=context.turn_id,
        conversation_id=context.conversation_id,
        delivery_stream_id=context.delivery_stream_id,
        user_id=context.user_id,
        chat_id=context.chat_id,
        is_group=context.is_group,
        group_id=context.group_id,
        message_id=context.message_id,
    )
    store = PostgresAssistantToolStore(
        operations=cast(AssistantToolOperations, object())
    )
    item = commentary_progress_item(step=0, text="我先查一下。", created_at=NOW)

    with pytest.raises(ValueError, match="generation fence"):
        asyncio.run(store.publish_progress(unfenced, item))
