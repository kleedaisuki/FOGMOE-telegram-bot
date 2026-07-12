"""@brief Durable `/tl` Telegram adapter / Durable `/tl` Telegram adapter.

该 adapter 只把已持久化 Telegram JSON envelope 映射成 typed translation ingress；
provider、计费和投递分别由 inference activity、acceptance UoW 与 outbox 所有。/
This adapter only maps a persisted Telegram JSON envelope into typed translation ingress;
inference activities, the acceptance UoW, and the outbox own the provider, charging, and delivery.
"""

from __future__ import annotations

from fogmoe_bot.application.conversation.translation_ingress import (
    TranslationFeedbackReason,
    TranslationIngressCoordinator,
    TranslationReplyTarget,
    TranslationTurnRequest,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import delivery_stream_for_chat


_SUPPORTED_CHAT_TYPES = frozenset({"private", "group", "supergroup"})
"""@brief `/tl` 支持的 Telegram chat 类型 / Telegram chat types supported by `/tl`."""

_GROUP_CHAT_TYPES = frozenset({"group", "supergroup"})
"""@brief 群聊类型 / Group-chat types."""


class TranslationTelegramCommandHandler:
    """@brief 将 `/tl` 映射到 durable Translation coordinator / Map `/tl` into the durable Translation coordinator."""

    def __init__(self, coordinator: TranslationIngressCoordinator) -> None:
        """@brief 注入翻译入口协调器 / Inject the translation-ingress coordinator.

        @param coordinator durable 翻译入口 / Durable translation ingress.
        """

        self._coordinator = coordinator

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回独占命令名 / Return the exclusively owned command name.

        @return `tl` / `tl`.
        """

        return frozenset({"tl"})

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 接受翻译或写入幂等用法反馈 / Accept a translation or enqueue idempotent usage feedback.

        @param update durable source Update / Durable source Update.
        @param command 已解析 `/tl` envelope / Parsed `/tl` envelope.
        @return None / None.
        """

        if command.command != "tl":
            raise ValueError("Translation handler received an unowned command")
        if command.chat_type not in _SUPPORTED_CHAT_TYPES:
            raise ValueError(
                f"Translation does not support chat type: {command.chat_type}"
            )
        target = TranslationReplyTarget(
            update_id=update.update_id,
            conversation_id=update.conversation_id,
            received_at=update.received_at,
            chat_id=command.chat_id,
            message_id=command.message_id,
            message_thread_id=command.message_thread_id,
            delivery_stream_id=delivery_stream_for_chat(
                command.chat_id,
                command.message_thread_id,
            ),
            trace_context=update.trace_context,
        )
        text = command.reply_text or command.argument_text
        if not text.strip():
            await self._coordinator.reject(
                target,
                TranslationFeedbackReason.USAGE,
            )
            return
        await self._coordinator.handle(
            TranslationTurnRequest(
                target=target,
                user_id=command.user_id,
                username=command.username,
                display_name=command.display_name,
                is_group=command.chat_type in _GROUP_CHAT_TYPES,
                text=text,
            )
        )


__all__ = ["TranslationTelegramCommandHandler"]
