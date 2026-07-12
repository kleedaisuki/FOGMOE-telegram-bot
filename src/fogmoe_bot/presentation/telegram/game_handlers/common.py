"""@brief 游戏 Telegram 处理器共享边界 / Shared boundary for game Telegram handlers."""

from __future__ import annotations

from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

type TelegramContext = ContextTypes.DEFAULT_TYPE
"""@brief PTB 默认 callback context / PTB default callback context."""


def current_time() -> datetime:
    """@brief 返回本地 aware 时刻 / Return the local aware instant.

    @return 当前 aware datetime / Current aware datetime.
    """

    return datetime.now().astimezone()


def idempotency_key(update: Update, operation: str, user_id: int) -> str:
    """@brief 构造 Update 绑定幂等键 / Build an Update-bound idempotency key.

    @param update Telegram Update / Telegram Update.
    @param operation 操作名 / Operation name.
    @param user_id 用户 ID / User ID.
    @return 稳定幂等键 / Stable idempotency key.
    """

    return f"telegram:{operation}:{update.update_id}:{user_id}"
