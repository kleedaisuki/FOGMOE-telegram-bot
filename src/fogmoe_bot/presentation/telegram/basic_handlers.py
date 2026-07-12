"""@brief 无业务写入的 durable 基础命令 / Durable basic commands without business writes."""

from __future__ import annotations

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_MESSAGE

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import delivery_stream_for_chat


_GITHUB_TEXT = "Open Source:\nAGPL-3.0: https://github.com/FogMoe/telegram-bot"
"""@brief 稳定纯文本源码链接 / Stable plain-text source link."""


class StaticTelegramCommandHandler:
    """@brief 通过 standalone outbox 回复 `/help` 与 `/github` / Reply to `/help` and `/github` through the standalone outbox."""

    def __init__(
        self,
        *,
        outbound: StandaloneOutboundCapability,
        help_text: str,
    ) -> None:
        """@brief 注入 durable outbox 与帮助内容 / Inject the durable outbox and help content.

        @param outbound standalone outbox 能力 / Standalone-outbox capability.
        @param help_text 版本控制帮助文本 / Version-controlled help text.
        @raise ValueError help 文本为空或超过 Telegram 上限 / Blank help text or text above Telegram's limit.
        """

        normalized_help = help_text.strip()
        if not normalized_help or len(normalized_help) > 4096:
            raise ValueError("help_text must contain 1-4096 characters")
        self._outbound = outbound
        self._help_text = normalized_help

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回静态命令所有权 / Return static-command ownership.

        @return help/github / help/github.
        """

        return frozenset({"help", "github"})

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 幂等写入静态回复 / Idempotently write a static reply.

        @param update durable source Update / Durable source Update.
        @param command 已解析静态命令 / Parsed static command.
        @return None / None.
        """

        if command.command == "help":
            text = self._help_text
            parse_mode: str | None = "Markdown"
            disable_preview = True
        elif command.command == "github":
            text = _GITHUB_TEXT
            parse_mode = None
            disable_preview = False
        else:
            raise ValueError("Static command handler received an unowned command")
        idempotency_key = (
            f"update:{int(update.update_id)}:command:{command.command}:response"
        )
        await self._outbound.enqueue(
            StandaloneOutboundCommand(
                conversation_id=update.conversation_id,
                delivery_stream_id=delivery_stream_for_chat(
                    command.chat_id,
                    command.message_thread_id,
                ),
                kind=SEND_TELEGRAM_MESSAGE,
                payload={
                    "chat_id": command.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "message_thread_id": command.message_thread_id,
                    "reply_to_message_id": command.message_id,
                    "disable_web_page_preview": disable_preview,
                },
                idempotency_key=idempotency_key,
                created_at=update.received_at,
            )
        )


__all__ = ["StaticTelegramCommandHandler"]
