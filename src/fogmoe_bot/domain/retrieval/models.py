"""@brief 通用语义检索领域值对象 / Generic semantic-retrieval domain values."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from fogmoe_bot.domain.temporal import ensure_utc


_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
"""@brief 稳定检索标识符语法 / Stable retrieval-identifier grammar."""

_PASSAGE_NAMESPACE = UUID("37b67af8-b1f7-5d40-87c6-97b7ed33e352")
"""@brief Passage UUIDv5 命名空间 / Namespace for deterministic passage UUIDv5 identities."""


@dataclass(frozen=True, slots=True)
class EmbeddingSpace:
    """@brief 一个不可混用的嵌入空间契约 / Contract for one non-interchangeable embedding space.

    @param space_id 稳定空间标识 / Stable space identifier.
    @param model Provider 模型标识 / Provider model identifier.
    @param dimensions 物理向量维度 / Physical vector dimensions.
    @param query_instruction Query 侧任务指令 / Query-side task instruction.
    @param passage_format_version Passage renderer 版本 / Passage-renderer version.
    """

    space_id: str
    model: str
    dimensions: int
    query_instruction: str
    passage_format_version: int

    def __post_init__(self) -> None:
        """@brief 规范并校验嵌入空间 / Normalize and validate the embedding space.

        @return None / None.
        @raise ValueError 标识、模型、维度或版本非法 / Invalid identity, model, dimensions, or version.
        """

        space_id = self.space_id.strip()
        model = self.model.strip()
        instruction = self.query_instruction.strip()
        if _IDENTIFIER_PATTERN.fullmatch(space_id) is None:
            raise ValueError("Embedding space_id has invalid syntax")
        if not model or len(model) > 255:
            raise ValueError("Embedding model must contain 1-255 characters")
        if not 1 <= self.dimensions <= 2_000:
            raise ValueError("Embedding dimensions must be between 1 and 2000")
        if not instruction or len(instruction) > 2_000:
            raise ValueError("Query instruction must contain 1-2000 characters")
        if (
            isinstance(self.passage_format_version, bool)
            or self.passage_format_version < 1
        ):
            raise ValueError("Passage format version must be positive")
        object.__setattr__(self, "space_id", space_id)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "query_instruction", instruction)


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """@brief 有限且非零的稠密向量 / Finite non-zero dense vector.

    @param values 浮点坐标 / Floating-point coordinates.
    """

    values: tuple[float, ...]

    def __post_init__(self) -> None:
        """@brief 校验坐标 / Validate coordinates.

        @return None / None.
        @raise ValueError 向量为空、非有限或为零 / Empty, non-finite, or zero vector.
        """

        values = tuple(float(value) for value in self.values)
        if not values:
            raise ValueError("Embedding vector cannot be empty")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Embedding vector must contain only finite values")
        if not any(value != 0.0 for value in values):
            raise ValueError("Embedding vector cannot be zero")
        object.__setattr__(self, "values", values)

    def require_space(self, space: EmbeddingSpace) -> None:
        """@brief 验证向量属于指定空间 / Require the vector to match a space.

        @param space 目标嵌入空间 / Target embedding space.
        @return None / None.
        @raise ValueError 维度不匹配 / Dimension mismatch.
        """

        if len(self.values) != space.dimensions:
            raise ValueError(
                f"Embedding dimension mismatch: expected {space.dimensions}, "
                f"received {len(self.values)}"
            )


@dataclass(frozen=True, slots=True)
class RetrievalPassage:
    """@brief 可独立召回且有来源的文本段 / Independently retrievable sourced text passage.

    @param passage_id 确定性 passage identity / Deterministic passage identity.
    @param corpus_id 逻辑语料库 / Logical corpus.
    @param owner_user_id 强租户边界 / Strong tenant boundary.
    @param source_kind 来源类别 / Source kind.
    @param source_id 不透明来源 UUID / Opaque source UUID.
    @param ordinal 来源内顺序 / Ordinal within the source.
    @param format_version 文本 renderer 版本 / Text-renderer version.
    @param text 规范文本 / Canonical text.
    @param content_digest 文本 SHA-256 / Text SHA-256.
    @param occurred_at 来源事件时间 / Source event time.
    """

    passage_id: UUID
    corpus_id: str
    owner_user_id: int
    source_kind: str
    source_id: UUID
    ordinal: int
    format_version: int
    text: str
    content_digest: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验 passage 不变量 / Validate passage invariants.

        @return None / None.
        @raise ValueError 任一 identity、文本或 digest 漂移 / Identity, text, or digest drift.
        """

        corpus_id = self.corpus_id.strip()
        source_kind = self.source_kind.strip()
        text = self.text.strip()
        if _IDENTIFIER_PATTERN.fullmatch(corpus_id) is None:
            raise ValueError("Retrieval corpus_id has invalid syntax")
        if _IDENTIFIER_PATTERN.fullmatch(source_kind) is None:
            raise ValueError("Retrieval source_kind has invalid syntax")
        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Retrieval owner_user_id must be positive")
        if isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise ValueError("Retrieval ordinal cannot be negative")
        if isinstance(self.format_version, bool) or self.format_version < 1:
            raise ValueError("Retrieval format_version must be positive")
        if not text or len(text) > 20_000:
            raise ValueError("Retrieval passage text must contain 1-20000 characters")
        digest = passage_digest(text)
        if self.content_digest != digest:
            raise ValueError("Retrieval passage digest does not match its text")
        expected_id = passage_identity(
            corpus_id=corpus_id,
            source_kind=source_kind,
            source_id=self.source_id,
            format_version=self.format_version,
            ordinal=self.ordinal,
        )
        if self.passage_id != expected_id:
            raise ValueError("Retrieval passage identity is not canonical")
        object.__setattr__(self, "corpus_id", corpus_id)
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))

    @classmethod
    def create(
        cls,
        *,
        corpus_id: str,
        owner_user_id: int,
        source_kind: str,
        source_id: UUID,
        ordinal: int,
        format_version: int,
        text: str,
        occurred_at: datetime,
    ) -> RetrievalPassage:
        """@brief 从来源语义构造规范 passage / Create a canonical passage from source semantics.

        @return 规范 passage / Canonical passage.
        """

        normalized = text.strip()
        return cls(
            passage_id=passage_identity(
                corpus_id=corpus_id,
                source_kind=source_kind,
                source_id=source_id,
                format_version=format_version,
                ordinal=ordinal,
            ),
            corpus_id=corpus_id,
            owner_user_id=owner_user_id,
            source_kind=source_kind,
            source_id=source_id,
            ordinal=ordinal,
            format_version=format_version,
            text=normalized,
            content_digest=passage_digest(normalized),
            occurred_at=occurred_at,
        )


@dataclass(frozen=True, slots=True)
class RetrievalEvidence:
    """@brief 检索返回的可追溯证据 / Provenance-bearing retrieval evidence.

    @param passage 命中 passage / Matching passage.
    @param cosine_distance 余弦距离，越小越相关 / Cosine distance; lower is more relevant.
    """

    passage: RetrievalPassage
    cosine_distance: float

    def __post_init__(self) -> None:
        """@brief 校验距离 / Validate the distance.

        @return None / None.
        @raise ValueError 距离非有限或超出余弦范围 / Non-finite or out-of-range cosine distance.
        """

        distance = float(self.cosine_distance)
        if not math.isfinite(distance) or not 0.0 <= distance <= 2.000001:
            raise ValueError("Cosine distance must be finite and between 0 and 2")
        object.__setattr__(self, "cosine_distance", distance)


def passage_identity(
    *,
    corpus_id: str,
    source_kind: str,
    source_id: UUID,
    format_version: int,
    ordinal: int,
) -> UUID:
    """@brief 派生 passage UUIDv5 / Derive a passage UUIDv5.

    @return 确定性 UUID / Deterministic UUID.
    """

    identity = (
        f"{corpus_id}\x1f{source_kind}\x1f{source_id}\x1f{format_version}\x1f{ordinal}"
    )
    return uuid5(_PASSAGE_NAMESPACE, identity)


def passage_digest(text: str) -> str:
    """@brief 计算规范 UTF-8 文本 SHA-256 / Compute canonical UTF-8 text SHA-256.

    @param text 已规范文本 / Canonical text.
    @return 小写十六进制 digest / Lowercase hexadecimal digest.
    """

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "EmbeddingSpace",
    "EmbeddingVector",
    "RetrievalEvidence",
    "RetrievalPassage",
    "passage_digest",
    "passage_identity",
]
