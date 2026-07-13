"""@brief Assistant 情景历史召回工具测试 / Tests for the Assistant episodic-history recall tool."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.retrieval import SemanticRecallQuery
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.retrieval import RetrievalEvidence, RetrievalPassage
from fogmoe_bot.infrastructure.assistant.tool_operations.retrieval import (
    recall_conversation_history,
)


NOW = datetime(2032, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 确定性来源时间 / Deterministic source instant."""


class _Recall:
    """@brief 记录强租户 Query 的语义召回 fake / Semantic-recall fake recording tenant-scoped queries."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call records."""

        self.queries: list[SemanticRecallQuery] = []

    async def recall(self, query: SemanticRecallQuery) -> tuple[RetrievalEvidence, ...]:
        """@brief 返回一条带 provenance 的证据 / Return one provenance-bearing evidence item."""

        self.queries.append(query)
        passage = RetrievalPassage.create(
            corpus_id="conversation.episodic",
            owner_user_id=query.owner_user_id,
            source_kind="conversation.turn",
            source_id=UUID("00000000-0000-0000-0000-000000000041"),
            ordinal=0,
            format_version=1,
            text="Time: 2032-01-02T03:04:00Z\nUser: 我喜欢红茶\nAssistant: 记住啦。",
            occurred_at=NOW,
        )
        return (RetrievalEvidence(passage, 0.125),)


def _request() -> ToolEffectRequest:
    """@brief 构造已校验工具请求 / Build a validated tool request."""

    return ToolEffectRequest(
        context=ToolExecutionContext(
            turn_id=TurnId.new(),
            conversation_id=ConversationId("assistant-user:7"),
            delivery_stream_id=DeliveryStreamId("telegram:user:7"),
            user_id=7,
            chat_id=7,
            is_group=False,
            group_id=None,
            message_id=9,
        ),
        invocation_id="step:0:call:0",
        provider_call_id="provider-recall-call",
        tool_name="recall_conversation_history",
        effect_kind="read.recall_conversation_history",
        mutating=False,
        arguments={"query": "我以前喜欢喝什么？", "limit": 3},
        request_hash="a" * 64,
    )


def test_recall_tool_enforces_authenticated_owner_and_returns_provenance() -> None:
    """@brief Query owner 只能来自授权上下文且结果保留来源 / Owner comes only from authorization context and results retain provenance."""

    async def scenario() -> None:
        """@brief 执行异步工具场景 / Execute the asynchronous tool scenario."""

        recall = _Recall()
        result = await recall_conversation_history(_request(), recall=recall)
        assert recall.queries == [
            SemanticRecallQuery(
                owner_user_id=7,
                text="我以前喜欢喝什么？",
                limit=3,
            )
        ]
        assert result == {
            "user_id": 7,
            "query": "我以前喜欢喝什么？",
            "trust": "untrusted_historical_data",
            "results": [
                {
                    "source_turn_id": "00000000-0000-0000-0000-000000000041",
                    "occurred_at": "2032-01-02T03:04:00+00:00",
                    "excerpt": (
                        "Time: 2032-01-02T03:04:00Z\n"
                        "User: 我喜欢红茶\nAssistant: 记住啦。"
                    ),
                    "cosine_distance": 0.125,
                }
            ],
        }

    asyncio.run(scenario())
