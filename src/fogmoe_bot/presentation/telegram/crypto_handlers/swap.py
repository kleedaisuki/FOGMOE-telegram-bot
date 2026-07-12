"""Telegram handler for FOGMOE token-swap requests."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from fogmoe_bot.application.crypto.workflow import (
    CryptoResultCode,
    SubmitTokenSwap,
    SwapSubmissionResult,
    TokenSwapRequest,
)
from fogmoe_bot.domain.crypto import SWAP_MINIMUM, CoinStake, SolanaWalletAddress

from .common import crypto_service


async def swap_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Parse ``/swap`` and call the atomic swap use case."""

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    service = crypto_service(context)
    account = await service.account_snapshot(user.id)
    if not account.registered:
        await message.reply_text(
            "***请先使用 /me 命令注册您的账户。***\n"
            "Please register first using the /me command.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    pending = await service.pending_swap(user.id)
    if pending is not None:
        await message.reply_text(
            _pending_swap_text(pending),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    args = tuple(context.args or ())
    if len(args) != 2:
        await message.reply_text(_swap_help(), parse_mode=ParseMode.MARKDOWN)
        return
    try:
        amount = CoinStake(int(args[0]))
    except ValueError, TypeError:
        await message.reply_text(
            "***请输入有效的金币数量。***\n***Please enter a valid coin amount.***",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if int(amount) < SWAP_MINIMUM:
        await message.reply_text(
            f"***最低兑换数量为{SWAP_MINIMUM}金币。***\n"
            f"***Minimum exchange amount is {SWAP_MINIMUM} coins.***",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        wallet = SolanaWalletAddress(args[1])
    except ValueError:
        await message.reply_text(
            "***您输入的不是有效的Solana钱包地址。***\n"
            "请确认后重试。\n\n"
            "***The address you entered is not a valid Solana wallet address.***\n"
            "Please check and try again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    result = await service.submit_swap(
        SubmitTokenSwap(
            user_id=user.id,
            username=user.username or "Unknown",
            wallet=wallet,
            amount=amount,
            idempotency_key=f"telegram:crypto:swap:{update.update_id}:{user.id}",
        )
    )
    await message.reply_text(
        _swap_result_text(result, amount=amount, wallet=wallet),
        parse_mode=ParseMode.MARKDOWN,
    )


def _pending_swap_text(request: TokenSwapRequest) -> str:
    """Render the existing pending-request text."""

    requested_at = request.requested_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "***您已有一个正在处理中的兑换请求***\n\n"
        f"金币数量: ***{int(request.amount)}***\n"
        f"接收钱包: ***{request.wallet}***\n"
        f"申请时间: ***{requested_at}***\n\n"
        "请等待该请求处理完成后再进行新的兑换操作。处理时间可能长达72小时。\n\n"
        "***You already have a pending exchange request***\n\n"
        f"Amount: ***{int(request.amount)}*** coins\n"
        f"Receiving wallet: ***{request.wallet}***\n"
        f"Request time: ***{requested_at}***\n\n"
        "Please wait for it to be processed before making a new exchange. "
        "Processing may take up to 72 hours."
    )


def _swap_help() -> str:
    """Return the existing bilingual swap help."""

    return (
        "***该命令用于将您的金币兑换为Solana链上的$FOGMOE代币***\n\n"
        "用法: `/swap <金币数量> <Solana钱包地址>`\n"
        "示例: `/swap 10000 5iz3epFDf9SKvLNHWQ42f4wMMrENaudE9eMkxfBLFd2n`\n\n"
        f"***最低兑换数量为{SWAP_MINIMUM}金币。***\n"
        "***当前兑换比例为1:1（1金币=1$FOGMOE），该比例可能随时调整，"
        "最终兑换比例以实际操作为准。***\n\n"
        "注意: 兑换处理时间可能长达72小时。\n\n"
        "访问 [token.fog.moe](https://token.fog.moe/) 了解关于$FOGMOE代币的详细信息。\n\n"
        "***This command is used to exchange your coins for $FOGMOE tokens on the Solana chain***\n\n"
        "Usage: `/swap <amount> <Solana wallet address>`\n"
        "Example: `/swap 10000 5iz3epFDf9SKvLNHWQ42f4wMMrENaudE9eMkxfBLFd2n`\n\n"
        f"***Minimum exchange amount is {SWAP_MINIMUM} coins.***\n"
        "***Current exchange rate is 1:1 (1 coin = 1 $FOGMOE). This rate may "
        "change at any time; the final rate is determined during processing.***\n\n"
        "Note: Processing time may take up to 72 hours.\n\n"
        "Visit [token.fog.moe](https://token.fog.moe/) to learn more about $FOGMOE tokens."
    )


def _swap_result_text(
    result: SwapSubmissionResult,
    *,
    amount: CoinStake,
    wallet: SolanaWalletAddress,
) -> str:
    """Render the existing swap-submission result."""

    if result.code is CryptoResultCode.NOT_REGISTERED:
        return (
            "***请先使用 /me 命令注册您的账户。***\n"
            "Please register first using the /me command."
        )
    if result.code is CryptoResultCode.PENDING_SWAP:
        if result.request is None:
            return (
                "***您已有一个正在处理中的兑换请求。***\n"
                "***You already have a pending exchange request.***"
            )
        return _pending_swap_text(result.request)
    if result.code is CryptoResultCode.INSUFFICIENT_COINS:
        return (
            "***您的金币不足。***\n"
            f"当前余额: ***{result.balance}*** 金币，需要: ***{int(amount)}*** 金币。\n\n"
            "***You don't have enough coins.***\n"
            f"Current balance: ***{result.balance}*** coins, "
            f"required: ***{int(amount)}*** coins."
        )
    return (
        "***您已成功提交兑换请求：***\n\n"
        f"金币数量: ***{int(amount)}***\n"
        f"接收钱包: ***{wallet}***\n\n"
        "***当前兑换比例为1:1（1金币=1$FOGMOE），该比例可能随时调整，"
        "最终兑换比例以实际处理为准。***\n\n"
        "请耐心等待处理，兑换可能需要长达72小时。完成后，$FOGMOE代币将发送到您提供的钱包地址。\n\n"
        "访问 [token.fog.moe](https://token.fog.moe/) 了解关于$FOGMOE代币的详细信息。\n\n"
        "***You have successfully submitted an exchange request:***\n\n"
        f"Amount: ***{int(amount)}*** coins\n"
        f"Receiving wallet: ***{wallet}***\n\n"
        "***Current exchange rate is 1:1 (1 coin = 1 $FOGMOE). This rate may "
        "change at any time; the final rate is determined during processing.***\n\n"
        "Please be patient as processing may take up to 72 hours. Once completed, "
        "$FOGMOE tokens will be sent to the wallet address you provided.\n\n"
        "Visit [token.fog.moe](https://token.fog.moe/) to learn more about $FOGMOE tokens."
    )
