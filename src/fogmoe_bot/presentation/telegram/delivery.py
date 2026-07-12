"""@brief Telegram 投递 identity helpers / Telegram delivery-identity helpers."""

from __future__ import annotations

from typing import Protocol

from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.conversation.identity import DeliveryStreamId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.conversation.outbox import SEND_TELEGRAM_MESSAGE


class TelegramReplyCommand(Protocol):
    """@brief durable 回复所需的最小命令视图 / Minimal command view required for a durable reply."""

    @property
    def command(self) -> str: ...

    @property
    def chat_id(self) -> int: ...

    @property
    def message_thread_id(self) -> int | None: ...

    @property
    def message_id(self) -> int: ...


def delivery_stream_for_chat(
    chat_id: int,
    message_thread_id: int | None,
) -> DeliveryStreamId:
    """@brief 构造 Telegram chat/topic 的唯一有序投递流 / Build the sole ordered Telegram chat/topic delivery stream.

    @param chat_id Telegram chat ID / Telegram chat identifier.
    @param message_thread_id 可选 topic ID / Optional topic identifier.
    @return 规范 delivery-stream identity / Canonical delivery-stream identity.
    @raise ValueError chat ID 为零或 thread ID 非正 / A zero chat ID or non-positive thread ID.
    """

    if isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id == 0:
        raise ValueError("Telegram chat_id must be a non-zero integer")
    if message_thread_id is not None and (
        isinstance(message_thread_id, bool)
        or not isinstance(message_thread_id, int)
        or message_thread_id < 1
    ):
        raise ValueError("Telegram message_thread_id must be positive when present")
    return DeliveryStreamId(
        f"telegram:primary:chat:{chat_id}:thread:{message_thread_id or 0}"
    )


async def enqueue_command_reply(
    outbound: StandaloneOutboundCapability,
    update: InboundUpdate,
    command: TelegramReplyCommand,
    text: str,
) -> None:
    """@brief 幂等写入 durable command 回复 / Idempotently enqueue a durable command reply.

    @param outbound standalone outbox / Standalone outbox.
    @param update durable source Update / Durable source Update.
    @param command parsed command / Parsed command.
    @param text 用户文本 / User-facing text.
    @return None / None.
    """

    await outbound.enqueue(
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
                "message_thread_id": command.message_thread_id,
                "reply_to_message_id": command.message_id,
                "disable_web_page_preview": True,
            },
            idempotency_key=(
                f"update:{int(update.update_id)}:command:{command.command}:response"
            ),
            created_at=update.received_at,
        )
    )


__all__ = ["delivery_stream_for_chat", "enqueue_command_reply"]
