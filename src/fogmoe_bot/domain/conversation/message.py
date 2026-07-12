"""Append-only conversation message 模型 / Append-only conversation-message models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .identity import (
    ConversationId,
    ConversationMessageId,
    MessageSequence,
    TurnId,
    UpdateId,
    normalize_idempotency_key,
)
from .payloads import JsonObject
from .temporal import ensure_utc


class MessageRole(StrEnum):
    """@brief 会话消息角色 / Conversation-message role."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class MessageDraft:
    """@brief 尚未分配会话序号的消息 / Message awaiting a conversation sequence.

    @param message_id 消息 ID / Message identifier.
    @param conversation_id 会话键 / Conversation key.
    @param turn_id 可选来源回合 / Optional source turn.
    @param source_update_id 可选来源 Update / Optional source Update.
    @param role 消息角色 / Message role.
    @param content 结构化内容 / Structured content.
    @param idempotency_key 会话内幂等键 / Conversation-scoped idempotency key.
    @param created_at 创建时间 / Creation time.
    """

    message_id: ConversationMessageId
    conversation_id: ConversationId
    turn_id: TurnId | None
    source_update_id: UpdateId | None
    role: MessageRole
    content: JsonObject
    idempotency_key: str
    created_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验消息草稿 / Validate the message draft.

        @return None / None.
        @raise ValueError 幂等键为空或过长时抛出 / Raised when the idempotency key is empty or too long.
        """

        key = normalize_idempotency_key(self.idempotency_key)
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "content", dict(self.content))


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """@brief 已分配顺序的追加式会话消息 / Sequenced append-only conversation message.

    @param draft 原始消息草稿 / Original message draft.
    @param sequence 会话内序号 / Conversation-local sequence.
    """

    draft: MessageDraft
    sequence: MessageSequence


@dataclass(frozen=True, slots=True)
class MessageAppendResult:
    """@brief 幂等追加消息结果 / Idempotent message-append result.

    @param message 数据库中的规范消息 / Canonical stored message.
    @param inserted 本次是否创建新行 / Whether this call inserted a new row.
    """

    message: ConversationMessage
    inserted: bool
