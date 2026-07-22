"""@brief Assistant Memory 工具的隔离与瞬时结果测试 / Tests for Assistant Memory-tool isolation and ephemeral results."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.memory.ports import WorkingMemoryQuery
from fogmoe_bot.domain.context.token_estimator import estimate_tokens
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.memory import (
    WorkingMemory,
    WorkingMemoryAvailability,
    WorkingMemoryMessage,
)
from fogmoe_bot.infrastructure.assistant.tool_operations.memory import search_memory
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    PostgresAssistantToolStore,
    ToolTransactionMode,
)

NOW = datetime(2032, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 确定性来源时间 / Deterministic source instant."""


class _Memory:
    """@brief 记录强隔离 Query 的 WorkingMemory fake / WorkingMemory fake recording strongly scoped queries."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call records."""

        self.queries: list[WorkingMemoryQuery] = []

    async def retrieve(self, query: WorkingMemoryQuery) -> WorkingMemory:
        """@brief 返回一条带 provenance 的工作记忆 / Return one provenance-bearing memory message."""

        self.queries.append(query)
        message = WorkingMemoryMessage(
            passage_id=UUID("00000000-0000-0000-0000-000000000040"),
            source_kind="conversation.turn",
            source_id=UUID("00000000-0000-0000-0000-000000000041"),
            content="Time: 2032-01-02T03:04:00Z\nUser: 我喜欢红茶\nAssistant: 记住啦。",
            occurred_at=NOW,
            cosine_distance=0.125,
        )
        return WorkingMemory(query.scope, query.text, (message,))


def _request(*, group_id: int | None = None) -> ToolEffectRequest:
    """@brief 构造已校验工具请求 / Build a validated tool request."""

    return ToolEffectRequest(
        context=ToolExecutionContext(
            turn_id=TurnId.new(),
            conversation_id=ConversationId(
                "assistant-user:7"
                if group_id is None
                else f"assistant-group:{group_id}"
            ),
            delivery_stream_id=DeliveryStreamId("telegram:user:7"),
            user_id=7,
            chat_id=7,
            is_group=group_id is not None,
            group_id=group_id,
            message_id=9,
        ),
        invocation_id="step:0:call:0",
        provider_call_id="provider-recall-call",
        tool_name="search_memory",
        effect_kind="read.search_memory",
        mutating=False,
        arguments={"query": "我以前喜欢喝什么？", "limit": 3},
        request_hash="a" * 64,
    )


def test_memory_tool_derives_personal_scope_and_returns_provenance() -> None:
    """@brief 个人域只能来自授权上下文且结果保留来源 / Personal scope comes only from authorization context and results retain provenance."""

    async def scenario() -> None:
        """@brief 执行异步工具场景 / Execute the asynchronous tool scenario."""

        memory = _Memory()
        result = await search_memory(_request(), memory=memory)
        assert memory.queries == [
            WorkingMemoryQuery(memory.queries[0].scope, "我以前喜欢喝什么？", 3)
        ]
        assert memory.queries[0].scope.user_id == 7
        assert result == {
            "scope": {"kind": "personal", "id": 7},
            "query": "我以前喜欢喝什么？",
            "availability": "available",
            "trust": "untrusted_historical_data",
            "results": [
                {
                    "passage_id": "00000000-0000-0000-0000-000000000040",
                    "source_kind": "conversation.turn",
                    "source_id": "00000000-0000-0000-0000-000000000041",
                    "occurred_at": "2032-01-02T03:04:00+00:00",
                    "content": (
                        "Time: 2032-01-02T03:04:00Z\n"
                        "User: 我喜欢红茶\nAssistant: 记住啦。"
                    ),
                    "cosine_distance": 0.125,
                }
            ],
        }

    asyncio.run(scenario())


def test_memory_tool_exposes_unavailable_instead_of_claiming_empty_recall() -> None:
    """@brief Tool payload 区分依赖不可用与成功空结果 / Tool payload distinguishes dependency unavailability from successful emptiness."""

    class _UnavailableMemory:
        """@brief 返回 typed unavailable WorkingMemory / Return typed unavailable WorkingMemory."""

        async def retrieve(self, query: WorkingMemoryQuery) -> WorkingMemory:
            """@brief 保留 scope/query 并标记不可用 / Preserve scope/query and mark unavailable."""

            return WorkingMemory(
                query.scope,
                query.text,
                (),
                WorkingMemoryAvailability.UNAVAILABLE,
            )

    async def scenario() -> None:
        """@brief 执行一次不可用工具召回 / Execute one unavailable tool recall."""

        result = await search_memory(_request(), memory=_UnavailableMemory())
        assert result == {
            "scope": {"kind": "personal", "id": 7},
            "query": "我以前喜欢喝什么？",
            "availability": "unavailable",
            "trust": "untrusted_historical_data",
            "results": [],
        }

    asyncio.run(scenario())


def test_memory_tool_derives_the_current_group_scope() -> None:
    """@brief 群聊只能检索当前群域 / A group chat can retrieve only its current group scope."""

    async def scenario() -> None:
        """@brief 验证群 ID 不来自模型参数 / Verify the group ID does not come from model arguments."""

        memory = _Memory()
        result = await search_memory(_request(group_id=-10042), memory=memory)
        assert memory.queries[0].scope.group_id == -10042
        assert result["scope"] == {"kind": "group", "id": -10042}

    asyncio.run(scenario())


def test_memory_tool_result_bypasses_durable_receipts(monkeypatch) -> None:
    """@brief Memory tool 每次 fresh 执行且结果不写 receipt / The Memory tool executes fresh and never writes a receipt."""

    class _Operations:
        """@brief 记录非缓存 operation / Record a non-cacheable operation."""

        calls = 0

        def transaction_mode(self, request):
            """@brief 指定事务外读取 / Select an outside-transaction read."""

            return ToolTransactionMode.OUTSIDE_TRANSACTION

        async def execute(self, request, *, connection):
            """@brief 每次产生一个不同结果 / Produce a distinct result every time."""

            assert connection is None
            self.calls += 1
            return {"fresh": self.calls}

        async def finalize(self, request, result, *, connection):
            """@brief 瞬时结果不得 finalize / Volatile results must not finalize."""

            raise AssertionError((request, result, connection))

    async def reject_database(*args, **kwargs):
        """@brief 发现 receipt SQL 即失败 / Fail upon any receipt SQL."""

        raise AssertionError((args, kwargs))

    async def scenario() -> None:
        """@brief 连续执行相同 invocation / Execute the same invocation twice."""

        monkeypatch.setattr(db, "fetch_one", reject_database)
        monkeypatch.setattr(db, "execute", reject_database)
        operations = _Operations()
        store = PostgresAssistantToolStore(operations=operations)
        request = replace(_request(), result_cacheable=False)
        first = await store.execute(request)
        second = await store.execute(request)
        assert first.result == {"fresh": 1}
        assert second.result == {"fresh": 2}
        assert first.replayed is second.replayed is False

    asyncio.run(scenario())


def test_memory_tool_result_has_an_independent_hard_budget() -> None:
    """@brief 显式 Memory tool 不能用大结果挤爆后续模型 Query / A Memory tool cannot overflow the next model query with a large result."""

    class _LargeMemory:
        """@brief 返回六条超长消息 / Return six oversized messages."""

        async def retrieve(self, query: WorkingMemoryQuery) -> WorkingMemory:
            """@brief 构造超长工作记忆 / Build oversized working memory."""

            return WorkingMemory(
                query.scope,
                query.text,
                tuple(
                    WorkingMemoryMessage(
                        passage_id=UUID(f"30000000-0000-0000-0000-{index:012d}"),
                        source_kind="conversation.turn",
                        source_id=UUID(f"40000000-0000-0000-0000-{index:012d}"),
                        occurred_at=NOW,
                        content="敏感历史" * 2_000,
                        cosine_distance=index / 100,
                    )
                    for index in range(1, 7)
                ),
            )

    async def scenario() -> None:
        """@brief 验证 JSON 总体预算与截断标记 / Verify the total JSON budget and truncation marker."""

        request = replace(
            _request(),
            arguments={"query": "我以前喜欢喝什么？", "limit": 6},
        )
        result = await search_memory(request, memory=_LargeMemory())
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        assert estimate_tokens(encoded) <= 4_096
        assert any(item.get("truncated") is True for item in result["results"])

    asyncio.run(scenario())
