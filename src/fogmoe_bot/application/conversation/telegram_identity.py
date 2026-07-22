"""@brief Telegram 私聊与群/Topic Conversation 身份 / Telegram private and group/topic Conversation identity."""

from __future__ import annotations

from dataclasses import dataclass

from fogmoe_bot.domain.conversation.identity import ConversationId

GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})
"""@brief 共享 Conversation 的 Telegram 群类型 / Telegram group types sharing a Conversation."""


@dataclass(frozen=True, slots=True)
class TelegramConversationAddress:
    """@brief Telegram chat/thread 的规范会话地址 / Canonical conversation address for a Telegram chat/thread.

    @param chat_type Telegram chat type / Telegram chat type.
    @param chat_id 可选 chat ID / Optional chat identifier.
    @param user_id 可选已认证发送者 ID / Optional authenticated sender identifier.
    @param message_thread_id 可选群 Topic ID / Optional group-topic identifier.
    @note 私聊以用户为 Conversation；群聊以 ``group_id + topic`` 为 Conversation，因而所有
        群成员共享同一 Context stream。/ Private chats use the user as the Conversation;
        group chats use ``group_id + topic`` so every group member shares one Context stream.
    """

    chat_type: str | None
    chat_id: int | None
    user_id: int | None
    message_thread_id: int | None

    def __post_init__(self) -> None:
        """@brief 校验 Telegram 地址组成 / Validate Telegram address components.

        @return None / None.
        @raise ValueError ID 或 chat type 非法 / An identifier or chat type is invalid.
        """

        if self.chat_type is not None and not self.chat_type.strip():
            raise ValueError("Telegram chat_type cannot be blank")
        if self.chat_id is not None and (
            isinstance(self.chat_id, bool) or self.chat_id == 0
        ):
            raise ValueError("Telegram chat_id must be a non-zero integer")
        if self.user_id is not None and (
            isinstance(self.user_id, bool) or self.user_id <= 0
        ):
            raise ValueError("Telegram user_id must be positive")
        if self.message_thread_id is not None and (
            isinstance(self.message_thread_id, bool) or self.message_thread_id <= 0
        ):
            raise ValueError("Telegram message_thread_id must be positive")
        if self.chat_type in GROUP_CHAT_TYPES and self.chat_id is None:
            raise ValueError("Telegram group conversations require chat_id")

    @property
    def conversation_id(self) -> ConversationId:
        """@brief 投影 durable Conversation identity / Project the durable Conversation identity.

        @return 私聊用户、群 Topic 或稳定 fallback 身份 / Private-user, group-topic, or stable fallback identity.
        @raise ValueError 地址既无 chat 也无 user / The address has neither chat nor user identity.
        """

        if self.chat_type in GROUP_CHAT_TYPES:
            if self.chat_id is None:
                raise RuntimeError("Validated group address lost chat_id")
            return ConversationId(
                f"assistant-group:{self.chat_id}:thread:{self.message_thread_id or 0}"
            )
        if self.user_id is not None:
            return ConversationId(f"assistant-user:{self.user_id}")
        if self.chat_id is not None:
            return ConversationId(f"telegram-chat:{self.chat_id}")
        raise ValueError("Telegram conversation address requires chat_id or user_id")


__all__ = ["GROUP_CHAT_TYPES", "TelegramConversationAddress"]
