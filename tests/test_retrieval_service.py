"""@brief Semantic Recall 性能遥测测试 / Semantic-Recall performance telemetry tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.application.retrieval import SemanticRecall, SemanticRecallQuery
from fogmoe_bot.domain.observability.signals import MetricSignal, SpanSignal
from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    EmbeddingVector,
    RetrievalEvidence,
    RetrievalPassage,
)


class _Embeddings:
    """@brief 固定 Query vector provider / Fixed query-vector provider."""

    async def embed_query(
        self,
        text: str,
        *,
        space: EmbeddingSpace,
    ) -> EmbeddingVector:
        """@brief 返回二维向量 / Return a two-dimensional vector."""

        assert text == "tea"
        assert space.dimensions == 2
        return EmbeddingVector((1.0, 0.0))


class _Store:
    """@brief 固定一条证据的检索 store / Retrieval store returning one evidence item."""

    async def search(
        self,
        *,
        owner_user_id: int,
        corpus_id: str,
        space: EmbeddingSpace,
        query_vector: EmbeddingVector,
        limit: int,
    ) -> tuple[RetrievalEvidence, ...]:
        """@brief 返回带 provenance 的证据 / Return provenance-bearing evidence."""

        assert (owner_user_id, corpus_id, limit) == (7, "conversation.episodic", 9)
        query_vector.require_space(space)
        passage = RetrievalPassage.create(
            corpus_id=corpus_id,
            owner_user_id=owner_user_id,
            source_kind="conversation.turn",
            source_id=UUID("00000000-0000-0000-0000-000000000077"),
            ordinal=0,
            format_version=1,
            text="User: tea",
            occurred_at=datetime(2035, 1, 1, tzinfo=UTC),
        )
        return (RetrievalEvidence(passage, 0.1),)


def test_semantic_recall_emits_hierarchical_performance_signals() -> None:
    """@brief Recall 产生 embedding/search 子 Span 与 outcome / Recall emits child spans and outcome."""

    async def scenario() -> None:
        """@brief 执行 Recall 并检查遥测 / Execute recall and inspect telemetry."""

        buffer = TelemetryBuffer(32)
        space = EmbeddingSpace(
            "test.recall.v1",
            "test/model",
            2,
            "Retrieve relevant evidence.",
            1,
        )
        recall = SemanticRecall(
            embeddings=_Embeddings(),  # type: ignore[arg-type]
            store=_Store(),  # type: ignore[arg-type]
            space=space,
            corpus_id="conversation.episodic",
            telemetry=Telemetry(buffer),
        )

        evidence = await recall.recall(SemanticRecallQuery(7, "tea", 3))

        assert len(evidence) == 1
        signals = buffer.drain(32)
        spans = tuple(signal for signal in signals if isinstance(signal, SpanSignal))
        metrics = tuple(
            signal for signal in signals if isinstance(signal, MetricSignal)
        )
        assert [span.name for span in spans] == [
            "retrieval.query.embedding",
            "retrieval.search",
            "retrieval.recall",
        ]
        root = spans[-1]
        assert all(span.parent_span_id == root.span_id for span in spans[:-1])
        assert spans[1].attributes["retrieval.candidate.count"] == 1
        assert root.attributes["retrieval.result.count"] == 1
        assert metrics[0].name == "fogmoe.retrieval.outcomes"
        assert metrics[0].attributes == {
            "operation": "recall",
            "outcome": "success",
        }

    asyncio.run(scenario())
