"""Telegram command and callback handlers for BTC predictions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from fogmoe_bot.application.crypto.workflow import (
    ActivePrediction,
    CreateBtcPrediction,
    CryptoResultCode,
    MarketDataUnavailable,
    PredictionCreationResult,
)
from fogmoe_bot.domain.crypto import (
    BTC_PREDICTION_MINIMUM,
    CoinStake,
    PredictionDirection,
    PriceQuote,
)

from .common import crypto_service


_TRADING_VIEW_URL = "https://cn.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P"

_PREDICT_AMOUNT_OWNED = re.compile(
    r"^crypto_amount_(?P<amount>custom|[0-9]+)_user_(?P<owner>[0-9]+)$"
)
_PREDICT_AMOUNT_LEGACY = re.compile(r"^crypto_amount_(?P<amount>custom|[0-9]+)$")
_PREDICT_DIRECTION_OWNED = re.compile(
    r"^crypto_predict_(?P<direction>up|down)_user_(?P<owner>[0-9]+)_(?P<amount>[0-9]+)$"
)
_PREDICT_DIRECTION_LEGACY = re.compile(
    r"^crypto_predict_(?P<direction>up|down)_(?P<amount>[0-9]+)$"
)
_PREDICT_CANCEL = re.compile(r"^crypto_cancel(?:_user_(?P<owner>[0-9]+))?$")


@dataclass(frozen=True, slots=True)
class _AmountCallback:
    """A parsed amount callback."""

    amount: CoinStake | None
    owner_id: int | None


@dataclass(frozen=True, slots=True)
class _DirectionCallback:
    """A parsed direction callback."""

    direction: PredictionDirection
    amount: CoinStake
    owner_id: int | None


@dataclass(frozen=True, slots=True)
class _CancelCallback:
    """A parsed cancellation callback."""

    owner_id: int | None


type _CryptoCallback = _AmountCallback | _DirectionCallback | _CancelCallback


async def btc_predict_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Parse ``/btc_predict`` and render the application result."""

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    try:
        overview = await crypto_service(context).prediction_overview(user.id)
    except MarketDataUnavailable:
        await message.reply_text("获取比特币价格失败，请稍后再试。")
        return
    if not overview.account.registered:
        await message.reply_text(
            "请先使用 /me 命令注册您的账户。\n"
            "Please register first using the /me command."
        )
        return
    if overview.active is not None:
        await message.reply_text(_active_prediction_text(overview.active))
        return
    args = tuple(context.args or ())
    if args:
        try:
            amount = CoinStake(int(args[0]))
        except ValueError, TypeError:
            await message.reply_text(
                "请输入有效的投入金额。格式: /btc_predict <金额>\n"
                "或直接使用 /btc_predict 选择金额。"
            )
            return
        await _show_direction_for_amount(
            update,
            context,
            amount=amount,
            quote=overview.quote,
        )
        return
    if overview.quote is None:
        raise RuntimeError("Prediction overview omitted both active state and quote")
    await message.reply_text(
        _prediction_intro(overview.quote),
        reply_markup=_amount_keyboard(user.id),
        parse_mode=ParseMode.MARKDOWN,
    )


async def crypto_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Parse the stable ``crypto_*`` callback namespace."""

    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    try:
        callback = _parse_callback(query.data)
    except ValueError:
        await query.answer("按钮数据格式错误", show_alert=True)
        return
    user_id = query.from_user.id
    owner_id = callback.owner_id
    if owner_id is not None and owner_id != user_id:
        await query.answer(
            "这不是您的预测，您不能操作他人的预测。",
            show_alert=True,
        )
        return
    if isinstance(callback, _CancelCallback):
        await query.answer()
        await query.edit_message_text("已取消预测。")
        return
    if isinstance(callback, _AmountCallback):
        await query.answer()
        if callback.amount is None:
            await query.edit_message_text(
                "请直接发送命令指定您要投入的金额，例如:\n"
                "/btc_predict 100\n\n"
                f"（最低投入{BTC_PREDICTION_MINIMUM}金币）"
            )
            return
        await _edit_direction_for_amount(
            update,
            context,
            amount=callback.amount,
        )
        return
    await query.answer()
    await _create_prediction_from_callback(update, context, callback)


async def _show_direction_for_amount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    amount: CoinStake,
    quote: PriceQuote | None,
) -> None:
    """Render direction selection for a command argument."""

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if int(amount) < BTC_PREDICTION_MINIMUM:
        await message.reply_text(
            f"最低投入金额为{BTC_PREDICTION_MINIMUM}金币。请重新选择。"
        )
        return
    account = await crypto_service(context).account_snapshot(user.id)
    if account.balance < int(amount):
        await message.reply_text(
            f"您的金币不足。当前余额: {account.balance} 金币，"
            f"需要: {int(amount)} 金币。"
        )
        return
    if quote is None:
        try:
            quoted = await crypto_service(context).quote_for_stake(user.id, amount)
        except MarketDataUnavailable:
            await message.reply_text("获取比特币价格失败，请稍后再试。")
            return
        if quoted.active is not None:
            await message.reply_text(_active_prediction_text(quoted.active))
            return
        quote = quoted.quote
    if quote is None:
        raise RuntimeError("Stake quote is missing")
    await message.reply_text(
        _direction_prompt(amount, quote),
        reply_markup=_direction_keyboard(user.id, amount),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _edit_direction_for_amount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    amount: CoinStake,
) -> None:
    """Edit direction selection for an amount callback."""

    query = update.callback_query
    if query is None:
        return
    if int(amount) < BTC_PREDICTION_MINIMUM:
        await query.edit_message_text(
            f"最低投入金额为{BTC_PREDICTION_MINIMUM}金币，请使用 /btc_predict 重新开始。"
        )
        return
    try:
        overview = await crypto_service(context).quote_for_stake(
            query.from_user.id, amount
        )
    except MarketDataUnavailable:
        await query.edit_message_text("获取比特币价格失败，请稍后再试。")
        return
    if not overview.account.registered:
        await query.edit_message_text("请先使用 /me 命令注册您的账户。")
        return
    if overview.active is not None:
        await query.edit_message_text(_active_prediction_text(overview.active))
        return
    if overview.account.balance < int(amount):
        await query.edit_message_text(
            f"您的金币不足。当前余额: {overview.account.balance} 金币，"
            f"需要: {int(amount)} 金币。\n请使用 /btc_predict 重新选择金额。"
        )
        return
    if overview.quote is None:
        raise RuntimeError("Amount callback quote is missing")
    await query.edit_message_text(
        _direction_prompt(amount, overview.quote),
        reply_markup=_direction_keyboard(query.from_user.id, amount),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _create_prediction_from_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    callback: _DirectionCallback,
) -> None:
    """Convert a direction callback into a typed prediction command."""

    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return
    command = CreateBtcPrediction(
        user_id=query.from_user.id,
        chat_id=chat.id,
        direction=callback.direction,
        amount=callback.amount,
        requested_at=datetime.now(UTC),
        idempotency_key=(
            f"telegram:crypto:prediction:{update.update_id}:{query.from_user.id}"
        ),
    )
    try:
        result = await crypto_service(context).create_prediction(command)
    except MarketDataUnavailable:
        await query.edit_message_text("获取比特币价格失败，请稍后再试。")
        return
    await _render_prediction_creation(query, result)


async def _render_prediction_creation(
    query: object, result: PredictionCreationResult
) -> None:
    """Edit the callback message into a prediction-creation result."""

    edit = getattr(query, "edit_message_text")
    if result.code is CryptoResultCode.NOT_REGISTERED:
        await edit("请先使用 /me 命令注册您的账户。")
        return
    if result.code is CryptoResultCode.INSUFFICIENT_COINS:
        await edit(f"创建预测失败: 金币不足。当前余额: {result.balance} 金币。")
        return
    if result.code is CryptoResultCode.ACTIVE_PREDICTION:
        if result.prediction is None:
            raise RuntimeError("Active-prediction result omitted its prediction")
        await edit(_active_prediction_text(result.prediction))
        return
    prediction = result.prediction
    if prediction is None:
        raise RuntimeError("Successful prediction result omitted its prediction")
    direction = "上涨 ↗" if prediction.direction is PredictionDirection.UP else "下跌 ↘"
    await edit(
        "🎯 预测已创建!\n\n"
        f"预测方向: {direction}\n"
        f"投入金额: {int(prediction.amount)} 金币\n"
        f"起始价格: ${prediction.start_price.value:,.2f}\n"
        f"结束时间: {prediction.due_at.astimezone():%H:%M:%S}\n\n"
        f"📊 [点击查看比特币实时价格图表]({_TRADING_VIEW_URL})\n\n"
        "10分钟后系统将自动检查结果并发送通知。",
        parse_mode=ParseMode.MARKDOWN,
    )


def _parse_callback(data: str) -> _CryptoCallback:
    """Strictly parse current and legacy callback data."""

    cancel = _PREDICT_CANCEL.fullmatch(data)
    if cancel is not None:
        owner = cancel.group("owner")
        return _CancelCallback(int(owner) if owner is not None else None)
    amount_match = _PREDICT_AMOUNT_OWNED.fullmatch(data)
    if amount_match is None:
        amount_match = _PREDICT_AMOUNT_LEGACY.fullmatch(data)
    if amount_match is not None:
        raw_amount = amount_match.group("amount")
        owner = amount_match.groupdict().get("owner")
        return _AmountCallback(
            None if raw_amount == "custom" else CoinStake(int(raw_amount)),
            int(owner) if owner is not None else None,
        )
    direction_match = _PREDICT_DIRECTION_OWNED.fullmatch(data)
    if direction_match is None:
        direction_match = _PREDICT_DIRECTION_LEGACY.fullmatch(data)
    if direction_match is not None:
        owner = direction_match.groupdict().get("owner")
        return _DirectionCallback(
            PredictionDirection(direction_match.group("direction")),
            CoinStake(int(direction_match.group("amount"))),
            int(owner) if owner is not None else None,
        )
    raise ValueError("Unknown Crypto callback data")


def _amount_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Build the callback-compatible amount keyboard."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "20 金币",
                    callback_data=f"crypto_amount_20_user_{user_id}",
                ),
                InlineKeyboardButton(
                    "50 金币",
                    callback_data=f"crypto_amount_50_user_{user_id}",
                ),
                InlineKeyboardButton(
                    "100 金币",
                    callback_data=f"crypto_amount_100_user_{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "自定义金额",
                    callback_data=f"crypto_amount_custom_user_{user_id}",
                )
            ],
        ]
    )


def _direction_keyboard(user_id: int, amount: CoinStake) -> InlineKeyboardMarkup:
    """Build the callback-compatible direction keyboard."""

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "预测上涨 ↗",
                    callback_data=(f"crypto_predict_up_user_{user_id}_{int(amount)}"),
                ),
                InlineKeyboardButton(
                    "预测下跌 ↘",
                    callback_data=(f"crypto_predict_down_user_{user_id}_{int(amount)}"),
                ),
            ],
            [
                InlineKeyboardButton(
                    "取消",
                    callback_data=f"crypto_cancel_user_{user_id}",
                )
            ],
        ]
    )


def _prediction_intro(quote: PriceQuote) -> str:
    """Render the existing prediction rules."""

    return (
        "🔮 比特币价格预测 🔮\n\n"
        f"当前比特币价格: ${quote.value:,.2f}\n\n"
        "游戏规则:\n"
        "1. 预测10分钟后比特币价格是上涨还是下跌\n"
        f"2. 最低投入{BTC_PREDICTION_MINIMUM}金币\n"
        "3. 预测正确: 返还投入金额 + 80%奖励\n"
        "4. 预测错误: 损失全部投入金额\n\n"
        f"📊 [点击查看比特币实时价格图表]({_TRADING_VIEW_URL})\n\n"
        "请选择您要投入的金币数量:"
    )


def _direction_prompt(amount: CoinStake, quote: PriceQuote) -> str:
    """Render the existing direction-selection prompt."""

    return (
        f"您准备投入 {int(amount)} 金币进行比特币价格预测。\n"
        f"当前价格: ${quote.value:,.2f}\n\n"
        f"📊 [点击查看比特币实时价格图表]({_TRADING_VIEW_URL})\n\n"
        "请选择您的预测方向:"
    )


def _active_prediction_text(prediction: ActivePrediction) -> str:
    """Render the existing active-prediction text."""

    remaining = max(0, int((prediction.due_at - datetime.now(UTC)).total_seconds()))
    minutes, seconds = divmod(remaining, 60)
    direction = "上涨" if prediction.direction is PredictionDirection.UP else "下跌"
    return (
        "⚠️ 您已经有一个正在进行的预测！\n\n"
        f"预测方向: {direction}\n"
        f"投入金额: {int(prediction.amount)} 金币\n"
        f"起始价格: ${prediction.start_price.value:,.2f}\n"
        f"剩余时间: {minutes}分钟 {seconds}秒\n\n"
        "请等待此次预测结束后再开始新预测。"
    )
