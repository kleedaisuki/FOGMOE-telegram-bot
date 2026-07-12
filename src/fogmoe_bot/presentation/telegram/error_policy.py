"""@brief Telegram legacy-callback error boundary / Telegram legacy-callback error boundary."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from .runtime_settings import telegram_runtime_settings


logger = logging.getLogger(__name__)

async def telegram_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 记录完整异常，仅向用户返回通用 correlation-safe 文本 / Log the full exception and return only generic correlation-safe text.

    @param update PTB 提供的 Update 或 None / Update or None supplied by PTB.
    @param context 含原始异常的 PTB context / PTB context containing the original error.
    @return None / None.
    @note 禁止把数据库错误、token、路径或 stack trace 回显给用户 / Database errors,
        tokens, paths, and stack traces must never be echoed to users.
    """

    error = context.error
    logger.error(
        "Telegram legacy callback failed: update_id=%s",
        update.update_id if isinstance(update, Update) else None,
        exc_info=error if isinstance(error, BaseException) else None,
    )
    if not isinstance(update, Update):
        return
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(
                "处理请求时出错，请稍后再试。",
                show_alert=True,
            )
            return
        if update.effective_message is not None:
            administrator = telegram_runtime_settings(
                context
            ).administrator_contact_label
            await update.effective_message.reply_text(
                "处理请求时出现了暂时性问题，请稍后重试。若问题持续，请联系管理员 "
                f"{administrator}。\n"
                "A temporary problem occurred while processing the request. Please retry later."
            )
    except Exception:
        logger.exception(
            "Telegram error response also failed: update_id=%s",
            update.update_id,
        )


__all__ = ["telegram_error_handler"]
