"""@brief Telegram 私聊与群/Topic 会话身份规则 / Telegram private and group/topic conversation identity rules."""

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
    """@brief 规范化的小写 chat 类型 / Normalized lowercase chat type."""

    chat_id: int | None
    """@brief 可选 chat ID / Optional chat identifier."""

    user_id: int | None
    """@brief 可选已认证发送者 ID / Optional authenticated sender identifier."""

    message_thread_id: int | None
    """@brief 可选群 Topic ID / Optional group-topic identifier."""

    def __post_init__(self) -> None:
        """@brief 校验并规范化 Telegram 地址组成 / Validate and normalize Telegram address components.

        @return None / None.
        @raise TypeError 字段不是声明类型时抛出 / Raised when a field has the wrong type.
        @raise ValueError ID 或 chat type 非法时抛出 / Raised when an identifier or chat type is invalid.
        """

        chat_type = self.chat_type
        if chat_type is not None:
            if not isinstance(chat_type, str):
                raise TypeError("Telegram chat_type must be a string when present")
            chat_type = chat_type.strip().casefold()
            if not chat_type:
                raise ValueError("Telegram chat_type cannot be blank")
        if self.chat_id is not None:
            _nonzero_integer(self.chat_id, "chat_id")
        if self.user_id is not None:
            _positive_integer(self.user_id, "user_id")
        if self.message_thread_id is not None:
            _positive_integer(self.message_thread_id, "message_thread_id")
        if chat_type in GROUP_CHAT_TYPES and self.chat_id is None:
            raise ValueError("Telegram group conversations require chat_id")
        if chat_type not in GROUP_CHAT_TYPES and self.message_thread_id is not None:
            raise ValueError("Telegram topics belong only to group conversations")
        object.__setattr__(self, "chat_type", chat_type)

    @property
    def is_group(self) -> bool:
        """@brief 判断地址是否为群或超级群 / Whether this address is a group or supergroup.

        @return 群聊类型为 True / True for a group-chat type.
        """

        return self.chat_type in GROUP_CHAT_TYPES

    @property
    def conversation_id(self) -> ConversationId:
        """@brief 投影 durable Conversation identity / Project the durable Conversation identity.

        @return 私聊用户、群 Topic 或稳定 fallback 身份 / Private-user, group-topic, or stable fallback identity.
        @raise ValueError 地址既无 chat 也无 user 时抛出 / Raised when the address has neither chat nor user identity.
        """

        if self.is_group:
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


def _positive_integer(value: int, field: str) -> None:
    """@brief 校验正严格整数 / Validate a positive strict integer.

    @param value 候选值 / Candidate value.
    @param field 错误字段名 / Field name for errors.
    @return None / None.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Telegram {field} must be an integer")
    if value <= 0:
        raise ValueError(f"Telegram {field} must be positive")


def _nonzero_integer(value: int, field: str) -> None:
    """@brief 校验非零严格整数 / Validate a non-zero strict integer.

    @param value 候选值 / Candidate value.
    @param field 错误字段名 / Field name for errors.
    @return None / None.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Telegram {field} must be an integer")
    if value == 0:
        raise ValueError(f"Telegram {field} must be non-zero")


__all__ = ["GROUP_CHAT_TYPES", "TelegramConversationAddress"]
