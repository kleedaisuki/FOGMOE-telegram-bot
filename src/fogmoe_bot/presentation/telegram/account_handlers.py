"""@brief Durable Telegram account commands / Durable Telegram account commands."""

from __future__ import annotations

from fogmoe_bot.application.accounts.operations import (
    AccountCode,
    AccountRegistrationResult,
    AccountService,
    PersonalInfoResult,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import enqueue_command_reply


class AccountTelegramCommandHandler:
    """@brief 将 `/me` 与 `/setmyinfo` 映射到 account service 和 outbox / Map `/me` and `/setmyinfo` to the account service and outbox."""

    def __init__(
        self,
        *,
        accounts: AccountService,
        outbound: StandaloneOutboundCapability,
    ) -> None:
        """@brief 注入账户服务与 outbox / Inject the account service and outbox.

        @param accounts 账户用例 / Account use cases.
        @param outbound standalone outbox / Standalone outbox.
        """

        self._accounts = accounts
        self._outbound = outbound

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回账户命令所有权 / Return account-command ownership.

        @return me/setmyinfo / me/setmyinfo.
        """

        return frozenset({"me", "setmyinfo"})

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行账户命令并写 deterministic response / Execute an account command and write a deterministic response.

        @param update durable source Update / Durable source Update.
        @param command parsed command / Parsed command.
        @return None / None.
        """

        if command.command == "me":
            text = await self._me(update, command)
        elif command.command == "setmyinfo":
            text = await self._personal_info(update, command)
        else:
            raise ValueError("Account handler received an unowned command")
        await enqueue_command_reply(self._outbound, update, command, text)

    async def _me(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 注册账户并渲染稳定快照 / Register an account and render its stable snapshot.

        @param update durable source Update / Durable source Update.
        @param command parsed `/me` / Parsed `/me`.
        @return 用户文本 / User-facing text.
        """

        if command.username is None:
            return (
                "您需要设置Telegram用户名才能使用机器人。\n"
                "请在Telegram设置中设置用户名后再尝试。\n\n"
                "You need to set a Telegram username to use this bot.\n"
                "Please set your username in Telegram settings and try again."
            )
        result = await self._accounts.register(
            command.user_id,
            command.username,
            idempotency_key=(
                f"telegram:account-register:{int(update.update_id)}:{command.user_id}"
            ),
        )
        return _profile_text(result)

    async def _personal_info(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 查看或更新个人信息 / Inspect or update personal information.

        @param update durable source Update / Durable source Update.
        @param command parsed `/setmyinfo` / Parsed `/setmyinfo`.
        @return 用户文本 / User-facing text.
        """

        new_info: str | None
        if not command.argument_text:
            new_info = None
        elif command.argument_text.casefold() == "clear":
            new_info = ""
        else:
            new_info = command.argument_text
        if new_info is not None and len(new_info) > 500:
            return (
                "最长500个字符，个人自定义信息长度超过500字符，请重试。\n"
                "The maximum length is 500 characters; please shorten your personal information."
            )
        result = await self._accounts.personal_info(
            command.user_id,
            new_info,
            idempotency_key=(
                f"telegram:personal-info:{int(update.update_id)}:{command.user_id}"
            ),
        )
        return _personal_info_text(result)


def _profile_text(result: AccountRegistrationResult) -> str:
    """@brief 渲染账户快照 / Render an account snapshot.

    @param result registration result / Registration result.
    @return 用户文本 / User-facing text.
    """

    profile = result.profile
    return (
        "👤 用户信息 User Info\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"用户名 Name: @{profile.username}\n"
        f"权限 Permission: {profile.permission}\n"
        f"方案 Plan: {profile.plan}\n\n"
        "💰 金币资产 Coins Balance\n"
        f"• 总额 Total: {profile.total_coins}\n"
        f"• 免费 Free: {profile.free_coins}\n"
        f"• 付费 Paid: {profile.paid_coins}"
    )


def _personal_info_text(result: PersonalInfoResult) -> str:
    """@brief 渲染个人信息结果 / Render a personal-info result.

    @param result typed result / Typed result.
    @return 用户文本 / User-facing text.
    """

    if result.code is AccountCode.NOT_REGISTERED:
        return "请先使用 /me 命令注册个人信息。"
    previous = result.previous_info or "无"
    prefix = f"您当前保存的个人自定义信息是 Your current personal info is:\n{previous}"
    if result.updated:
        return f"{prefix}\n\n个人自定义信息已更新。\nPersonal information has been updated."
    return (
        f"{prefix}\n\n"
        "请在 /setmyinfo 命令后输入要保存的信息。输入 CLEAR 可以清空。\n"
        "Enter personal information after /setmyinfo, or CLEAR to remove it."
    )


__all__ = ["AccountTelegramCommandHandler"]
