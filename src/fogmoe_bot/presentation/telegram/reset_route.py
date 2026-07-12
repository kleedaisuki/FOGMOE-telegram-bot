"""@brief Telegram `/clear` durable Conversation reset route / Telegram `/clear` durable Conversation-reset route."""

from __future__ import annotations

from fogmoe_bot.application.conversation.reset import (
    ConversationResetPersistence,
    ResetConversation,
)
from fogmoe_bot.application.conversation.router import (
    RoutedOperation,
    conversation_aggregate_key,
)
from fogmoe_bot.application.runtime import WorkPriority
from fogmoe_bot.domain.conversation.identity import (
    OutboundMessageId,
    TurnSource,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)

from .assistant_update_models import MalformedTelegramAssistantUpdate
from .assistant_update_parser import parse_telegram_assistant_update
from .delivery import delivery_stream_for_chat


_RESET_CONFIRMATION_TEXT = (
    "雾萌娘已进行记忆清除处理。\nThe current conversation history has been cleared."
)
"""@brief reset 成功确认文本 / Successful-reset confirmation text."""


class TelegramConversationResetPrimaryRoute:
    """@brief 将 `/clear` 映射为 reset+outbox 原子命令 / Map `/clear` to an atomic reset-plus-outbox command."""

    def __init__(
        self,
        *,
        persistence: ConversationResetPersistence,
        bot_username: str,
    ) -> None:
        """@brief 注入 reset 工作流与 Bot username / Inject the reset workflow and Bot username.

        @param persistence durable reset 持久化端口 / Durable reset persistence port.
        @param bot_username 用于校验 `/clear@Bot` / Used to validate `/clear@Bot`.
        @raise ValueError bot username 为空 / The bot username is blank.
        """

        normalized = bot_username.removeprefix("@").strip()
        if not normalized:
            raise ValueError("bot_username cannot be blank")
        self._persistence = persistence
        self._bot_username = normalized

    @property
    def name(self) -> str:
        """@brief 返回稳定 route 名 / Return the stable route name.

        @return `telegram-conversation-reset` / `telegram-conversation-reset`.
        """

        return "telegram-conversation-reset"

    def matches(self, update: InboundUpdate) -> bool:
        """@brief 匹配当前 Bot 的 `/clear` 命令 / Match a `/clear` command targeting this Bot.

        @param update durable Telegram Update / Durable Telegram Update.
        @return 独占该命令时为 True / True when this route owns the command.
        """

        try:
            parsed = parse_telegram_assistant_update(update)
        except MalformedTelegramAssistantUpdate:
            return False
        return parsed.command == "clear" and (
            parsed.command_target is None
            or parsed.command_target.casefold() == self._bot_username.casefold()
        )

    async def operation(self, update: InboundUpdate) -> RoutedOperation:
        """@brief 构造无直接 I/O 的 reset 操作 / Build a reset operation with no direct I/O.

        @param update 已匹配 `/clear` Update / Matched `/clear` Update.
        @return keyed reset 操作 / Keyed reset operation.
        @raise ValueError route 不拥有该 Update / The route does not own the Update.
        """

        parsed = parse_telegram_assistant_update(update)
        if not self.matches(update):
            raise ValueError("Conversation reset operation requires a matching /clear")
        source = TurnSource.telegram(update.update_id)
        idempotency_key = (
            f"update:{int(update.update_id)}:conversation-reset-confirmation"
        )
        confirmation = OutboundDraft(
            message_id=OutboundMessageId.for_conversation(
                update.conversation_id,
                idempotency_key,
            ),
            conversation_id=update.conversation_id,
            turn_id=None,
            delivery_stream_id=delivery_stream_for_chat(
                parsed.chat_id,
                parsed.message_thread_id,
            ),
            kind=SEND_TELEGRAM_MESSAGE,
            payload={
                "chat_id": parsed.chat_id,
                "text": _RESET_CONFIRMATION_TEXT,
                "reply_to_message_id": parsed.message_id,
                "message_thread_id": parsed.message_thread_id,
                "disable_notification": False,
                "protect_content": False,
                "disable_web_page_preview": True,
            },
            idempotency_key=idempotency_key,
            created_at=update.received_at,
        )
        command = ResetConversation(
            source=source,
            conversation_id=update.conversation_id,
            confirmation=confirmation,
            requested_at=update.received_at,
        )

        async def call() -> None:
            """@brief 写入 reset 与 confirmation / Write the reset and confirmation.

            @return None / None.
            """

            await self._persistence.reset(command)

        return RoutedOperation(
            name=f"telegram-conversation-reset:{int(update.update_id)}",
            key=conversation_aggregate_key(update.conversation_id),
            call=call,
            priority=WorkPriority.HIGH,
        )


__all__ = ["TelegramConversationResetPrimaryRoute"]
