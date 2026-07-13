"""@brief 语义召回应用服务 / Semantic-recall application service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.application.retrieval.ports import EmbeddingProvider, RetrievalStore
from fogmoe_bot.domain.retrieval import EmbeddingSpace, RetrievalEvidence


@dataclass(frozen=True, slots=True)
class SemanticRecallQuery:
    """@brief 有界租户语义查询 / Bounded tenant-scoped semantic query.

    @param owner_user_id 认证所有者 / Authenticated owner.
    @param text 自然语言 Query / Natural-language query.
    @param limit 最大证据数 / Maximum evidence count.
    """

    owner_user_id: int
    text: str
    limit: int = 6

    def __post_init__(self) -> None:
        """@brief 校验 Query / Validate the query.

        @return None / None.
        @raise ValueError owner、文本或 limit 非法 / Invalid owner, text, or limit.
        """

        text = self.text.strip()
        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Semantic recall owner_user_id must be positive")
        if not text or len(text) > 2_000:
            raise ValueError("Semantic recall text must contain 1-2000 characters")
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
    ) -> None:
        """@brief 注入检索依赖 / Inject retrieval dependencies.

        @param embeddings Query embedder / Query embedder.
        @param store 检索存储 / Retrieval store.
        @param space 活跃嵌入空间 / Active embedding space.
        @param corpus_id 目标语料库 / Target corpus.
        """

        self._embeddings = embeddings
        self._store = store
        self._space = space
        self._corpus_id = corpus_id

    async def recall(self, query: SemanticRecallQuery) -> tuple[RetrievalEvidence, ...]:
        """@brief 返回有 provenance 的相关证据 / Return relevant evidence with provenance.

        @param query 已验证查询 / Validated query.
        @return 距离升序证据 / Evidence ordered by ascending distance.
        """

        vector = await self._embeddings.embed_query(query.text, space=self._space)
        vector.require_space(self._space)
        evidence = await self._store.search(
            owner_user_id=query.owner_user_id,
            corpus_id=self._corpus_id,
            space=self._space,
            query_vector=vector,
            limit=min(100, query.limit * 3),
        )
        selected: list[RetrievalEvidence] = []
        seen_sources = set()
        for item in evidence:
            source = (item.passage.source_kind, item.passage.source_id)
            if source in seen_sources:
                continue
            seen_sources.add(source)
            selected.append(item)
            if len(selected) >= query.limit:
                break
        return tuple(selected)


class SemanticRecallReader(Protocol):
    """@brief Assistant 所需的语义召回端口 / Semantic-recall port required by Assistant."""

    async def recall(self, query: SemanticRecallQuery) -> tuple[RetrievalEvidence, ...]:
        """@brief 返回相关证据 / Return relevant evidence."""

        ...


__all__ = ["SemanticRecall", "SemanticRecallQuery", "SemanticRecallReader"]
