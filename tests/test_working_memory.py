"""@brief WorkingMemory 的检索映射、预算与投影测试 / Tests for WorkingMemory retrieval mapping, budgeting, and projection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from fogmoe_bot.application.memory.ports import WorkingMemoryQuery
from fogmoe_bot.application.memory.rendering import (
    compose_model_messages,
    render_working_memory,
)
from fogmoe_bot.application.memory.service import RetrievalWorkingMemory
from fogmoe_bot.application.retrieval import (
    SemanticRecallQuery,
    SemanticRecallUnavailableError,
)
from fogmoe_bot.domain.context.token_estimator import estimate_message_tokens
from fogmoe_bot.domain.memory import (
    GroupMemoryScope,
    PersonalMemoryScope,
    WorkingMemory,
    WorkingMemoryAvailability,
    WorkingMemoryMessage,
)
from fogmoe_bot.domain.retrieval import (
    RetrievalEvidence,
    RetrievalPassage,
    RetrievalScope,
)

NOW = datetime(2036, 1, 1, tzinfo=UTC)
"""@brief 固定来源时刻 / Fixed source instant."""


def _message(*, ordinal: int, content: str) -> WorkingMemoryMessage:
    """@brief 构造工作记忆消息 / Build a WorkingMemory message.

    @param ordinal 稳定 ID 尾数 / Stable identity suffix.
    @param content 正文 / Content.
    @return 消息 / Message.
    """

    return WorkingMemoryMessage(
        passage_id=UUID(f"00000000-0000-0000-0000-{ordinal:012d}"),
        source_kind="conversation.turn",
        source_id=UUID(f"10000000-0000-0000-0000-{ordinal:012d}"),
        occurred_at=NOW,
        content=content,
        cosine_distance=ordinal / 100,
    )


def test_working_memory_projection_is_single_untrusted_and_hard_bounded() -> None:
    """@brief WorkingMemory 只注入一次且严格服从独立 token 预算 / WorkingMemory is injected once and obeys its independent token budget."""

    memory = WorkingMemory(
        scope=PersonalMemoryScope(7),
        query="以前讨论过什么？",
        messages=tuple(
            _message(ordinal=index, content="机密历史内容" * 1_000)
            for index in range(1, 5)
        ),
    )
    rendered = render_working_memory(memory, maximum_tokens=512)
    assert estimate_message_tokens((rendered,)) <= 512
    assert 'trust="untrusted_historical_data"' in rendered["content"]
    assert 'truncated="true"' in rendered["content"]

    projected = compose_model_messages(
        (
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "以前讨论过什么？"},
        ),
        memory,
        maximum_tokens=512,
    )
    assert projected[0] == {"role": "system", "content": "policy"}
    assert sum("<working_memory" in str(item.get("content")) for item in projected) == 1


def test_working_memory_count_cap_is_high_but_finite() -> None:
    """@brief WorkingMemory 允许大量短证据但仍拒绝无界结果 / WorkingMemory admits many short evidence rows while rejecting unbounded results."""

    scope = PersonalMemoryScope(7)
    query = WorkingMemoryQuery(scope, "query", 128)
    memory = WorkingMemory(
        scope,
        query.text,
        tuple(
            _message(ordinal=index, content=f"short {index}") for index in range(1, 129)
        ),
    )

    assert len(memory.messages) == 128
    with pytest.raises(ValueError, match="between 1 and 128"):
        WorkingMemoryQuery(scope, "query", 129)


def test_available_empty_is_distinct_from_unavailable_empty() -> None:
    """@brief 成功空结果与依赖不可用是不同领域状态 / Successful emptiness and dependency unavailability are distinct domain states."""

    scope = PersonalMemoryScope(7)
    available = WorkingMemory(scope, "query", ())
    unavailable = WorkingMemory(
        scope,
        "query",
        (),
        WorkingMemoryAvailability.UNAVAILABLE,
    )

    assert available.availability is WorkingMemoryAvailability.AVAILABLE
    assert unavailable.availability is WorkingMemoryAvailability.UNAVAILABLE
    available_projection = compose_model_messages(
        ({"role": "user", "content": "query"},), available
    )
    assert any(
        "<working_memory" in str(message.get("content"))
        for message in available_projection
    )
    assert compose_model_messages(
        ({"role": "user", "content": "query"},), unavailable
    ) == ({"role": "user", "content": "query"},)
    with pytest.raises(ValueError, match="cannot be rendered"):
        render_working_memory(unavailable)
    with pytest.raises(ValueError, match="cannot contain messages"):
        WorkingMemory(
            scope,
            "query",
            (_message(ordinal=1, content="evidence"),),
            WorkingMemoryAvailability.UNAVAILABLE,
        )


class _Recall:
    """@brief 记录通用 Retrieval 查询 / Record generic Retrieval queries."""

    def __init__(self) -> None:
        """@brief 初始化日志 / Initialize the log."""

        self.queries: list[SemanticRecallQuery] = []

    async def recall(self, query: SemanticRecallQuery) -> tuple[RetrievalEvidence, ...]:
        """@brief 返回与请求 scope 相同的证据 / Return evidence in the requested scope."""

        self.queries.append(query)
        passage = RetrievalPassage.create(
            corpus_id="conversation.episodic",
            scope=query.scope,
            source_kind="conversation.turn",
            source_id=UUID("20000000-0000-0000-0000-000000000001"),
            ordinal=0,
            format_version=1,
            text="historical evidence",
            occurred_at=NOW,
        )
        return (RetrievalEvidence(passage, 0.2),)


def test_working_memory_maps_personal_and_group_scopes_exhaustively() -> None:
    """@brief 产品 Memory scope 精确映射到通用 Retrieval scope / Product Memory scopes map exactly to generic Retrieval scopes."""

    async def scenario() -> None:
        """@brief 分别查询个人与两个群 / Query personal and two groups separately."""

        recall = _Recall()
        memory = RetrievalWorkingMemory(recall=recall)
        scopes = (
            PersonalMemoryScope(7),
            GroupMemoryScope(-1001),
            GroupMemoryScope(-1002),
        )
        for scope in scopes:
            result = await memory.retrieve(WorkingMemoryQuery(scope, "query", 2))
            assert result.scope == scope
            assert len(result.messages) == 1
            assert result.availability is WorkingMemoryAvailability.AVAILABLE
        assert [query.scope for query in recall.queries] == [
            RetrievalScope("personal", 7),
            RetrievalScope("group", -1001),
            RetrievalScope("group", -1002),
        ]

    asyncio.run(scenario())


class _UnavailableRecall:
    """@brief 显式报告 recall 依赖不可用 / Explicitly report an unavailable recall dependency."""

    async def recall(
        self,
        query: SemanticRecallQuery,
    ) -> tuple[RetrievalEvidence, ...]:
        """@brief 抛出 typed unavailable 错误 / Raise the typed unavailable error."""

        del query
        raise SemanticRecallUnavailableError("recall unavailable")


class _BuggyRecall:
    """@brief 模拟未分类程序错误 / Simulate an unclassified programming error."""

    async def recall(
        self,
        query: SemanticRecallQuery,
    ) -> tuple[RetrievalEvidence, ...]:
        """@brief 抛出普通 RuntimeError / Raise an ordinary RuntimeError."""

        del query
        raise RuntimeError("mapping bug")


def test_working_memory_downgrades_only_explicit_recall_unavailability() -> None:
    """@brief WorkingMemory 仅 fail-open 已分类可用性错误 / WorkingMemory fails open only for classified availability errors."""

    async def scenario() -> None:
        """@brief 对比 typed 故障与程序错误 / Compare a typed failure with a programming error."""

        query = WorkingMemoryQuery(PersonalMemoryScope(7), "query", 2)
        unavailable = await RetrievalWorkingMemory(
            recall=_UnavailableRecall()
        ).retrieve(query)
        assert unavailable.scope == query.scope
        assert unavailable.query == query.text
        assert unavailable.messages == ()
        assert unavailable.availability is WorkingMemoryAvailability.UNAVAILABLE

        with pytest.raises(RuntimeError, match="mapping bug"):
            await RetrievalWorkingMemory(recall=_BuggyRecall()).retrieve(query)

    asyncio.run(scenario())
