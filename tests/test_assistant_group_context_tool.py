"""@brief Assistant 群上下文工具测试 / Tests for the Assistant group-context tool."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.memory.ports import WorkingMemoryQuery
from fogmoe_bot.application.chat.group_messages import GroupMessage, GroupMessageKind
from fogmoe_bot.application.timekeeping.service import TimeService
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.memory.models import WorkingMemory
from fogmoe_bot.domain.temporal import UTC_TIME_ZONE
from fogmoe_bot.infrastructure.assistant.tool_operations.dispatcher import (
    AssistantToolOperationDispatcher,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)


NOW = datetime(2026, 7, 12, tzinfo=UTC)


class _Groups:
    """@brief 记录 canonical context 查询 / Record canonical context queries."""

    def __init__(self, messages: tuple[GroupMessage, ...] | None = None) -> None:
        """@brief 保存可配置返回消息 / Store configurable returned messages.

        @param messages 可选规范消息 / Optional canonical messages.
        """

        self.calls: list[tuple[int, int | None, int | None, int]] = []
        self.messages = messages

    async def project(self, observation: object) -> None:
        """@brief 本测试不写投影 / This test performs no projection writes."""

        raise AssertionError(observation)

    async def fetch_before(
        self,
        group_id: int,
        *,
        message_thread_id: int | None,
        before_message_id: int | None,
        limit: int,
    ) -> tuple[GroupMessage, ...]:
        """@brief 返回一条规范消息 / Return one canonical message."""

        self.calls.append((group_id, message_thread_id, before_message_id, limit))
        return self.messages or (
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

    async def retrieve(self, query: WorkingMemoryQuery) -> WorkingMemory:
        """@brief 返回空 WorkingMemory 以满足未调用端口 / Return empty WorkingMemory for the unused port.

        @param query 已验证 Query / Validated query.
        @return 空瞬时记忆 / Empty ephemeral memory.
        """

        return WorkingMemory(query.scope, query.text, ())


def _request(
    *,
    is_group: bool,
    message_thread_id: int | None = None,
    window_size: int | None = 7,
) -> ToolEffectRequest:
    """@brief 构造群或私聊工具请求 / Build a group or private tool request.

    @param is_group 是否群聊 / Whether this is a group chat.
    @param message_thread_id 可选 Topic ID / Optional topic identifier.
    @param window_size 可选显式条数；None 使用产品默认 / Optional explicit row count; None uses the product default.
    @return 工具效果请求 / Tool-effect request.
    """

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
            message_thread_id=message_thread_id,
        ),
        invocation_id="step:0:call:0",
        provider_call_id="provider-group-call",
        tool_name="fetch_group_context",
        effect_kind="read.fetch_group_context",
        mutating=False,
        arguments={} if window_size is None else {"window_size": window_size},
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
            memory=unused,
            groups=groups,
            time=TimeService(default_time_zone=UTC_TIME_ZONE),
        )
        result = await operations.execute(
            _request(is_group=True, message_thread_id=23), connection=None
        )
        assert groups.calls == [(-1001, 23, 10, 7)]
        assert result == {
            "group_id": -1001,
            "message_thread_id": 23,
            "before_message_id": 10,
            "trust": "untrusted_group_context",
            "omitted_older_messages": 0,
            "messages": [
                {
                    "message_id": 8,
                    "user_id": 42,
                    "username": None,
                    "display_name": "Klee",
                    "message_type": "text",
                    "content": "durable context",
                    "created_at": NOW.isoformat(),
                    "edited": True,
                    "truncated": False,
                }
            ],
        }

        await operations.execute(
            _request(
                is_group=True,
                message_thread_id=23,
                window_size=None,
            ),
            connection=None,
        )
        assert groups.calls[-1] == (-1001, 23, 10, 256)

        rejected = await operations.execute(_request(is_group=False), connection=None)
        assert rejected == {"error": "This tool is available only in a group chat"}
        assert groups.calls == [(-1001, 23, 10, 7), (-1001, 23, 10, 256)]

    asyncio.run(scenario())


def test_group_context_tool_keeps_a_recent_suffix_inside_its_hard_budget() -> None:
    """@brief 无界大消息被截断且更新消息优先保留 / An unbounded large message is truncated while newer messages are retained."""

    async def scenario() -> None:
        """@brief 请求超大群上下文页 / Request an oversized group-context page."""

        messages = tuple(
            GroupMessage(
                -1001,
                message_id,
                41,
                "Alice",
                GroupMessageKind.TEXT,
                "x" * 20_000,
                NOW,
                False,
                message_thread_id=23,
            )
            for message_id in range(4, 8)
        ) + (
            GroupMessage(
                -1001,
                8,
                42,
                "Klee",
                GroupMessageKind.TEXT,
                "newest",
                NOW,
                False,
                message_thread_id=23,
            ),
        )
        groups = _Groups(messages)
        unused = _UnusedAdapters()
        operations = AssistantToolOperationDispatcher(
            help_text="help",
            external_reads=unused,
            generated_media=unused,
            stickers=unused,
            outbox=PostgresOutboxRepository(),
            memory=unused,
            groups=groups,
            time=TimeService(default_time_zone=UTC_TIME_ZONE),
        )

        result = await operations.execute(
            _request(is_group=True, message_thread_id=23), connection=None
        )

        assert isinstance(result, dict)
        raw_entries = result["messages"]
        assert isinstance(raw_entries, list)
        assert all(isinstance(entry, dict) for entry in raw_entries)
        entries = cast(list[JsonObject], raw_entries)
        assert entries[-1]["content"] == "newest"
        assert entries[0]["truncated"] is True
        oldest_content = entries[0]["content"]
        assert isinstance(oldest_content, str)
        assert len(oldest_content) < 20_000
        assert result["omitted_older_messages"] == 1

    asyncio.run(scenario())
