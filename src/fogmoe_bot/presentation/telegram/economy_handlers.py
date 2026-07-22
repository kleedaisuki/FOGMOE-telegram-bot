"""@brief 经济 Telegram 薄适配器 / Thin Telegram adapters for economy use cases."""

from __future__ import annotations

import html
from datetime import date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.community import TaskClaimCommand
from fogmoe_bot.application.economy.referral import ReferralCommand, ReferralResult
from fogmoe_bot.application.economy.rewards import CheckInCommand
from fogmoe_bot.application.economy.service import (
    ECONOMY_SERVICE_DATA_KEY,
    EconomyService,
)

from .runtime_settings import telegram_runtime_settings

INVITATION_REWARD = 20
"""@brief 邀请双方旧奖励 / Legacy referral reward for both parties."""

_TASKS: dict[str, tuple[int, int, int, str]] = {
    "task_check_group1": (1, -1001870858408, 10, "@ScarletKc_Group"),
    "task_check_group2": (2, -1002053007005, 10, "@FOG_MOE"),
}
"""@brief callback 到任务配置的稳定映射 / Stable callback-to-task mapping."""


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 处理推荐参数后发送欢迎语 / Process a referral argument and then send the welcome message.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    args = tuple(context.args or ())
    if user is not None and args:
        try:
            referrer_id = int(args[0])
        except ValueError:
            referrer_id = 0
        if referrer_id > 0:
            result = await _service(context).bind_referral(
                ReferralCommand(
                    invited_user_id=user.id,
                    referrer_id=referrer_id,
                    invited_name=user.full_name,
                    invitation_reward=INVITATION_REWARD,
                    new_user_bonus=telegram_runtime_settings(context).new_user_bonus,
                    idempotency_key=f"telegram:ref:start:{update.update_id}:{user.id}",
                )
            )
            if result.code is EconomyCode.SUCCESS:
                await _send_referral_success(
                    update,
                    result,
                    new_user_bonus=telegram_runtime_settings(context).new_user_bonus,
                )
    chat = update.effective_chat
    if chat is not None:
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                "欢迎使用雾萌机器人喵！！我是雾萌娘，有什么可以帮到您的吗？输入 /help "
                "我会尽力帮助您的哦。\nWelcome to the FogMoeBot! Meow! I'm "
                "your assistant, is there anything I can help you with? Type /help "
                "and I'll do my best."
            ),
        )


async def checkin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 解析签到身份并渲染结果 / Parse check-in identity and render the result.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if not user.username:
        await message.reply_text(
            "您需要设置Telegram用户名才能使用签到功能。\n"
            "请在Telegram设置中设置用户名后再尝试。\n\n"
            "You need to set a Telegram username to use the check-in feature.\n"
            "Please set your username in Telegram settings and try again."
        )
        return
    result = await _service(context).check_in(
        CheckInCommand(
            user.id,
            date.today(),
            f"telegram:checkin:{update.update_id}:{user.id}",
        )
    )
    if result.code is EconomyCode.NOT_REGISTERED:
        await message.reply_text(
            "请先使用 /me 命令注册账户。\nPlease register first using the /me command."
        )
        return
    username = html.escape(user.username)
    if result.code is EconomyCode.SUCCESS:
        progress = min(result.consecutive_days, 31) / 31
        progress_bar = "".join(
            "🟢" if index / 10 <= progress else "⚪" for index in range(1, 11)
        )
        details = (
            f"距离最高奖励还有 {31 - result.consecutive_days} 天\n"
            f"{progress_bar} {int(progress * 100)}%\n\n"
            if result.consecutive_days < 31
            else "恭喜！你已达到最高奖励等级！🏆\n\n"
        )
        text = (
            "🎉 <b>签到成功</b> 🎉\n\n"
            f"用户: @{username}\n"
            f"连续签到: <b>{result.consecutive_days}</b> 天\n"
            f"今日奖励: <b>{result.reward}</b> 金币\n\n"
            f"{details}每天签到可获得金币奖励，连续签到奖励更多！"
        )
    else:
        text = (
            "⚠️ 您今天已经签到过了！请明天再来。\n\n"
            f"当前连续签到: <b>{result.consecutive_days}</b> 天\n"
            "请明天再来签到以继续你的连续签到记录！"
        )
    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def task_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 渲染任务菜单 / Render the task menu.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    del context
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "任务中心：\n"
        "任务1：请先加入我们的指定群组 @ScarletKc_Group，完成后领取10个硬币奖励。\n"
        "任务2：请先加入我们的指定群组 @FOG_MOE，完成后领取10个硬币奖励。",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "领取@ScarletKc_Group任务1奖励 - 10金币",
                        callback_data="task_check_group1",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "领取@FOG_MOE任务2奖励 - 10金币",
                        callback_data="task_check_group2",
                    )
                ],
                [InlineKeyboardButton("关闭任务窗口", callback_data="task_close")],
            ]
        ),
    )


async def task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 验证群成员身份后调用原子任务用例 / Verify group membership then call the atomic task use case.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    if query.data == "task_close":
        await query.delete_message()
        return
    task = _TASKS.get(query.data)
    if task is None:
        return
    task_id, group_id, reward, group_name = task
    member = await context.bot.get_chat_member(group_id, query.from_user.id)
    if member.status in {"left", "kicked"}:
        await query.answer(
            f"检测到您尚未加入 {group_name} 群组，请先加入再领取奖励。",
            show_alert=True,
        )
        return
    result = await _service(context).claim_task(
        TaskClaimCommand(
            query.from_user.id,
            task_id,
            reward,
            f"telegram:task:{update.update_id}:{query.from_user.id}:{task_id}",
        )
    )
    if result.code is EconomyCode.ALREADY_CLAIMED:
        await query.answer("您已完成该任务，不能重复领取奖励。", show_alert=True)
    elif result.code is EconomyCode.SUCCESS:
        await query.answer(
            f"恭喜您完成任务，获得 {result.reward} 个硬币奖励！", show_alert=True
        )
    else:
        await query.answer("发放奖励时出现错误，请稍后再试。", show_alert=True)


async def ref_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 展示或绑定推荐关系 / Display or bind a referral relationship.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    args = tuple(context.args or ())
    service = _service(context)
    if not args:
        summary = await service.referral_summary(user.id)
        bot_user = await context.bot.get_me()
        invite_link = f"https://t.me/{bot_user.username}?start={user.id}"
        text = f"🎁 *推广邀请系统*\n\n📊 您已成功邀请 *{summary.total}* 位用户\n"
        if summary.referrer_id is not None:
            text += (
                f"👤 您的邀请人：*{summary.referrer_name}* "
                f"(`{summary.referrer_id}`)\n\n"
            )
        text += (
            f"您的邀请码：`{user.id}`\n\n"
            f"🔗 您的专属邀请链接：\n`{invite_link}`\n\n"
            f"将此链接分享给好友，当他们点击链接并启动机器人时，您将获得 *{INVITATION_REWARD}* 金币奖励！\n\n"
            "如需手动绑定邀请人，请使用命令：`/ref <邀请码>`"
        )
        if summary.invited:
            text += "\n\n🙋‍♂️ *最近邀请的用户（最多显示10个）：*\n"
            for index, invited in enumerate(summary.invited, 1):
                text += (
                    f"{index}. {invited.name} (`{invited.user_id}`) - "
                    f"{invited.invited_at:%Y-%m-%d %H:%M:%S}\n"
                )
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return
    try:
        referrer_id = int(args[0])
    except ValueError:
        await message.reply_text("邀请码必须是数字！")
        return
    result = await service.bind_referral(
        ReferralCommand(
            user.id,
            referrer_id,
            user.full_name,
            INVITATION_REWARD,
            telegram_runtime_settings(context).new_user_bonus,
            f"telegram:ref:bind:{update.update_id}:{user.id}",
        )
    )
    await message.reply_text(
        _render_referral_result(
            result,
            new_user_bonus=telegram_runtime_settings(context).new_user_bonus,
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def ref_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 应答保留的 ``ref_`` namespace / Answer the retained ``ref_`` namespace.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    del context
    if update.callback_query is not None:
        await update.callback_query.answer()


async def webpassword_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """@brief 显示或设置 Web 密码 / Display or set a web password.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    user = update.effective_user
    message = update.effective_message
    if user is None or message is None:
        return
    if not user.username:
        await message.reply_text(
            "您需要设置Telegram用户名才能使用Web密码功能。\n"
            "请在Telegram设置中设置用户名后再尝试。\n\n"
            "You need to set a Telegram username to use the web password feature.\n"
            "Please set your username in Telegram settings and try again."
        )
        return
    service = _service(context)
    if not await service.account_exists(user.id):
        await message.reply_text(
            "请先使用 /me 命令注册账户。\nPlease register first using the /me command."
        )
        return
    args = tuple(context.args or ())
    username = html.escape(user.username)
    if not args:
        status = await service.web_password_status(user.id)
        details = (
            f"状态: <b>已设置</b>\n设置时间: {status.created_at:%Y-%m-%d %H:%M:%S}\n"
            f"更新时间: {status.updated_at:%Y-%m-%d %H:%M:%S}"
            if status.exists and status.created_at and status.updated_at
            else "状态: <b>未设置</b>"
        )
        await message.reply_text(
            f"🔐 <b>Web密码状态</b>\n\n用户: @{username}\n{details}\n\n"
            "使用方法: <code>/webpassword 新密码</code>\n"
            "密码要求: 6-20位，包含字母和数字",
            parse_mode=ParseMode.HTML,
        )
        return
    is_update, result_message = await service.set_web_password(user.id, " ".join(args))
    if result_message.startswith("Web"):
        action = "更新" if is_update else "设置"
        text = (
            f"✅ <b>Web密码{action}成功</b>\n\n用户: @{username}\n"
            f"操作: {action}Web密码\n时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
            "⚠️ 请妥善保管您的密码，不要泄露给他人！"
        )
    else:
        text = (
            "❌ <b>Web密码设置失败</b>\n\n"
            f"错误信息: {html.escape(result_message)}\n\n"
            "密码要求:\n• 长度: 6-20位\n• 字符: 仅限字母和数字\n"
            "• 必须包含至少一个字母和一个数字"
        )
    await message.reply_text(text, parse_mode=ParseMode.HTML)


def _service(context: ContextTypes.DEFAULT_TYPE) -> EconomyService:
    """@brief 获取已装配 economy service / Resolve the assembled economy service.

    @param context PTB callback context / PTB callback context.
    @return 经济服务 / Economy service.
    @raise RuntimeError 启动装配缺失 / Raised when startup assembly is missing.
    """

    candidate = context.application.bot_data.get(ECONOMY_SERVICE_DATA_KEY)
    if not isinstance(candidate, EconomyService):
        raise RuntimeError("Economy service is not configured")
    return candidate


async def _send_referral_success(
    update: Update,
    result: ReferralResult,
    *,
    new_user_bonus: int,
) -> None:
    """@brief 投递 start 推荐成功提示 / Deliver a successful start-referral prompt.

    @param update Telegram Update / Telegram Update.
    @param result 推荐结果 / Referral result.
    @param new_user_bonus 新用户初始金币 / Initial coins granted to a new user.
    @return None / None.
    """

    if update.effective_message is None:
        return
    total = INVITATION_REWARD + (new_user_bonus if result.new_user else 0)
    await update.effective_message.reply_text(
        f"🎁 您已通过邀请链接加入，获得了 *{total}* 金币！",
        parse_mode=ParseMode.MARKDOWN,
    )


def _render_referral_result(
    result: ReferralResult,
    *,
    new_user_bonus: int,
) -> str:
    """@brief 渲染推荐绑定结果 / Render a referral-binding result.

    @param result 推荐结果 / Referral result.
    @param new_user_bonus 新用户初始金币 / Initial coins granted to a new user.
    @return 用户可见文本 / User-visible text.
    """

    if result.code is EconomyCode.SELF_REFERRAL:
        return "您不能邀请自己哦！"
    if result.code is EconomyCode.REFERRER_NOT_FOUND:
        return "邀请绑定失败，邀请人不存在。请检查邀请码是否正确。"
    if result.code is EconomyCode.ALREADY_BOUND:
        return f"绑定失败，您已经被 *{result.referrer_name or '其他用户'}* 邀请过了。"
    total = INVITATION_REWARD + (new_user_bonus if result.new_user else 0)
    return f"邀请绑定成功！您获得了 *{total}* 金币！"


__all__ = [
    "ECONOMY_SERVICE_DATA_KEY",
    "checkin_command",
    "ref_callback",
    "ref_command",
    "start_command",
    "task_callback",
    "task_command",
    "webpassword_command",
]
