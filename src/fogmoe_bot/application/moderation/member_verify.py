"""@brief Telegram 群组成员验证用例 / Telegram group member-verification use cases."""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from fogmoe_bot.application.telegram.command_cooldown import cooldown
from fogmoe_bot.domain.moderation import ChatId, MessageId, UserId
from fogmoe_bot.domain.moderation.verification import (
    VerificationTask,
    hash_verification_token,
)
from fogmoe_bot.infrastructure.database.repositories import moderation_repository


logger = logging.getLogger(__name__)
"""@brief 模块日志器 / Module logger."""

VERIFICATION_TIMEOUT = timedelta(minutes=5)
"""@brief 新成员验证有效期 / New-member verification lifetime."""

_verification_locks: dict[tuple[int, int], asyncio.Lock] = {}
"""@brief 进程内任务竞争锁 / In-process verification race locks."""


def _task_name(chat_id: int, user_id: int) -> str:
    """@brief 构造 JobQueue 任务名 / Build a JobQueue task name.

    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param user_id Telegram 用户 ID / Telegram user ID.
    @return 稳定任务名 / Stable task name.
    """

    return f"member-verification:{chat_id}:{user_id}"


def _lock_for(chat_id: int, user_id: int) -> asyncio.Lock:
    """@brief 获取单用户验证锁 / Get a per-member verification lock.

    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param user_id Telegram 用户 ID / Telegram user ID.
    @return 异步锁 / Async lock.
    """

    return _verification_locks.setdefault((chat_id, user_id), asyncio.Lock())


def _cancel_scheduled_job(job_queue, chat_id: int, user_id: int) -> None:
    """@brief 取消指定成员的超时任务 / Cancel a member's timeout jobs.

    @param job_queue PTB JobQueue / PTB JobQueue.
    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param user_id Telegram 用户 ID / Telegram user ID.
    @return None / None.
    """

    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(_task_name(chat_id, user_id)):
        job.schedule_removal()


def _schedule_task(job_queue, task: VerificationTask, when) -> None:
    """@brief 调度类型化验证超时任务 / Schedule a typed verification timeout.

    @param job_queue PTB JobQueue / PTB JobQueue.
    @param task 验证任务 / Verification task.
    @param when PTB run_once 时间参数 / PTB run_once time argument.
    @return None / None.
    """

    if job_queue is None:
        raise RuntimeError("JobQueue is required for member verification")
    _cancel_scheduled_job(job_queue, int(task.chat_id), int(task.user_id))
    job_queue.run_once(
        verification_timeout,
        when,
        data=task,
        name=_task_name(int(task.chat_id), int(task.user_id)),
        chat_id=int(task.chat_id),
        user_id=int(task.user_id),
    )


async def check_bot_permissions(bot, chat_id: int) -> tuple[bool, str]:
    """@brief 检查成员限制权限 / Check member-restriction permissions.

    @param bot Telegram Bot / Telegram Bot.
    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @return 是否满足及说明 / Success flag and explanation.
    """

    bot_member = await bot.get_chat_member(chat_id, bot.id)
    if bot_member.status not in {"administrator", "creator"}:
        return False, "机器人需要管理员权限"
    if not getattr(bot_member, "can_restrict_members", False):
        return False, "机器人缺少以下权限: 限制成员"
    return True, "权限检查通过"


async def _restore_member_permissions(bot, chat_id: int, user_id: int) -> None:
    """@brief 按群默认权限解除成员限制 / Restore a member to the chat defaults.

    @param bot Telegram Bot / Telegram Bot.
    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param user_id Telegram 用户 ID / Telegram user ID.
    @return None / None.
    """

    chat = await bot.get_chat(chat_id)
    permissions = chat.permissions or ChatPermissions.all_permissions()
    await bot.restrict_chat_member(chat_id, user_id, permissions)


@cooldown
async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 开启或关闭新成员验证 / Toggle new-member verification."""

    if update.effective_chat is None or update.effective_user is None or update.message is None:
        return
    if update.effective_chat.type not in {"group", "supergroup"}:
        await update.message.reply_text("此命令只能在群组中使用。")
        return

    chat_id = update.effective_chat.id
    sender = await context.bot.get_chat_member(chat_id, update.effective_user.id)
    if sender.status not in {"administrator", "creator"}:
        await update.message.reply_text("只有群组管理员才能使用该命令。")
        return

    if await moderation_repository.verification_group_exists(chat_id):
        await moderation_repository.disable_group_verification(chat_id)
        await update.message.reply_text("验证接管已取消。")
        return

    has_permissions, reason = await check_bot_permissions(context.bot, chat_id)
    if not has_permissions:
        await update.message.reply_text(
            f"机器人缺少必要权限，无法开启验证功能：{reason}"
        )
        return

    group_name = update.effective_chat.title or "未知群组"
    await moderation_repository.enable_group_verification(chat_id, group_name)
    await update.message.reply_text(
        "新成员验证功能已开启。新成员加入时将被禁言并要求点击【验证】按钮验证，5分钟内有效。"
    )


async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 为新成员创建持久化验证任务 / Create persisted verification tasks for new members."""

    if update.effective_chat is None or update.message is None:
        return
    chat_id = update.effective_chat.id
    if not await moderation_repository.verification_group_exists(chat_id):
        return

    for new_member in update.message.new_chat_members:
        if new_member.is_bot:
            logger.info("跳过机器人验证: user=%s", new_member.id)
            continue
        user_id = new_member.id
        try:
            await context.bot.restrict_chat_member(
                chat_id,
                user_id,
                ChatPermissions.no_permissions(),
            )
        except Exception as exc:
            logger.warning("限制新成员失败: chat=%s user=%s error=%s", chat_id, user_id, exc)
            await context.bot.send_message(
                chat_id,
                f"验证错误：无法限制成员 {new_member.full_name}({user_id})。",
            )
            continue

        token = secrets.token_hex(8)
        welcome_message = await update.message.reply_text(
            f"欢迎 {new_member.mention_html()} 加入群组！请点击【验证】按钮进行验证（5分钟内有效）。",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("点击验证", callback_data=f"verify_{user_id}_{token}")]]
            ),
            parse_mode="HTML",
        )
        task = VerificationTask(
            chat_id=ChatId(chat_id),
            user_id=UserId(user_id),
            message_id=MessageId(welcome_message.message_id),
            token_hash=hash_verification_token(token),
            expires_at=datetime.now() + VERIFICATION_TIMEOUT,
        )
        try:
            await moderation_repository.upsert_verification_task(
                user_id,
                chat_id,
                welcome_message.message_id,
                task.expires_at,
                task.token_hash,
            )
            _schedule_task(context.job_queue, task, VERIFICATION_TIMEOUT)
        except Exception as exc:
            logger.error(
                "创建成员验证任务失败并回滚限制: chat=%s user=%s error=%s",
                chat_id,
                user_id,
                exc,
            )
            await moderation_repository.delete_verification_task(user_id, chat_id)
            try:
                await _restore_member_permissions(context.bot, chat_id, user_id)
                await welcome_message.edit_text("验证服务暂时不可用，已解除成员限制。")
            except Exception as rollback_exc:
                logger.error(
                    "回滚成员验证限制失败: chat=%s user=%s error=%s",
                    chat_id,
                    user_id,
                    rollback_exc,
                )


async def verification_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 处理 JobQueue 成员验证超时 / Handle a JobQueue verification timeout."""

    if context.job is None or not isinstance(context.job.data, VerificationTask):
        logger.error("验证超时任务缺少类型化 data")
        return
    scheduled = context.job.data
    chat_id = int(scheduled.chat_id)
    user_id = int(scheduled.user_id)
    async with _lock_for(chat_id, user_id):
        current = await moderation_repository.fetch_verification_task(user_id, chat_id)
        if (
            current is None
            or current.message_id != scheduled.message_id
            or current.token_hash != scheduled.token_hash
        ):
            return
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id)
        except Exception as exc:
            logger.warning("移出验证超时成员失败: chat=%s user=%s error=%s", chat_id, user_id, exc)
            return

        await moderation_repository.delete_verification_task(user_id, chat_id)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(current.message_id),
                text="验证超时，您已被移出群组。",
            )
        except Exception as exc:
            logger.warning("更新验证超时消息失败: message=%s error=%s", current.message_id, exc)


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 校验 token 并解除成员限制 / Validate a token and unrestrict the member."""

    query = update.callback_query
    if query is None or update.effective_chat is None:
        return
    callback_parts = (query.data or "").split("_", maxsplit=2)
    if (
        len(callback_parts) != 3
        or callback_parts[0] != "verify"
        or callback_parts[1] != str(query.from_user.id)
    ):
        await query.answer("这不是为您准备的验证按钮。", show_alert=True)
        return

    chat_id = update.effective_chat.id
    user_id = query.from_user.id
    token = callback_parts[2]
    async with _lock_for(chat_id, user_id):
        task = await moderation_repository.fetch_verification_task(user_id, chat_id)
        if task is None or not task.accepts(token, datetime.now()):
            await query.answer("验证已失效或 token 不正确。", show_alert=True)
            return

        try:
            await _restore_member_permissions(context.bot, chat_id, user_id)
        except Exception as exc:
            logger.warning("解除成员限制失败: chat=%s user=%s error=%s", chat_id, user_id, exc)
            await query.answer("验证时出现错误，请稍后再试。", show_alert=True)
            return

        await moderation_repository.delete_verification_task(user_id, chat_id)
        _cancel_scheduled_job(context.job_queue, chat_id, user_id)
        await query.edit_message_text("验证通过，欢迎加入群组！")
        await query.answer("验证成功！", show_alert=True)


async def restore_verification_tasks(application) -> None:
    """@brief 启动时恢复持久化验证任务 / Restore persisted verification tasks at startup."""

    now = datetime.now()
    tasks = await moderation_repository.fetch_pending_verification_tasks()
    for task in tasks:
        remaining = max((task.expires_at - now).total_seconds(), 0.0)
        _schedule_task(application.job_queue, task, remaining)
    if tasks:
        logger.info("已恢复 %s 个成员验证任务", len(tasks))


async def handle_member_left(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 清理离群成员或机器人自身的验证状态 / Clean verification state after a member leaves."""

    if update.effective_chat is None or update.message is None:
        return
    user = update.message.left_chat_member
    if user is None:
        return
    chat_id = update.effective_chat.id
    bot = await context.bot.get_me()
    if user.id == bot.id:
        await moderation_repository.disable_group_verification(chat_id)
        return

    async with _lock_for(chat_id, user.id):
        task = await moderation_repository.fetch_verification_task(user.id, chat_id)
        if task is None:
            return
        _cancel_scheduled_job(context.job_queue, chat_id, user.id)
        await moderation_repository.delete_verification_task(user.id, chat_id)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(task.message_id),
                text=f"用户 {user.full_name} 在验证前离开了群组。",
            )
        except Exception as exc:
            logger.warning("更新离群验证消息失败: message=%s error=%s", task.message_id, exc)


def setup_member_verification(dispatcher) -> None:
    """@brief 注册成员验证 handlers / Register member-verification handlers."""

    dispatcher.add_handler(CommandHandler("verify", verify_command))
    dispatcher.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler)
    )
    dispatcher.add_handler(CallbackQueryHandler(verify_callback, pattern=r"^verify_"))
    dispatcher.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_member_left)
    )
