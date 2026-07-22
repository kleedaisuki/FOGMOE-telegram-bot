"""@brief Conversation 历史重置工作流 / Conversation-history reset workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    TurnSource,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundDraft,
    OutboundEnqueueResult,
)
from fogmoe_bot.domain.temporal import ensure_utc


@dataclass(frozen=True, slots=True)
class ResetConversation:
    """@brief 在某个 durable 来源处建立历史可见性边界 / Establish a history-visibility boundary at a durable source.

    @param source 命令的幂等来源 / Idempotent source of the command.
    @param conversation_id 被重置会话 / Conversation being reset.
    @param confirmation 与 reset 原子写入的确认 outbox / Confirmation outbox atomically written with the reset.
    @param requested_at 用户请求时刻 / Time of the user request.
    """

    source: TurnSource
    conversation_id: ConversationId
    confirmation: OutboundDraft
    requested_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验 reset 与确认副作用共享身份边界 / Validate the shared identity boundary of the reset and confirmation.

        @return None / None.
        @raise ValueError confirmation 不属于同一会话或携带 Turn / Confirmation belongs to another conversation or references a Turn.
        """

        requested_at = ensure_utc(self.requested_at)
        if self.confirmation.conversation_id != self.conversation_id:
            raise ValueError("Reset confirmation must belong to the reset conversation")
        if self.confirmation.turn_id is not None:
            raise ValueError("Reset confirmation must be a standalone outbound effect")
        if self.confirmation.created_at != requested_at:
            raise ValueError("Reset confirmation and command must share one timestamp")
        object.__setattr__(self, "requested_at", requested_at)


@dataclass(frozen=True, slots=True)
class ConversationResetResult:
    """@brief 历史边界与确认副作用的原子结果 / Atomic result of a history boundary and confirmation effect.

    @param through_sequence reset 隐藏的最大消息序号；空历史为 0 / Greatest hidden message sequence; zero for empty history.
    @param inserted 本次是否新建 reset / Whether this call created the reset.
    @param confirmation 规范 outbox 回执 / Canonical outbox receipt.
    """

    through_sequence: int
    inserted: bool
    confirmation: OutboundEnqueueResult

    def __post_init__(self) -> None:
        """@brief 校验非负历史边界 / Validate the non-negative history boundary.

        @return None / None.
        @raise ValueError 边界为负数 / The boundary is negative.
        """

        if self.through_sequence < 0:
            raise ValueError("Conversation reset sequence cannot be negative")


class ConversationResetPersistence(Protocol):
    """@brief reset 与确认 outbox 的原子持久化端口 / Atomic persistence port for reset and confirmation outbox."""

    async def reset(self, command: ResetConversation) -> ConversationResetResult:
        """@brief 幂等写入历史边界与确认 / Idempotently write the history boundary and confirmation.

        @param command 已校验 reset 命令 / Validated reset command.
        @return 规范 reset 结果 / Canonical reset result.
        """

        ...


__all__ = [
    "ConversationResetPersistence",
    "ConversationResetResult",
    "ResetConversation",
]
