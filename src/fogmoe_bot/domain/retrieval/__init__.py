"""@brief 通用检索领域公共接口 / Public generic-retrieval domain API."""

from fogmoe_bot.domain.retrieval.models import (
    EmbeddingSpace,
    EmbeddingVector,
    RetrievalEvidence,
    RetrievalPassage,
    RetrievalScope,
    RetrievalScopeKind,
    passage_digest,
    passage_identity,
)

__all__ = [
    "EmbeddingSpace",
    "EmbeddingVector",
    "RetrievalEvidence",
    "RetrievalPassage",
    "RetrievalScope",
    "RetrievalScopeKind",
    "passage_digest",
    "passage_identity",
]
