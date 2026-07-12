"""@brief Telegram Bot membership adapter / Telegram Bot membership adapter."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import ContextTypes


_WELCOME_TEXT = (
    "欢迎使用雾萌机器人喵！输入 /help 查看功能。\n"
    "Welcome to FogMoeBot! Type /help to see available features."
)
"""@brief Bot 加入 chat 时的稳定欢迎文本 / Stable welcome text when the Bot joins a chat."""


async def bot_membership_changed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief Bot 从离开态进入成员态时发送欢迎 / Send a welcome when the Bot transitions from absent to member.

    @param update my_chat_member Update / my_chat_member Update.
    @param context PTB context / PTB context.
    @return None / None.
    """

    change = update.my_chat_member
    if change is None:
        return
    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status
    if (
        change.new_chat_member.user.id != context.bot.id
        or old_status not in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
        or new_status
        not in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    ):
        return
    await context.bot.send_message(chat_id=change.chat.id, text=_WELCOME_TEXT)


__all__ = ["bot_membership_changed"]
