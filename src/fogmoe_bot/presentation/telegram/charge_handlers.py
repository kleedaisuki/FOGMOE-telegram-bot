"""@brief 充值 Telegram 薄适配器 / Thin Telegram adapters for top-up use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.service import (
    ECONOMY_SERVICE_DATA_KEY,
    EconomyService,
)
from fogmoe_bot.application.economy.topup import ApproveTopUp

from .runtime_settings import telegram_runtime_settings


@dataclass(frozen=True, slots=True)
class TopUpPackage:
    """@brief 充值套餐 DTO / Top-up package DTO.

    @param cents 价格分 / Price in cents.
    @param coins 付费金币 / Paid coins.
    """

    cents: int
    coins: int


_PACKAGES = (
    TopUpPackage(199, 50),
    TopUpPackage(299, 100),
    TopUpPackage(499, 200),
)
"""@brief 稳定充值套餐 / Stable top-up packages."""


async def charge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 解析 ``/charge <UUID>`` 并渲染原子兑换结果 / Parse ``/charge <UUID>`` and render atomic redemption.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    service = _service(context)
    if not await service.account_exists(user.id):
        await message.reply_text(
            "❌ 请先使用 /me 命令注册个人信息后再使用充值功能。\n"
            "Please register first using the /me command before charging."
        )
        return
    args = tuple(context.args or ())
    if len(args) != 1:
        await message.reply_text(
            "⚠️ 请输入正确的充值卡密！\n使用方法: /charge <卡密码>\n\n"
            "🔹 卡密格式例如: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\n\n"
            "Please enter a valid redemption code!\nUsage: /charge <code>"
        )
        return
    processing = await message.reply_text(
        "⏳ 正在处理您的充值请求，请稍候...\n"
        "Processing your charge request, please wait..."
    )
    result = await service.redeem(
        user.id,
        args[0],
        redeemed_at=datetime.now(),
        idempotency_key=f"telegram:charge:{update.update_id}:{user.id}",
    )
    if result.code is EconomyCode.SUCCESS:
        await processing.edit_text(
            "✅ 充值成功！\n\n"
            f"🎟️ 卡密: {args[0]}\n"
            f"💰 充值金额: +{result.amount} 金币\n"
            f"💳 充值前余额: {result.balance - result.amount} 金币\n"
            f"💎 当前余额: {result.balance} 金币\n\n"
            "感谢您的支持！\n\nCharge successful!\n"
            f"Added: {result.amount} coins\nCurrent balance: {result.balance} coins\n"
            "Thank you for your support!"
        )
        return
    reason = _redemption_error(result.code, result.used_by, result.used_at, user.id)
    await processing.edit_text(
        f"❌ 充值失败\n原因: {reason}\n\n"
        "如需帮助，请联系机器人管理员 @ScarletKc\n\n"
        f"Charge failed\nReason: {reason}\n"
        "For assistance, please contact the bot admin @ScarletKc"
    )


async def recharge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 渲染主动联系管理员的充值套餐 / Render administrator-contact top-up packages.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    status = await _service(context).topup_status(user.id)
    if not status.exists:
        await message.reply_text(
            "❌ 请先使用 /me 命令注册个人信息后再使用充值功能。\n"
            "Please register first using the /me command before charging."
        )
        return
    if status.blocked_until is not None and status.blocked_until > datetime.now():
        await message.reply_text(_block_text(status.blocked_until))
        return
    await message.reply_text(
        "【充值须知】\n"
        "目前仅支持用户主动私聊管理员充值。请务必核对管理员账号，谨防假冒！"
        "官方绝不会主动私信索要财物，请谨慎甄别，拒绝第三方渠道。\n\n"
        "请选择充值套餐，系统会将请求转发给管理员 @ScarletKc ：",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"{_price(package.cents)} - {package.coins}金币",
                        callback_data=(f"topup_req_{package.cents}_{package.coins}"),
                    )
                ]
                for package in _PACKAGES
            ]
        ),
    )


async def topup_request_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 将类型化套餐请求转发给管理员 / Forward a typed package request to the administrator.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    await query.answer()
    status = await _service(context).topup_status(query.from_user.id)
    if status.blocked_until is not None and status.blocked_until > datetime.now():
        await query.edit_message_text(_block_text(status.blocked_until))
        return
    package = _parse_request(query.data)
    if package is None or package not in _PACKAGES:
        await query.edit_message_text("充值请求数据无效，请重新发起。")
        return
    user_name = query.from_user.username or str(query.from_user.id)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "确认发放",
                    callback_data=(
                        f"topup_admin_approve_{query.from_user.id}_{package.coins}_{package.cents}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "拒绝",
                    callback_data=(
                        f"topup_admin_reject_{query.from_user.id}_{package.coins}_{package.cents}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "禁用1天",
                    callback_data=(
                        f"topup_admin_block_{query.from_user.id}_{package.coins}_{package.cents}"
                    ),
                )
            ],
        ]
    )
    await context.bot.send_message(
        chat_id=telegram_runtime_settings(context).administrator_id,
        text=(
            "收到充值请求：\n"
            f"用户: @{user_name} (ID: {query.from_user.id})\n"
            f"套餐: {_price(package.cents)} -> {package.coins}金币\n"
            "请核对付款后点击下方按钮处理。"
        ),
        reply_markup=keyboard,
    )
    await query.edit_message_text(
        f"已通知管理员 @ScarletKc 处理您的充值请求"
        f"（{_price(package.cents)} -> {package.coins}金币）。"
    )


async def topup_admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 幂等处理管理员充值决策 / Idempotently process an administrator top-up decision.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    if query.from_user.id != telegram_runtime_settings(context).administrator_id:
        await query.answer("您没有权限处理该请求。", show_alert=True)
        return
    await query.answer()
    parsed = _parse_admin(query.data)
    if parsed is None:
        await query.edit_message_text("请求数据无效。")
        return
    action, target_user_id, package = parsed
    service = _service(context)
    status = await service.topup_status(target_user_id)
    if not status.exists:
        await query.edit_message_text(
            f"用户不存在，无法处理充值请求（ID: {target_user_id}）。"
        )
        return
    if action == "approve":
        result = await service.approve_topup(
            ApproveTopUp(
                target_user_id,
                package.coins,
                f"telegram:topup:approve:{update.update_id}:{target_user_id}",
            )
        )
        await query.edit_message_text(
            f"已发放充值：{_price(package.cents)} -> {result.coins}金币\n"
            f"用户: {result.name} (ID: {target_user_id})"
        )
        await context.bot.send_message(
            target_user_id,
            f"充值成功！已到账 {result.coins} 金币（{_price(package.cents)}）。",
        )
        return
    if action == "block":
        deadline = datetime.now() + timedelta(days=1)
        result = await service.block_recharge(target_user_id, deadline)
        await query.edit_message_text(
            "已禁止用户 1 天内使用 /recharge。\n"
            f"用户: {result.name} (ID: {target_user_id})\n"
            f"截止时间: {deadline:%Y-%m-%d %H:%M:%S}"
        )
        await context.bot.send_message(target_user_id, _block_text(deadline))
        return
    await query.edit_message_text(
        f"已拒绝充值请求：{_price(package.cents)} -> {package.coins}金币\n"
        f"用户: {status.name} (ID: {target_user_id})"
    )
    await context.bot.send_message(
        target_user_id,
        f"充值请求未通过（{_price(package.cents)}）。如有疑问请联系管理员 @ScarletKc 。",
    )


async def admin_create_code(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 校验管理员输入并创建卡密 / Validate administrator input and create redemption codes.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if user.id != telegram_runtime_settings(context).administrator_id:
        await message.reply_text("❌ 您没有足够的权限执行此操作\n您不是管理员")
        return
    args = tuple(context.args or ())
    if len(args) != 2:
        await message.reply_text(
            "⚠️ 使用方法: /create_code <生成数量> <每个卡密的金币数>\n"
            "例如: /create_code 5 100"
        )
        return
    try:
        count, amount = int(args[0]), int(args[1])
        codes = await _service(context).create_codes(count, amount)
    except ValueError:
        await message.reply_text("⚠️ 生成数量必须在1-20之间，金币数必须在1-10000之间")
        return
    listing = "\n\n".join(
        f"{index}. `{code}` - {amount}金币" for index, code in enumerate(codes, 1)
    )
    await message.reply_text(
        f"✅ 成功生成 {len(codes)} 个充值卡密，每个价值 {amount} 金币：\n\n"
        f"{listing}\n\n💡 提示：请保存这些卡密，它们只会显示一次！"
    )


def _service(context: ContextTypes.DEFAULT_TYPE) -> EconomyService:
    """@brief 获取已装配经济服务 / Resolve the assembled economy service.

    @param context PTB callback context / PTB callback context.
    @return 经济服务 / Economy service.
    """

    candidate = context.application.bot_data.get(ECONOMY_SERVICE_DATA_KEY)
    if not isinstance(candidate, EconomyService):
        raise RuntimeError("Economy service is not configured")
    return candidate


def _parse_request(value: str) -> TopUpPackage | None:
    """@brief 严格解析用户套餐 callback / Strictly parse a user package callback.

    @param value callback_data / callback_data.
    @return 套餐；无效为 None / Package, or None.
    """

    parts = value.split("_")
    if len(parts) != 4 or parts[:2] != ["topup", "req"]:
        return None
    try:
        return TopUpPackage(int(parts[2]), int(parts[3]))
    except ValueError:
        return None


def _parse_admin(value: str) -> tuple[str, int, TopUpPackage] | None:
    """@brief 严格解析管理员 callback / Strictly parse an administrator callback.

    @param value callback_data / callback_data.
    @return ``(action, user_id, package)``；无效为 None / Parsed tuple, or None.
    """

    parts = value.split("_")
    if len(parts) != 6 or parts[:2] != ["topup", "admin"]:
        return None
    if parts[2] not in {"approve", "reject", "block"}:
        return None
    try:
        user_id = int(parts[3])
        package = TopUpPackage(int(parts[5]), int(parts[4]))
    except ValueError:
        return None
    return parts[2], user_id, package


def _price(cents: int) -> str:
    """@brief 格式化美元分 / Format US-dollar cents.

    @param cents 价格分 / Price in cents.
    @return ``$x.xx`` / ``$x.xx``.
    """

    value = (Decimal(cents) / Decimal(100)).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN,
    )
    return f"${value}"


def _block_text(deadline: datetime) -> str:
    """@brief 格式化充值禁用提示 / Format a recharge-block prompt.

    @param deadline 截止时间 / Deadline.
    @return 用户可见文本 / User-visible text.
    """

    return f"您暂时无法使用 /recharge，请在 {deadline:%Y-%m-%d %H:%M:%S} 后再试。"


def _redemption_error(
    code: EconomyCode,
    used_by: int | None,
    used_at: datetime | None,
    user_id: int,
) -> str:
    """@brief 渲染卡密拒绝原因 / Render a redemption rejection.

    @param code 结果代码 / Result code.
    @param used_by 已使用者 / Existing redeemer.
    @param used_at 已兑换时间 / Existing redemption time.
    @param user_id 当前用户 / Current user.
    @return 拒绝文本 / Rejection text.
    """

    if code is EconomyCode.INVALID:
        return "卡密格式无效，请确保输入了正确的充值卡密"
    if code is EconomyCode.NOT_FOUND:
        return "无效的充值卡密，此卡密不存在或已被删除"
    if code is EconomyCode.ALREADY_USED:
        when = used_at.strftime("%Y-%m-%d %H:%M:%S") if used_at else "未知时间"
        owner = "您" if used_by == user_id else "其他用户"
        return f"此卡密已被{owner}在 {when} 使用"
    return "充值处理过程中出现错误，请联系管理员"


__all__ = [
    "admin_create_code",
    "charge_command",
    "recharge_command",
    "topup_admin_callback",
    "topup_request_callback",
]
