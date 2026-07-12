"""@brief Assistant 群上下文工具测试 / Tests for the Assistant group-context tool."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.chat.group_messages import GroupMessage, GroupMessageKind
from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.infrastructure.assistant.tool_operations.dispatcher import (
    AssistantToolOperationDispatcher,
)
from fogmoe_bot.infrastructure.database.conversation_retention import (
    PostgresConversationRetention,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)


NOW = datetime(2026, 7, 12, tzinfo=UTC)


class _Groups:
    """@brief 记录 canonical context 查询 / Record canonical context queries."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int | None, int]] = []

    async def project(self, observation: object) -> None:
        """@brief 本测试不写投影 / This test performs no projection writes."""

        raise AssertionError(observation)

    async def fetch_before(
        self,
        group_id: int,
        *,
        before_message_id: int | None,
        limit: int,
    ) -> tuple[GroupMessage, ...]:
        """@brief 返回一条规范消息 / Return one canonical message."""

        self.calls.append((group_id, before_message_id, limit))
        return (
            GroupMessage(
                group_id,
                8,
                42,
                "Klee",
                GroupMessageKind.TEXT,
                "durable context",
                NOW,
                True,
            ),
        )


class _UnusedAdapters:
    """@brief 拒绝意外外部工具调用 / Reject unexpected external-tool calls."""

    async def execute(self, request: ToolEffectRequest) -> JsonValue:
        raise AssertionError(request.tool_name)

    async def generate(self, request: ToolEffectRequest) -> JsonValue:
        raise AssertionError(request.tool_name)

    async def list_packs(self, pack_name: str | None) -> JsonValue:
        raise AssertionError(pack_name)


def _request(*, is_group: bool) -> ToolEffectRequest:
    """@brief 构造群或私聊工具请求 / Build a group or private tool request."""

    return ToolEffectRequest(
        context=ToolExecutionContext(
            turn_id=TurnId.new(),
            conversation_id=ConversationId("assistant-user:42"),
            delivery_stream_id=DeliveryStreamId("telegram:test:-1001"),
            user_id=42,
            chat_id=-1001 if is_group else 42,
            is_group=is_group,
            group_id=-1001 if is_group else None,
            message_id=10,
        ),
        invocation_id="step:0:call:0",
        provider_call_id="provider-group-call",
        tool_name="fetch_group_context",
        effect_kind="read.fetch_group_context",
        mutating=False,
        arguments={"window_size": 7},
        request_hash="b" * 64,
    )


def test_group_context_tool_reads_only_the_canonical_projection() -> None:
    """@brief 工具经 typed port 读取当前消息之前的 canonical window / The tool reads the canonical pre-message window through its typed port."""

    async def scenario() -> None:
        groups = _Groups()
        unused = _UnusedAdapters()
        operations = AssistantToolOperationDispatcher(
            help_text="help",
            external_reads=unused,
            generated_media=unused,
            stickers=unused,
            outbox=PostgresOutboxRepository(),
            memory=PostgresConversationRetention(),
            groups=groups,
        )
        result = await operations.execute(_request(is_group=True), connection=None)
        assert groups.calls == [(-1001, 10, 7)]
        assert result == {
            "group_id": -1001,
            "before_message_id": 10,
            "window_size": 7,
            "messages": [
                {
                    "message_id": 8,
                    "user_id": 42,
                    "username": "Klee",
                    "message_type": "text",
                    "content": "durable context",
                    "created_at": NOW.isoformat(),
                    "edited": True,
                }
            ],
        }

        rejected = await operations.execute(_request(is_group=False), connection=None)
        assert rejected == {"error": "This tool is available only in a group chat"}
        assert groups.calls == [(-1001, 10, 7)]

    asyncio.run(scenario())
