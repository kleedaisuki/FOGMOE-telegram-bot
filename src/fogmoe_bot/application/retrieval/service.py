"""@brief 语义召回应用服务 / Semantic-recall application service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.application.retrieval.ports import EmbeddingProvider, RetrievalStore
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind
from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    RetrievalEvidence,
    RetrievalScope,
)


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
        if not 1 <= self.limit <= 20:
            raise ValueError("Semantic recall limit must be between 1 and 20")
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
    ) -> None:
        """@brief 注入检索依赖 / Inject retrieval dependencies.

        @param embeddings Query embedder / Query embedder.
        @param store 检索存储 / Retrieval store.
        @param space 活跃嵌入空间 / Active embedding space.
        @param corpus_id 目标语料库 / Target corpus.
        @param telemetry 进程 typed telemetry / Process typed telemetry.
        """

        self._embeddings = embeddings
        self._store = store
        self._space = space
        self._corpus_id = corpus_id
        self._telemetry = telemetry

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
                with self._telemetry.span(
                    "retrieval.query.embedding",
                    kind=SpanKind.CLIENT,
                ):
                    vector = await self._embeddings.embed_query(
                        query.text,
                        space=self._space,
                    )
                vector.require_space(self._space)
                candidate_limit = min(100, query.limit * 3)
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
                    search_span.set_attribute(
                        "retrieval.candidate.count",
                        len(evidence),
                    )
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


class SemanticRecallReader(Protocol):
    """@brief Assistant 所需的语义召回端口 / Semantic-recall port required by Assistant."""

    async def recall(self, query: SemanticRecallQuery) -> tuple[RetrievalEvidence, ...]:
        """@brief 返回相关证据 / Return relevant evidence."""

        ...


__all__ = ["SemanticRecall", "SemanticRecallQuery", "SemanticRecallReader"]
