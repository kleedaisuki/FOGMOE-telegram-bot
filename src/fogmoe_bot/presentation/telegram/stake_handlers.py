"""@brief 质押 Telegram 薄适配器 / Thin Telegram adapter for staking."""

from __future__ import annotations

from datetime import datetime
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from fogmoe_bot.application.economy.staking import (
    CollectStakeReward,
    OpenStake,
    StakingService,
    WithdrawStake,
)
from fogmoe_bot.domain.economy import (
    REWARD_INTERVAL_DAYS,
    StakeAction,
    StakeDecision,
)

STAKING_SERVICE_DATA_KEY = "economy.staking.service"
"""@brief ``bot_data`` 中质押服务的稳定键 / Stable ``bot_data`` key for the staking service."""


async def stake_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 解析 ``/stake`` 并渲染应用决策 / Parse ``/stake`` and render an application decision.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    service = _service(context)
    if service is None:
        await message.reply_text(_UNAVAILABLE_TEXT)
        return
    args = tuple(context.args or ())
    if not args:
        decision = await service.status(user.id, now=datetime.now())
        await message.reply_text(
            _render_status(decision, user.id),
            reply_markup=_stake_keyboard(user.id) if decision.position else None,
        )
        return
    try:
        amount = int(args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.reply_text(
            "请输入有效的质押金额。格式: /stake <数量>\n"
            "Please enter a valid stake amount. Format: /stake <amount>"
        )
        return
    decision = await service.open(
        OpenStake(
            user_id=user.id,
            amount=amount,
            requested_at=datetime.now(),
            idempotency_key=f"telegram:stake:open:{update.update_id}:{user.id}",
        )
    )
    await message.reply_text(_render_open(decision, amount))


async def stake_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 严格解析 ``stake_`` callback 并执行领奖或取回 / Strictly parse a ``stake_`` callback and collect or withdraw.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or not isinstance(query.data, str):
        return
    parsed = _parse_callback(query.data)
    if parsed is None:
        await query.answer("无效的质押操作。", show_alert=True)
        return
    action, target_user_id = parsed
    if user.id != target_user_id:
        await query.answer("这不是你的质押，你不能操作。", show_alert=True)
        return
    service = _service(context)
    if service is None:
        await query.answer(_UNAVAILABLE_TEXT, show_alert=True)
        return
    key = f"telegram:stake:{action}:{update.update_id}:{user.id}"
    if action == "collect":
        decision = await service.collect(
            CollectStakeReward(user.id, datetime.now(), key)
        )
        await _render_collection(query, decision, user.id)
        return
    decision = await service.withdraw(WithdrawStake(user.id, datetime.now(), key))
    await _render_withdrawal(query, decision)


def _service(context: ContextTypes.DEFAULT_TYPE) -> StakingService | None:
    """@brief 从组合根获取类型化服务 / Resolve the typed service from the composition root.

    @param context PTB callback context / PTB callback context.
    @return 质押服务；未装配为 None / Staking service, or None when not assembled.
    """

    candidate = context.application.bot_data.get(STAKING_SERVICE_DATA_KEY)
    return candidate if isinstance(candidate, StakingService) else None


def _parse_callback(value: str) -> tuple[str, int] | None:
    """@brief 严格解析旧 callback namespace / Strictly parse the legacy callback namespace.

    @param value callback_data / callback_data.
    @return ``(action, user_id)``；无效为 None / ``(action, user_id)``, or None.
    """

    parts = value.split("_")
    if (
        len(parts) != 3
        or parts[0] != "stake"
        or parts[1]
        not in {
            "collect",
            "withdraw",
        }
    ):
        return None
    try:
        user_id = int(parts[2])
    except ValueError:
        return None
    return (parts[1], user_id) if user_id > 0 else None


def _stake_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """@brief 构造保持旧 namespace 的质押按钮 / Build staking buttons preserving the legacy namespace.

    @param user_id 按钮所有者 / Button owner.
    @return Telegram inline keyboard / Telegram inline keyboard.
    """

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "领取回报", callback_data=f"stake_collect_{user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "取出本金", callback_data=f"stake_withdraw_{user_id}"
                )
            ],
        ]
    )


def _render_status(decision: StakeDecision, user_id: int) -> str:
    """@brief 渲染质押状态 / Render staking status.

    @param decision 应用决策 / Application decision.
    @param user_id 用户 ID / User ID.
    @return 保持旧语义的文本 / Text preserving legacy semantics.
    """

    del user_id
    if decision.action is StakeAction.NOT_REGISTERED:
        return (
            "请先使用 /me 命令注册您的账户。\n"
            "Please register first using the /me command."
        )
    prefix = (
        f"当前质押回报率: {decision.daily_rate:.2f}%/天\n"
        f"回报按天累计，每{REWARD_INTERVAL_DAYS}天可领取一次。\n"
        "取出本金将收取 3% 手续费。\n"
    )
    if decision.position is None:
        return prefix + (
            "您当前没有质押任何金币。\n使用 /stake <数量> 命令来质押金币。"
        )
    return prefix + (
        f"您当前已质押: {decision.position.amount} 金币\n"
        f"质押时间: {decision.position.staked_at:%Y-%m-%d %H:%M:%S}\n"
        f"可领取回报: {decision.reward} 金币"
    )


def _render_open(decision: StakeDecision, amount: int) -> str:
    """@brief 渲染开仓结果 / Render an opening decision.

    @param decision 应用决策 / Application decision.
    @param amount 请求本金 / Requested principal.
    @return 用户可见文本 / User-visible text.
    """

    if decision.action is StakeAction.NOT_REGISTERED:
        return _render_status(decision, 0)
    if decision.action is StakeAction.ALREADY_STAKED:
        return (
            "您已经有质押的金币。如果要增加质押金额，请先取出当前质押。\n"
            "You already have staked coins. If you want to increase your stake, "
            "please withdraw your current stake first."
        )
    if decision.action is StakeAction.INSUFFICIENT_COINS:
        return (
            f"您没有足够的金币。当前余额: {decision.available} 金币。\n"
            f"You don't have enough coins. Current balance: {decision.available} coins."
        )
    return (
        f"成功质押 {amount} 金币！当前回报率为 {decision.daily_rate:.2f}%/天。\n"
        f"每{REWARD_INTERVAL_DAYS}天可领取一次回报。\n"
        f"Successfully staked {amount} coins! Current reward rate is "
        f"{decision.daily_rate:.2f}% everyday.\n"
        f"You can collect rewards once every {REWARD_INTERVAL_DAYS} days."
    )


async def _render_collection(
    query: CallbackQuery,
    decision: StakeDecision,
    user_id: int,
) -> None:
    """@brief 投递领奖结果 / Deliver a collection decision.

    @param query Telegram CallbackQuery / Telegram CallbackQuery.
    @param decision 应用决策 / Application decision.
    @param user_id 用户 ID / User ID.
    @return None / None.
    """

    if decision.action is StakeAction.NO_STAKE:
        await query.answer("您没有质押任何金币。", show_alert=True)
        return
    if decision.action is StakeAction.TOO_EARLY:
        await query.answer(
            f"没有可领取的回报。需要等待至少{REWARD_INTERVAL_DAYS}天。",
            show_alert=True,
        )
        return
    if decision.action is StakeAction.BELOW_ONE_COIN:
        await query.answer(
            f"已满{REWARD_INTERVAL_DAYS}天，但累计回报不足 1 金币，继续质押会继续累计。",
            show_alert=True,
        )
        return
    if decision.action is StakeAction.POOL_EMPTY:
        await query.answer("奖励池余额不足，暂时无法发放回报。", show_alert=True)
        return
    if decision.position is None:
        await query.answer(_UNAVAILABLE_TEXT, show_alert=True)
        return
    await query.edit_message_text(
        f"您已成功领取 {decision.reward} 金币的回报！\n"
        f"当前质押金额: {decision.position.amount} 金币\n"
        f"当前回报率: {decision.daily_rate:.2f}%/天",
        reply_markup=_stake_keyboard(user_id),
    )
    await query.answer(f"成功领取 {decision.reward} 金币回报！", show_alert=True)


async def _render_withdrawal(query: CallbackQuery, decision: StakeDecision) -> None:
    """@brief 投递取回结果 / Deliver a withdrawal decision.

    @param query Telegram CallbackQuery / Telegram CallbackQuery.
    @param decision 应用决策 / Application decision.
    @return None / None.
    """

    if decision.action is StakeAction.NO_STAKE:
        await query.answer("您没有质押任何金币。", show_alert=True)
        return
    if decision.action is not StakeAction.WITHDRAWN:
        await query.answer(_UNAVAILABLE_TEXT, show_alert=True)
        return
    if decision.reward > 0:
        result = (
            f"您已取出质押本金 {decision.principal} 金币"
            f"（手续费 {decision.fee} 金币），并获得回报 {decision.reward} 金币！"
        )
    else:
        result = (
            f"您已取出质押本金 {decision.principal} 金币"
            f"（手续费 {decision.fee} 金币）。\n"
            "本次未发放回报。"
        )
    await query.edit_message_text(
        f"{result}\n\n"
        f"当前质押回报率: {decision.daily_rate:.2f}%/天\n"
        "您目前没有质押金币。\n"
        "使用 /stake <数量> 命令来质押金币。"
    )
    await query.answer(result, show_alert=True)


_UNAVAILABLE_TEXT = "质押服务暂时不可用，请稍后再试。"
"""@brief 质押服务未装配时的稳定提示 / Stable prompt when staking is not assembled."""


__all__ = [
    "STAKING_SERVICE_DATA_KEY",
    "stake_callback",
    "stake_command",
]
