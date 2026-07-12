"""Telegram handler for group chart-token bindings."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from fogmoe_bot.application.crypto.workflow import BindChartToken, ClearChartToken
from fogmoe_bot.domain.crypto import Blockchain, ChartToken, ContractAddress

from .common import crypto_service


logger = logging.getLogger(__name__)

_SUPPORTED_CHAIN_TEXT = "sol, solana, eth, ethereum, blast, bsc, bnb"


async def chart_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Parse ``/chart``, authorize mutations, and render the result."""

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat is None or user is None or message is None:
        return
    if chat.type not in {"group", "supergroup"}:
        await message.reply_text("此命令只能在群组中使用。")
        return
    args = tuple(context.args or ())
    service = crypto_service(context)
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
    """Verify group-administrator identity at the Telegram boundary."""

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
    """Render the existing chart help text."""

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
