"""@brief Telegram 用户举报用例 / Telegram user-reporting use case."""

import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from fogmoe_bot.application.telegram.command_cooldown import cooldown
from fogmoe_bot.domain.moderation import ChatId, MessageId, UserId
from fogmoe_bot.domain.moderation.reporting import (
    InMemoryReportDeduplicator,
    ReportDeliveryResult,
    ReportKey,
    ReportRegistration,
)


logger = logging.getLogger(__name__)
"""@brief 模块日志器 / Module logger."""

REPORT_DEDUPLICATOR = InMemoryReportDeduplicator(ttl_seconds=3600.0)
"""@brief 进程内举报去重器 / In-process report deduplicator."""


def _message_link(chat_id: int, message_id: int) -> str | None:
    """@brief 构造超级群消息链接 / Build a supergroup message link.

    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param message_id Telegram 消息 ID / Telegram message ID.
    @return 可用链接；普通群返回 None / Link, or None for basic groups.
    """

    encoded = str(chat_id)
    if not encoded.startswith("-100"):
        return None
    return f"https://t.me/c/{encoded[4:]}/{message_id}"


@cooldown
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 将回复目标举报给群管理员 / Report a replied-to message to chat administrators."""

    if update.effective_chat is None or update.message is None or update.effective_user is None:
        return
    if update.effective_chat.type not in {"group", "supergroup"}:
        await update.message.reply_text("此命令只能在群组中使用。")
        return
    reported_message = update.message.reply_to_message
    if reported_message is None or reported_message.from_user is None:
        await update.message.reply_text("请回复您要举报的消息，并附带 /report 命令。")
        return

    key = ReportKey(
        chat_id=ChatId(update.effective_chat.id),
        message_id=MessageId(reported_message.message_id),
    )
    registration = REPORT_DEDUPLICATOR.register(
        key,
        UserId(update.effective_user.id),
        now=time.monotonic(),
    )
    if registration is ReportRegistration.DUPLICATE:
        await update.message.reply_text("您已经举报过这条消息了。")
        return

    try:
        administrators = [
            member
            for member in await context.bot.get_chat_administrators(update.effective_chat.id)
            if not member.user.is_bot
        ]
    except Exception as exc:
        logger.error("获取举报通知管理员失败: %s", exc)
        await update.message.reply_text("举报处理过程中出错，请稍后再试。")
        return

    reported_user = reported_message.from_user
    reporter = update.effective_user
    reported_text = reported_message.text or reported_message.caption or "消息内容无法获取"
    report_text = (
        "<b>== 举报信息 ==</b>\n\n"
        f"<b>群组:</b> {html.escape(update.effective_chat.title or '未知群组')}\n"
        f"<b>群组 ID:</b> <code>{update.effective_chat.id}</code>\n\n"
        f"<b>被举报用户:</b> {html.escape(reported_user.full_name)}\n"
        f"<b>用户 ID:</b> <code>{reported_user.id}</code>\n\n"
        f"<b>被举报消息:</b>\n{html.escape(reported_text[:300])}"
        f"{'…' if len(reported_text) > 300 else ''}\n\n"
        f"<b>举报人:</b> {html.escape(reporter.full_name)}\n"
        f"<b>举报人 ID:</b> <code>{reporter.id}</code>"
    )
    link = _message_link(update.effective_chat.id, reported_message.message_id)
    keyboard = (
        InlineKeyboardMarkup([[InlineKeyboardButton("查看被举报消息", url=link)]])
        if link
        else None
    )

    delivered = 0
    for administrator in administrators:
        try:
            await context.bot.send_message(
                chat_id=administrator.user.id,
                text=report_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            delivered += 1
        except Exception as exc:
            logger.warning(
                "投递举报通知失败: admin=%s error=%s",
                administrator.user.id,
                exc,
            )

    result = ReportDeliveryResult(
        administrator_count=len(administrators),
        delivered_count=delivered,
    )
    if result.delivered_count:
        await update.message.reply_text(
            f"您的举报已发送给群组管理员({result.delivered_count}/{result.administrator_count})。"
        )
    else:
        await update.message.reply_text(
            "无法发送举报信息给管理员，请直接联系群组管理员处理。"
        )


def setup_report_handlers(application) -> None:
    """@brief 注册举报命令 handler / Register the report command handler."""

    application.add_handler(CommandHandler("report", report_command))
    logger.info("已加载举报功能处理器")
