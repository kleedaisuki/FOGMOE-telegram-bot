"""@brief 同一 Turn 的追加式 steer 领域值 / Append-only steering domain values for one Turn."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fogmoe_bot.domain.temporal import ensure_utc

from .identity import ConversationId, TurnId, TurnRevision, UpdateId
from .message import ConversationMessage, MessageRole

STEER_INPUT_KIND = "steer"
"""@brief Conversation user row 的 steer 标记 / Steer marker for a Conversation user row."""


@dataclass(frozen=True, slots=True)
class TurnSteer:
    """@brief 已原子追加到 active Turn 的用户 steer / User steer atomically appended to an active Turn.

    @param turn_id 被修订的 active Turn / Active Turn being revised.
    @param conversation_id 所属 Conversation / Owning Conversation.
    @param source_update_id steer 来源 Telegram Update / Telegram Update sourcing the steer.
    @param revision 接受 steer 后的新 revision / New revision after accepting the steer.
    @param message 同一 Turn 的 canonical user history row / Canonical user-history row in the same Turn.
    @param accepted_at durable 接受时刻 / Durable acceptance instant.
    @note steer 文本只有一份 durable 事实：``conversation_messages`` row。该值对象不创建
        第二份内容表。/ The steer text has exactly one durable fact: its
        ``conversation_messages`` row. This value object creates no second content table.
    """

    turn_id: TurnId
    conversation_id: ConversationId
    source_update_id: UpdateId
    revision: TurnRevision
    message: ConversationMessage
    accepted_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验 steer 的 ownership、来源与消息标记 / Validate steer ownership, source, and message marker.

        @return None / None.
        @raise ValueError revision、消息或来源边界不一致时抛出 /
            Raised when revision, message, or source boundaries disagree.
        """

        if int(self.revision) < 1:
            raise ValueError("A Turn steer requires a positive revision")
        draft = self.message.draft
        if draft.turn_id != self.turn_id:
            raise ValueError("Turn steer message must belong to the target Turn")
        if draft.conversation_id != self.conversation_id:
            raise ValueError("Turn steer message must belong to the target Conversation")
        if draft.source_update_id != self.source_update_id:
            raise ValueError("Turn steer message must carry its source Update")
        if draft.role is not MessageRole.USER:
            raise ValueError("Turn steer message must have the user role")
        if draft.content.get("input_kind") != STEER_INPUT_KIND:
            raise ValueError("Turn steer message requires input_kind=steer")
        if draft.content.get("input_revision") != int(self.revision):
            raise ValueError(
                "Turn steer message input_revision must match the accepted revision"
            )
        query_text = draft.content.get("text")
        if not isinstance(query_text, str) or not query_text.strip():
            raise ValueError("Turn steer message requires non-blank text")
        object.__setattr__(self, "accepted_at", ensure_utc(self.accepted_at))

    @property
    def query_text(self) -> str:
        """@brief 返回本 revision 的 WorkingMemory query / Return the WorkingMemory query for this revision.

        @return 未改写 steer 文本 / Unrewritten steer text.
        """

        value = self.message.draft.content["text"]
        if not isinstance(value, str):  # pragma: no cover - constructor proves this.
            raise AssertionError("Turn steer text lost its validated type")
        return value.strip()


__all__ = ["STEER_INPUT_KIND", "TurnSteer"]
