"""@brief Telegram Bot API 群管理员权限 adapter / Telegram Bot API adapter for group-administrator privileges."""

from __future__ import annotations

from telegram import Bot
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden


class TelegramGroupAdministratorSource:
    """@brief 通过 ``getChatMember`` 读取 owner/administrator / Read owner-or-administrator status through ``getChatMember``."""

    def __init__(self, bot: Bot) -> None:
        """@brief 注入 Telegram Bot client / Inject the Telegram Bot client.

        @param bot 进程共享 Bot client / Process-shared Bot client.
        """

        self._bot = bot

    async def is_administrator(self, *, chat_id: int, user_id: int) -> bool:
        """@brief 查询群权限 / Query group privileges.

        @param chat_id 群 ID / Group identifier.
        @param user_id 用户 ID / User identifier.
        @return owner/administrator 为 True / True for owner or administrator.
        @note Telegram API 错误原样传播，由 durable inbox 重试策略处理。/
            Telegram API errors propagate to the durable-inbox retry policy.
        """

        try:
            member = await self._bot.get_chat_member(chat_id, user_id)
        except BadRequest, Forbidden:
            return False
        return member.status in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }


__all__ = ["TelegramGroupAdministratorSource"]
