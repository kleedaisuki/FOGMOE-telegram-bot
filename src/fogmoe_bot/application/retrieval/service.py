"""@brief 语义召回应用服务 / Semantic-recall application service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from math import isfinite
from typing import Protocol

from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.application.retrieval.ports import (
    EmbeddingContractError,
    EmbeddingProvider,
    RetrievalIOError,
    RetrievalStore,
    RetryableEmbeddingError,
)
from fogmoe_bot.application.runtime import FailureCircuit
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind
from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    RetrievalEvidence,
    RetrievalScope,
)


MAX_SEMANTIC_RECALL_RESULTS = 128
"""@brief 单次通用语义召回结果硬上限 / Hard result limit for one generic semantic recall."""


type SemanticRecallCircuitKey = tuple[str, str]
"""@brief 语料库与嵌入空间组成的断路隔离键 / Circuit-isolation key of corpus and embedding space."""


class SemanticRecallUnavailableError(RuntimeError):
    """@brief 可选语义召回依赖暂时不可用 / Optional semantic-recall dependency is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class SemanticRecallQuery:
    """@brief 有界租户语义查询 / Bounded tenant-scoped semantic query.

    @param scope 个人或群聊检索域 / Personal or group retrieval scope.
    @param text 自然语言 Query / Natural-language query.
    @param limit 最大证据数 / Maximum evidence count.
    """

    scope: RetrievalScope
    text: str
    limit: int = 6

    def __post_init__(self) -> None:
        """@brief 校验 Query / Validate the query.

        @return None / None.
        @raise ValueError owner、文本或 limit 非法 / Invalid owner, text, or limit.
        """

        text = self.text.strip()
        if not text or len(text) > 20_000:
            raise ValueError("Semantic recall text must contain 1-20000 characters")
        if not 1 <= self.limit <= MAX_SEMANTIC_RECALL_RESULTS:
            raise ValueError(
                "Semantic recall limit must be between 1 and "
                f"{MAX_SEMANTIC_RECALL_RESULTS}"
            )
        object.__setattr__(self, "text", text)


class SemanticRecall:
    """@brief 嵌入 Query 并执行精确检索 / Embed a query and execute exact retrieval."""

    def __init__(
        self,
        *,
        embeddings: EmbeddingProvider,
        store: RetrievalStore,
        space: EmbeddingSpace,
        corpus_id: str,
        telemetry: Telemetry,
        query_timeout_seconds: float,
        failure_circuit: FailureCircuit[SemanticRecallCircuitKey],
    ) -> None:
        """@brief 注入检索依赖 / Inject retrieval dependencies.

        @param embeddings Query embedder / Query embedder.
        @param store 检索存储 / Retrieval store.
        @param space 活跃嵌入空间 / Active embedding space.
        @param corpus_id 目标语料库 / Target corpus.
        @param telemetry 进程 typed telemetry / Process typed telemetry.
        @param query_timeout_seconds 单次在线召回的独立 deadline / Independent deadline for one online recall.
        @param failure_circuit 在线召回专用短路器 / Circuit dedicated to online recall.
        """

        self._embeddings = embeddings
        self._store = store
        self._space = space
        self._corpus_id = corpus_id
        self._telemetry = telemetry
        timeout_seconds = float(query_timeout_seconds)
        if not isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise ValueError("query_timeout_seconds must be finite and positive")
        self._query_timeout_seconds = timeout_seconds
        self._failure_circuit = failure_circuit
        self._circuit_key = (corpus_id, space.space_id)

    async def recall(self, query: SemanticRecallQuery) -> tuple[RetrievalEvidence, ...]:
        """@brief 返回有 provenance 的相关证据 / Return relevant evidence with provenance.

        @param query 已验证查询 / Validated query.
        @return 距离升序证据 / Evidence ordered by ascending distance.
        """

        try:
            with self._telemetry.span(
                "retrieval.recall",
                attributes={
                    "retrieval.corpus.id": self._corpus_id,
                    "retrieval.space.id": self._space.space_id,
                    "retrieval.result.limit": query.limit,
                },
            ) as recall_span:
                permit = self._failure_circuit.try_acquire(self._circuit_key)
                if permit is None:
                    recall_span.set_attribute("retrieval.availability", "unavailable")
                    recall_span.set_attribute(
                        "retrieval.unavailable.reason", "circuit_open"
                    )
                    raise SemanticRecallUnavailableError(
                        "Semantic recall circuit is open"
                    )
                outcome_recorded = False
                try:
                    async with asyncio.timeout(self._query_timeout_seconds):
                        selected = await self._execute(query)
                except asyncio.CancelledError:
                    raise
                except (
                    TimeoutError,
                    RetryableEmbeddingError,
                    EmbeddingContractError,
                    RetrievalIOError,
                ) as error:
                    self._failure_circuit.record_failure(permit)
                    outcome_recorded = True
                    recall_span.set_attribute("retrieval.availability", "unavailable")
                    recall_span.set_attribute(
                        "retrieval.unavailable.reason",
                        _unavailable_reason(error),
                    )
                    raise SemanticRecallUnavailableError(
                        "Semantic recall dependency is unavailable"
                    ) from error
                else:
                    self._failure_circuit.record_success(permit)
                    outcome_recorded = True
                finally:
                    if not outcome_recorded:
                        self._failure_circuit.abandon(permit)
                recall_span.set_attribute("retrieval.availability", "available")
                recall_span.set_attribute("retrieval.result.count", len(selected))
        except Exception:
            self._telemetry.counter(
                MetricName.RETRIEVAL_OUTCOMES,
                attributes={"operation": "recall", "outcome": Outcome.FAILURE},
            )
            raise
        self._telemetry.counter(
            MetricName.RETRIEVAL_OUTCOMES,
            attributes={"operation": "recall", "outcome": Outcome.SUCCESS},
        )
        return tuple(selected)

    async def _execute(
        self,
        query: SemanticRecallQuery,
    ) -> tuple[RetrievalEvidence, ...]:
        """@brief 在调用方 deadline 内执行嵌入、搜索与去重 / Embed, search, and deduplicate within the caller deadline.

        @param query 已验证查询 / Validated query.
        @return 距离升序且来源唯一的证据 / Distance-ordered evidence with unique sources.
        """

        with self._telemetry.span(
            "retrieval.query.embedding",
            kind=SpanKind.CLIENT,
        ):
            vector = await self._embeddings.embed_query(
                query.text,
                space=self._space,
            )
        vector.require_space(self._space)
        candidate_limit = min(384, query.limit * 3)
        with self._telemetry.span(
            "retrieval.search",
            kind=SpanKind.CLIENT,
            attributes={"retrieval.candidate.limit": candidate_limit},
        ) as search_span:
            evidence = await self._store.search(
                scope=query.scope,
                corpus_id=self._corpus_id,
                space=self._space,
                query_vector=vector,
                limit=candidate_limit,
            )
            search_span.set_attribute("retrieval.candidate.count", len(evidence))
        selected: list[RetrievalEvidence] = []
        seen_sources: set[tuple[str, object]] = set()
        for item in evidence:
            source = (item.passage.source_kind, item.passage.source_id)
            if source in seen_sources:
                continue
            seen_sources.add(source)
            selected.append(item)
            if len(selected) >= query.limit:
                break
        return tuple(selected)


def _unavailable_reason(error: Exception) -> str:
    """@brief 将已知可用性错误映射为低基数观测值 / Map known availability errors to low-cardinality telemetry.

    @param error 已分类可用性错误 / Classified availability error.
    @return 稳定原因名 / Stable reason name.
    """

    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, RetryableEmbeddingError):
        return "embedding_transport"
    if isinstance(error, EmbeddingContractError):
        return "embedding_contract"
    return "retrieval_io"


class SemanticRecallReader(Protocol):
    """@brief Assistant 所需的语义召回端口 / Semantic-recall port required by Assistant."""

    async def recall(self, query: SemanticRecallQuery) -> tuple[RetrievalEvidence, ...]:
        """@brief 返回相关证据 / Return relevant evidence."""

        ...


__all__ = [
    "SemanticRecall",
    "SemanticRecallCircuitKey",
    "SemanticRecallQuery",
    "SemanticRecallReader",
    "SemanticRecallUnavailableError",
]
