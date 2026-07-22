"""@brief 检索应用端口与工作值对象 / Retrieval application ports and work values."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from fogmoe_bot.domain.retrieval import (
    EmbeddingSpace,
    EmbeddingVector,
    RetrievalEvidence,
    RetrievalPassage,
    RetrievalScope,
)
from fogmoe_bot.domain.temporal import ensure_utc

EPISODIC_CORPUS_ID = "conversation.episodic"
"""@brief 私聊情景历史语料库 / Private-conversation episodic-history corpus."""

CONVERSATION_TURN_SOURCE_KIND = "conversation.turn"
"""@brief Conversation Turn 来源类别 / Conversation-Turn source kind."""


@dataclass(frozen=True, slots=True)
class EpisodicTurn:
    """@brief 可投影的一次完整 Assistant Turn / One complete Assistant turn ready for projection.

    @param turn_id 来源 Turn / Source Turn.
    @param scope 个人或群聊隔离域 / Personal or group isolation scope.
    @param user_text 用户文本 / User text.
    @param assistant_text Assistant 文本 / Assistant text.
    @param occurred_at Turn 事件时间 / Turn event time.
    """

    turn_id: UUID
    scope: RetrievalScope
    user_text: str
    assistant_text: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验情景来源 / Validate the episodic source.

        @return None / None.
        @raise ValueError owner 或文本非法 / Invalid owner or text.
        """

        user_text = self.user_text.strip()
        assistant_text = self.assistant_text.strip()
        if not user_text or len(user_text) > 100_000:
            raise ValueError("Episodic user text must contain 1-100000 characters")
        if not assistant_text or len(assistant_text) > 100_000:
            raise ValueError("Episodic assistant text must contain 1-100000 characters")
        object.__setattr__(self, "user_text", user_text)
        object.__setattr__(self, "assistant_text", assistant_text)
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))


@dataclass(frozen=True, slots=True)
class PassageVectorClaim:
    """@brief 带 fencing token 的 passage embedding claim / Passage-embedding claim with fencing.

    @param passage 待嵌入文本 / Passage to embed.
    @param space 嵌入空间 / Embedding space.
    @param claim_token 当前 claim UUID / Current claim UUID.
    @param attempt_count 已开始尝试次数 / Number of started attempts.
    """

    passage: RetrievalPassage
    space: EmbeddingSpace
    claim_token: UUID
    attempt_count: int

    def __post_init__(self) -> None:
        """@brief 校验 claim / Validate the claim.

        @return None / None.
        @raise ValueError attempt_count 非正 / Non-positive attempt count.
        """

        if isinstance(self.attempt_count, bool) or self.attempt_count < 1:
            raise ValueError("Vector claim attempt_count must be positive")


class RetryableEmbeddingError(RuntimeError):
    """@brief 可通过重试恢复的 embedding 失败 / Embedding failure recoverable by retry.

    @param retry_after Provider 指定的最小等待 / Provider-specified minimum delay.
    """

    retry_after: timedelta | None

    def __init__(self, message: str, *, retry_after: timedelta | None = None) -> None:
        """@brief 创建可重试错误 / Create a retryable error.

        @param message 安全错误消息 / Safe error message.
        @param retry_after 可选最小等待 / Optional minimum delay.
        """

        if retry_after is not None and retry_after <= timedelta():
            raise ValueError("retry_after must be positive")
        super().__init__(message)
        self.retry_after = retry_after


class EmbeddingContractError(RuntimeError):
    """@brief Provider 响应违反 embedding 契约 / Provider response violates the embedding contract."""


class RetrievalIOError(RuntimeError):
    """@brief 检索存储端口发生可用性故障 / Retrieval-store port encountered an availability failure.

    @note Adapter 应仅把数据库驱动、连接或远程存储 I/O 错误翻译为该类型；
        数据映射和业务不变量错误必须原样暴露。/ Adapters should translate only
        database-driver, connection, or remote-store I/O errors to this type; data-mapping
        and business-invariant errors must remain visible.
    """


class StaleVectorClaimError(RuntimeError):
    """@brief Passage vector claim 已被回收或替换 / Passage-vector claim was recovered or superseded."""


class EmbeddingProvider(Protocol):
    """@brief Provider-neutral embedding 端口 / Provider-neutral embedding port."""

    async def embed_documents(
        self,
        texts: Sequence[str],
        *,
        space: EmbeddingSpace,
    ) -> Sequence[EmbeddingVector]:
        """@brief 批量嵌入 passage / Embed passages in a batch.

        @param texts 非空文本序列 / Non-empty text sequence.
        @param space 目标空间 / Target space.
        @return 与输入同序向量 / Vectors in input order.
        """

        ...

    async def embed_query(
        self,
        text: str,
        *,
        space: EmbeddingSpace,
    ) -> EmbeddingVector:
        """@brief 使用空间指令嵌入 Query / Embed a query with the space instruction.

        @param text Query 文本 / Query text.
        @param space 目标空间 / Target space.
        @return Query 向量 / Query vector.
        """

        ...


class EpisodicSource(Protocol):
    """@brief 未投影 Conversation Turn 来源 / Source of unprojected conversation turns."""

    async def read_unprojected(
        self,
        *,
        format_version: int,
        limit: int,
    ) -> Sequence[EpisodicTurn]:
        """@brief 读取尚未形成指定格式的 Turn / Read turns absent from a format projection.

        @return 稳定顺序 Turn / Turns in stable order.
        """

        ...


class RetrievalStore(Protocol):
    """@brief Passage、vector workflow 与检索的持久化端口 / Persistence for passages, vector workflow, and search."""

    async def ensure_space(self, space: EmbeddingSpace) -> None:
        """@brief 创建或验证空间契约 / Create or verify a space contract."""

        ...

    async def project_turn(
        self,
        turn: EpisodicTurn,
        passages: Sequence[RetrievalPassage],
        *,
        space: EmbeddingSpace,
        projected_at: datetime,
    ) -> None:
        """@brief 原子投影 Turn、passages 与 vector intents / Atomically project a turn, passages, and vector intents."""

        ...

    async def claim_vectors(
        self,
        *,
        space: EmbeddingSpace,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[PassageVectorClaim]:
        """@brief 领取待嵌入 passages / Claim passages awaiting embeddings."""

        ...

    async def complete_vector(
        self,
        claim: PassageVectorClaim,
        vector: EmbeddingVector,
        *,
        completed_at: datetime,
    ) -> None:
        """@brief fenced 完成一个向量 / Complete one vector with fencing."""

        ...

    async def retry_vector(
        self,
        claim: PassageVectorClaim,
        *,
        retry_at: datetime,
        error: str,
        failed_at: datetime,
    ) -> None:
        """@brief 安排有限重试 / Schedule a bounded retry."""

        ...

    async def fail_vector(
        self,
        claim: PassageVectorClaim,
        *,
        error: str,
        failed_at: datetime,
    ) -> None:
        """@brief 终结不可恢复向量任务 / Finally fail an unrecoverable vector task."""

        ...

    async def recover_expired_vector_leases(
        self,
        *,
        space: EmbeddingSpace,
        now: datetime,
    ) -> int:
        """@brief 回收 crash 遗留 lease / Recover leases left by crashes."""

        ...

    async def search(
        self,
        *,
        scope: RetrievalScope,
        corpus_id: str,
        space: EmbeddingSpace,
        query_vector: EmbeddingVector,
        limit: int,
    ) -> Sequence[RetrievalEvidence]:
        """@brief 在个人/群聊隔离后执行精确语义检索 / Run exact semantic search after personal/group isolation."""

        ...


__all__ = [
    "CONVERSATION_TURN_SOURCE_KIND",
    "EPISODIC_CORPUS_ID",
    "EmbeddingContractError",
    "EmbeddingProvider",
    "EpisodicSource",
    "EpisodicTurn",
    "PassageVectorClaim",
    "RetrievalStore",
    "RetrievalIOError",
    "RetryableEmbeddingError",
    "StaleVectorClaimError",
]
