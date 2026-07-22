"""@brief Memory 遗忘命令的应用契约 / Application contract for memory forgetting commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fogmoe_bot.domain.conversation.identity import ConversationId, TurnSource
from fogmoe_bot.domain.conversation.outbox import OutboundDraft, OutboundEnqueueResult
from fogmoe_bot.domain.retrieval import RetrievalScope
from fogmoe_bot.domain.temporal import ensure_utc


@dataclass(frozen=True, slots=True)
class ForgetMemory:
    """@brief 遗忘一个隔离域在请求时刻前的派生记忆 / Forget derived memory in one scope through the request time.

    @param source 命令的 durable 幂等来源 / Durable idempotency source.
    @param conversation_id 命令所属 Conversation / Conversation owning the command.
    @param scope 个人或群聊检索隔离域 / Personal or group retrieval scope.
    @param confirmation 与遗忘原子提交的确认消息 / Confirmation committed atomically with forgetting.
    @param requested_at 严格遗忘上界 / Inclusive forgetting cutoff.
    """

    source: TurnSource
    conversation_id: ConversationId
    scope: RetrievalScope
    confirmation: OutboundDraft
    requested_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验命令与确认共享一个事实边界 / Validate that command and confirmation describe one fact boundary.

        @return None / None.
        @raise ValueError 确认不属于命令或携带 Turn / Confirmation is unrelated or Turn-owned.
        """

        timestamp = ensure_utc(self.requested_at)
        if self.confirmation.conversation_id != self.conversation_id:
            raise ValueError(
                "Memory-reset confirmation must belong to its conversation"
            )
        if self.confirmation.turn_id is not None:
            raise ValueError("Memory-reset confirmation must be standalone")
        if self.confirmation.created_at != timestamp:
            raise ValueError("Memory reset and confirmation must share one timestamp")
        object.__setattr__(self, "requested_at", timestamp)


@dataclass(frozen=True, slots=True)
class ForgetMemoryResult:
    """@brief 一次幂等遗忘的规范结果 / Canonical result of an idempotent forgetting operation.

    @param deleted_passages 删除的可召回 passage 数 / Number of retrievable passages deleted.
    @param applied 本次是否首次应用命令 / Whether this call first applied the command.
    @param confirmation 规范 outbox 回执 / Canonical outbox receipt.
    """

    deleted_passages: int
    applied: bool
    confirmation: OutboundEnqueueResult

    def __post_init__(self) -> None:
        """@brief 校验删除计数 / Validate the deletion count.

        @return None / None.
        @raise ValueError 删除计数为负 / The deletion count is negative.
        """

        if self.deleted_passages < 0:
            raise ValueError("Deleted passage count cannot be negative")


class MemoryForgetPersistence(Protocol):
    """@brief Memory 遗忘与确认 outbox 的原子持久化端口 / Atomic persistence for memory forgetting and its confirmation."""

    async def forget(self, command: ForgetMemory) -> ForgetMemoryResult:
        """@brief 幂等建立遗忘边界并删除派生语料 / Idempotently establish a forgetting boundary and delete derived passages.

        @param command 已校验遗忘命令 / Validated forgetting command.
        @return 规范结果 / Canonical result.
        """

        ...


__all__ = ["ForgetMemory", "ForgetMemoryResult", "MemoryForgetPersistence"]
