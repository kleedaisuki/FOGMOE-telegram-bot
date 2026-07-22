"""@brief Semantic Recall 性能遥测测试 / Semantic-Recall performance telemetry tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.application.retrieval import (
    RetryableEmbeddingError,
    SemanticRecall,
    SemanticRecallQuery,
    SemanticRecallUnavailableError,
)
from fogmoe_bot.application.runtime import FailureCircuit, FailureCircuitPolicy
from fogmoe_bot.domain.observability.signals import MetricSignal, SpanSignal
from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    EmbeddingVector,
    RetrievalEvidence,
    RetrievalPassage,
    RetrievalScope,
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
        scope: RetrievalScope,
        corpus_id: str,
        space: EmbeddingSpace,
        query_vector: EmbeddingVector,
        limit: int,
    ) -> tuple[RetrievalEvidence, ...]:
        """@brief 返回带 provenance 的证据 / Return provenance-bearing evidence."""

        assert (scope, corpus_id, limit) == (
            RetrievalScope("personal", 7),
            "conversation.episodic",
            9,
        )
        query_vector.require_space(space)
        passage = RetrievalPassage.create(
            corpus_id=corpus_id,
            scope=scope,
            source_kind="conversation.turn",
            source_id=UUID("00000000-0000-0000-0000-000000000077"),
            ordinal=0,
            format_version=1,
            text="User: tea",
            occurred_at=datetime(2035, 1, 1, tzinfo=UTC),
        )
        return (RetrievalEvidence(passage, 0.1),)


class _Clock:
    """@brief 可控断路单调时钟 / Controllable circuit monotonic clock."""

    def __init__(self) -> None:
        """@brief 从固定时刻开始 / Start at a fixed instant."""

        self.now = 100.0

    def __call__(self) -> float:
        """@brief 返回当前单调秒数 / Return current monotonic seconds.

        @return 单调秒数 / Monotonic seconds.
        """

        return self.now

    def advance(self, seconds: float) -> None:
        """@brief 推进单调时钟 / Advance the monotonic clock.

        @param seconds 推进秒数 / Seconds to advance.
        @return None / None.
        """

        self.now += seconds


def _circuit(clock: _Clock) -> FailureCircuit[tuple[str, str]]:
    """@brief 构造一次失败即打开的 recall 断路器 / Build a recall circuit opening on one failure.

    @param clock 可控单调时钟 / Controllable monotonic clock.
    @return recall 断路器 / Recall circuit.
    """

    return FailureCircuit[tuple[str, str]](
        FailureCircuitPolicy(
            failure_threshold=1,
            failure_window_seconds=60.0,
            cooldown_seconds=60.0,
        ),
        monotonic=clock,
    )


def _space() -> EmbeddingSpace:
    """@brief 构造测试嵌入空间 / Build the test embedding space.

    @return 二维空间 / Two-dimensional space.
    """

    return EmbeddingSpace(
        "test.recall.v1",
        "test/model",
        2,
        "Retrieve relevant evidence.",
        1,
    )


def test_semantic_recall_emits_hierarchical_performance_signals() -> None:
    """@brief Recall 产生 embedding/search 子 Span 与 outcome / Recall emits child spans and outcome."""

    async def scenario() -> None:
        """@brief 执行 Recall 并检查遥测 / Execute recall and inspect telemetry."""

        buffer = TelemetryBuffer(32)
        space = _space()
        clock = _Clock()
        recall = SemanticRecall(
            embeddings=_Embeddings(),  # type: ignore[arg-type]
            store=_Store(),  # type: ignore[arg-type]
            space=space,
            corpus_id="conversation.episodic",
            telemetry=Telemetry(buffer),
            query_timeout_seconds=1.0,
            failure_circuit=_circuit(clock),
        )

        evidence = await recall.recall(
            SemanticRecallQuery(RetrievalScope("personal", 7), "tea", 3)
        )

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


class _BlockingEmbeddings:
    """@brief 永不主动完成的 Query embedder / Query embedder that never completes voluntarily."""

    def __init__(self) -> None:
        """@brief 初始化调用数 / Initialize the call count."""

        self.calls = 0

    async def embed_query(
        self,
        text: str,
        *,
        space: EmbeddingSpace,
    ) -> EmbeddingVector:
        """@brief 等待至 deadline 取消 / Wait until cancelled by the deadline."""

        del text, space
        self.calls += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_timeout_downgrades_first_recall_and_cooldown_short_circuits() -> None:
    """@brief 首次超时显式降级，冷却期不再触碰端口 / First timeout downgrades explicitly and cooldown avoids the port."""

    async def scenario() -> None:
        """@brief 连续执行两次召回 / Execute two consecutive recalls."""

        embeddings = _BlockingEmbeddings()
        clock = _Clock()
        recall = SemanticRecall(
            embeddings=embeddings,  # type: ignore[arg-type]
            store=_Store(),  # type: ignore[arg-type]
            space=_space(),
            corpus_id="conversation.episodic",
            telemetry=Telemetry(TelemetryBuffer(32)),
            query_timeout_seconds=0.01,
            failure_circuit=_circuit(clock),
        )
        query = SemanticRecallQuery(RetrievalScope("personal", 7), "tea", 3)

        with pytest.raises(SemanticRecallUnavailableError) as first:
            await recall.recall(query)
        assert isinstance(first.value.__cause__, TimeoutError)
        with pytest.raises(SemanticRecallUnavailableError) as second:
            await recall.recall(query)
        assert second.value.__cause__ is None
        assert embeddings.calls == 1

    asyncio.run(scenario())


class _RecoveringEmbeddings:
    """@brief 首次传输失败后恢复的 embedder / Embedder recovering after one transport failure."""

    def __init__(self) -> None:
        """@brief 初始化调用数 / Initialize the call count."""

        self.calls = 0

    async def embed_query(
        self,
        text: str,
        *,
        space: EmbeddingSpace,
    ) -> EmbeddingVector:
        """@brief 首次失败，之后返回向量 / Fail once and then return a vector."""

        del text, space
        self.calls += 1
        if self.calls == 1:
            raise RetryableEmbeddingError("temporary transport failure")
        return EmbeddingVector((1.0, 0.0))


def test_recall_probes_after_cooldown_and_success_recovers_circuit() -> None:
    """@brief 冷却结束后允许恢复探测且成功关闭断路 / Cooldown expiry permits a recovery probe whose success closes the circuit."""

    async def scenario() -> None:
        """@brief 失败、短路、推进时钟并成功 / Fail, short-circuit, advance, and succeed."""

        embeddings = _RecoveringEmbeddings()
        clock = _Clock()
        recall = SemanticRecall(
            embeddings=embeddings,  # type: ignore[arg-type]
            store=_Store(),  # type: ignore[arg-type]
            space=_space(),
            corpus_id="conversation.episodic",
            telemetry=Telemetry(TelemetryBuffer(64)),
            query_timeout_seconds=1.0,
            failure_circuit=_circuit(clock),
        )
        query = SemanticRecallQuery(RetrievalScope("personal", 7), "tea", 3)

        with pytest.raises(SemanticRecallUnavailableError):
            await recall.recall(query)
        with pytest.raises(SemanticRecallUnavailableError):
            await recall.recall(query)
        assert embeddings.calls == 1

        clock.advance(61.0)
        assert len(await recall.recall(query)) == 1
        assert len(await recall.recall(query)) == 1
        assert embeddings.calls == 3

    asyncio.run(scenario())


class _CancellableEmbeddings:
    """@brief 可协调外部取消的 embedder / Embedder coordinating external cancellation."""

    def __init__(self) -> None:
        """@brief 初始化同步事件与模式 / Initialize synchronization and mode."""

        self.started = asyncio.Event()
        self.block = True
        self.calls = 0

    async def embed_query(
        self,
        text: str,
        *,
        space: EmbeddingSpace,
    ) -> EmbeddingVector:
        """@brief 首次等待取消，恢复后返回 / Await cancellation first, then return after recovery."""

        del text, space
        self.calls += 1
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        return EmbeddingVector((1.0, 0.0))


def test_cancelled_error_propagates_without_opening_recall_circuit() -> None:
    """@brief 外部取消原样传播且不污染依赖健康状态 / External cancellation propagates without poisoning dependency health."""

    async def scenario() -> None:
        """@brief 取消首个 task 后立即成功召回 / Cancel the first task and immediately recall successfully."""

        embeddings = _CancellableEmbeddings()
        clock = _Clock()
        recall = SemanticRecall(
            embeddings=embeddings,  # type: ignore[arg-type]
            store=_Store(),  # type: ignore[arg-type]
            space=_space(),
            corpus_id="conversation.episodic",
            telemetry=Telemetry(TelemetryBuffer(64)),
            query_timeout_seconds=5.0,
            failure_circuit=_circuit(clock),
        )
        query = SemanticRecallQuery(RetrievalScope("personal", 7), "tea", 3)
        task = asyncio.create_task(recall.recall(query))
        await embeddings.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        embeddings.block = False
        assert len(await recall.recall(query)) == 1
        assert embeddings.calls == 2

    asyncio.run(scenario())


class _BuggyEmbeddings:
    """@brief 模拟未分类程序错误的 embedder / Embedder simulating an unclassified programming error."""

    def __init__(self) -> None:
        """@brief 初始化调用数 / Initialize the call count."""

        self.calls = 0

    async def embed_query(
        self,
        text: str,
        *,
        space: EmbeddingSpace,
    ) -> EmbeddingVector:
        """@brief 泄漏未分类 OSError / Leak an unclassified OSError."""

        del text, space
        self.calls += 1
        raise OSError("unclassified adapter error")


def test_unclassified_program_error_propagates_without_opening_circuit() -> None:
    """@brief 程序错误不被伪装成可用性降级且不触发断路 / Programming errors are neither disguised as availability failures nor circuit-breaking signals."""

    async def scenario() -> None:
        """@brief 连续暴露同一程序错误 / Expose the same programming error twice."""

        embeddings = _BuggyEmbeddings()
        recall = SemanticRecall(
            embeddings=embeddings,  # type: ignore[arg-type]
            store=_Store(),  # type: ignore[arg-type]
            space=_space(),
            corpus_id="conversation.episodic",
            telemetry=Telemetry(TelemetryBuffer(64)),
            query_timeout_seconds=1.0,
            failure_circuit=_circuit(_Clock()),
        )
        query = SemanticRecallQuery(RetrievalScope("personal", 7), "tea", 3)

        for _ in range(2):
            with pytest.raises(OSError, match="unclassified adapter error"):
                await recall.recall(query)
        assert embeddings.calls == 2

    asyncio.run(scenario())
