"""@brief 群组代币图表绑定 Telegram handler / Telegram handler for group chart-token bindings."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from fogmoe_bot.application.crypto.chart_service import (
    BindChartToken,
    ClearChartToken,
)
from fogmoe_bot.domain.crypto import Blockchain, ChartToken, ContractAddress

from .common import chart_service


logger = logging.getLogger(__name__)
"""@brief 图表 handler 的结构化日志器 / Structured logger for the chart handler."""

_SUPPORTED_CHAIN_TEXT = "sol, solana, eth, ethereum, blast, bsc, bnb"
"""@brief 用户可输入的链别名列表 / Supported user-input chain aliases."""


async def chart_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 解析 `/chart`、授权变更并渲染图表 / Parse `/chart`, authorize mutations, and render the chart.

    @param update Telegram Update / Telegram Update.
    @param context PTB 默认 callback context / PTB default callback context.
    @return None / None.
    @note 仅群组管理员可修改绑定；读取不涉及余额、报价或外部资产交易 /
        Only group administrators may mutate a binding; reads involve no balance, quote, or asset trade.
    """

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat is None or user is None or message is None:
        return
    if chat.type not in {"group", "supergroup"}:
        await message.reply_text("此命令只能在群组中使用。")
        return
    args = tuple(context.args or ())
    service = chart_service(context)
    if len(args) >= 3 and args[0].lower() == "bind":
        if not await _is_group_admin(update):
            await message.reply_text("只有群组管理员才能绑定代币。")
            return
        try:
            token = ChartToken(
                Blockchain.parse(args[1]),
                ContractAddress(args[2]),
            )
        except ValueError:
            await message.reply_text(
                f"不支持的链名称或合约地址。支持的链: {_SUPPORTED_CHAIN_TEXT}"
            )
            return
        await service.bind_chart(
            BindChartToken(
                group_id=chat.id,
                actor_id=user.id,
                token=token,
                idempotency_key=(
                    f"telegram:crypto:chart-bind:{update.update_id}:{chat.id}"
                ),
            )
        )
        await message.reply_text(
            f"成功为群组绑定{token.chain.value}链上的代币。\n合约地址: {token.contract}"
        )
        return
    if len(args) == 1 and args[0].lower() == "clear":
        if not await _is_group_admin(update):
            await message.reply_text("只有群组管理员才能清除代币绑定。")
            return
        await service.clear_chart(
            ClearChartToken(
                group_id=chat.id,
                actor_id=user.id,
                idempotency_key=(
                    f"telegram:crypto:chart-clear:{update.update_id}:{chat.id}"
                ),
            )
        )
        await message.reply_text("成功清除了群组的代币绑定。")
        return
    if not args:
        bound_token = await service.chart_token(chat.id)
        if bound_token is None:
            await message.reply_text(_chart_help(unbound=True))
            return
        chart_url = (
            f"https://www.gmgn.cc/kline/{bound_token.chain.value}/"
            f"{bound_token.contract}"
        )
        await message.reply_text(
            "🔍 *代币图表*\n\n"
            f"链: {bound_token.chain.display_name}\n"
            f"合约: `{bound_token.contract}`\n\n"
            f"[点击查看图表]({chart_url})",
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False,
        )
        return
    await message.reply_text(_chart_help(unbound=False))


async def _is_group_admin(update: Update) -> bool:
    """@brief 在 Telegram 边界验证群管理员身份 / Verify group-administrator identity at the Telegram boundary.

    @param update Telegram Update / Telegram Update.
    @return 当前用户是管理员时为 True / True when the current user is an administrator.
    @note Telegram API 暂不可用时 fail closed / Fail closed when the Telegram API is unavailable.
    """

    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return False
    try:
        member = await chat.get_member(user.id)
    except TelegramError as error:
        logger.warning("Could not verify chart administrator: %s", error)
        return False
    return member.status in {"creator", "administrator"}


def _chart_help(*, unbound: bool) -> str:
    """@brief 渲染图表命令帮助 / Render chart-command help text.

    @param unbound 当前群组是否尚未绑定代币 / Whether the current group has no bound token.
    @return 用户可见帮助文本 / User-visible help text.
    """

    prefix = (
        "此群组尚未绑定代币。\n\n管理员可使用以下命令绑定:\n"
        if unbound
        else "命令格式不正确。\n\n管理员绑定代币:\n"
    )
    return (
        prefix
        + "/chart bind <chain> <CA>\n\n"
        + ("" if unbound else "管理员清除代币绑定:\n/chart clear\n\n")
        + "示例:\n/chart bind sol 2z9nPFtFRFwTTpQ6RpamUzsMfmF65Y3g14wu5FLj5rWC"
    )
