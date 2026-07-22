"""@brief Telegram 监控通知投递适配器 / Telegram monitor-notification delivery adapter."""

import logging

from telegram import Bot

from fogmoe_bot.infrastructure.telegram.telegram_utils import (
    partial_send,
    safe_send_markdown,
)

logger = logging.getLogger(__name__)


class TelegramMonitorNotificationSink:
    """@brief 向 Telegram chat 投递监控通知 / Deliver monitor notifications to Telegram chats."""

    def __init__(self, bot: Bot) -> None:
        """@brief 创建适配器 / Create the adapter.

        @param bot 主生命周期拥有的 Telegram Bot / Telegram Bot owned by the main lifecycle.
        """

        self._bot = bot

    async def send(self, chat_id: int, message: str) -> None:
        """@brief 安全投递 Markdown 消息 / Safely deliver a Markdown message.

        @param chat_id 目标 chat ID / Target chat ID.
        @param message 通知文本 / Notification text.
        @return None / None.
        """

        await safe_send_markdown(
            partial_send(self._bot.send_message, chat_id),
            message,
            logger=logger,
        )


__all__ = ["TelegramMonitorNotificationSink"]
