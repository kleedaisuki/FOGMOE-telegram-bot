"""@brief 成员验证 Telegram 薄适配器 / Thin Telegram adapter for member verification."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import (
    Bot,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from fogmoe_bot.application.moderation.verification_service import (
    VERIFICATION_SERVICE_DATA_KEY,
    VerificationRejected,
    VerificationService,
)
from fogmoe_bot.domain.moderation.models import ChatId, MessageId, UserId
from fogmoe_bot.domain.moderation.verification import (
    VerificationKey,
    VerificationStatus,
    VerificationTask,
    VerificationVersion,
)

from .idempotency import telegram_update_idempotency_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VerificationCallback:
    """@brief 绑定成员、版本与 token 的 callback DTO / Callback DTO bound to member, version, and token.

    @param user_id 目标成员 / Target member.
    @param version 聚合版本 / Aggregate version.
    @param token 明文 token / Plain token.
    """

    user_id: UserId
    """@brief 目标成员 / Target member."""

    version: VerificationVersion
    """@brief 聚合版本 / Aggregate version."""

    token: str
    """@brief 明文 token / Plain token."""

    def encode(self) -> str:
        """@brief 编码为 Telegram callback_data / Encode as Telegram callback_data.

        @return 不超过 64 字节的 callback / Callback within 64 bytes.
        """

        value = f"verify:{int(self.user_id)}:{self.version.value}:{self.token}"
        if len(value.encode("utf-8")) > 64:
            raise ValueError("verification callback exceeds 64 bytes")
        return value

    @classmethod
    def decode(cls, value: str) -> VerificationCallback:
        """@brief 严格解析 callback_data / Strictly parse callback_data.

        @param value callback_data / callback_data.
        @return 类型化 DTO / Typed DTO.
        """

        parts = value.split(":")
        if len(parts) != 4 or parts[0] != "verify":
            raise ValueError("invalid verification callback")
        _prefix, raw_user, raw_version, token = parts
        if not token or len(token) > 32:
            raise ValueError("invalid verification token")
        return cls(
            user_id=UserId(int(raw_user)),
            version=VerificationVersion(int(raw_version)),
            token=token,
        )


class TelegramVerificationDelivery:
    """@brief 可重放但非 exactly-once 的 Telegram 验证副作用 / Replayable but not exactly-once Telegram verification effects.

    @param bot Telegram Bot / Telegram Bot.
    """

    def __init__(self, bot: Bot) -> None:
        """@brief 创建投递适配器 / Create the delivery adapter.

        @param bot Telegram Bot / Telegram Bot.
        @return None / None.
        """

        self._bot = bot

    async def deliver(self, task: VerificationTask) -> None:
        """@brief 按过渡态执行 Telegram 副作用 / Deliver Telegram effects for a transitional state.

        @param task 过渡态聚合 / Transitional aggregate.
        @return None / None.
        @note Telegram 成功、DB ack 前崩溃会重放；权限操作设计为可安全重复。/
            A crash after Telegram success but before DB ack replays the operation; permission effects are safely repeatable.
        """

        chat_id = int(task.chat_id)
        user_id = int(task.user_id)
        if task.status is VerificationStatus.PASSING:
            await _restore_member_permissions(self._bot, chat_id, user_id)
            await self._edit(task, "验证通过，欢迎加入群组！")
            return
        if task.status is VerificationStatus.EXPIRING:
            await self._bot.ban_chat_member(chat_id, user_id)
            await self._bot.unban_chat_member(chat_id, user_id)
            await self._edit(task, "验证超时，您已被移出群组。")
            return
        if task.status is VerificationStatus.CANCELLING:
            if task.message_id is None:
                await _restore_member_permissions(self._bot, chat_id, user_id)
            else:
                await self._edit(task, f"用户 {task.member_name} 在验证前离开了群组。")
            return
        raise ValueError(f"unsupported verification delivery state: {task.status}")

    async def _edit(self, task: VerificationTask, text: str) -> None:
        """@brief 幂等编辑验证消息 / Idempotently edit a verification message.

        @param task 验证聚合 / Verification aggregate.
        @param text 终态文本 / Terminal text.
        @return None / None.
        """

        if task.message_id is None:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=int(task.chat_id),
                message_id=int(task.message_id),
                text=text,
            )
        except BadRequest as error:
            logger.info(
                "Verification message cannot be edited and will be treated as terminal: "
                "chat=%s user=%s message=%s error=%s",
                task.chat_id,
                task.user_id,
                task.message_id,
                error,
            )


async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 开启或关闭新成员验证 / Toggle new-member verification.

    @param update Telegram Update / Telegram Update.
    @param context PTB context / PTB context.
    @return None / None.
    """

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat is None or user is None or message is None:
        return
    if chat.type not in {"group", "supergroup"}:
        await message.reply_text("此命令只能在群组中使用。")
        return
    sender = await context.bot.get_chat_member(chat.id, user.id)
    if sender.status not in {"administrator", "creator"}:
        await message.reply_text("只有群组管理员才能使用该命令。")
        return
    service = _service(context)
    chat_id = ChatId(chat.id)
    if not await service.group_enabled(chat_id):
        permitted, reason = await check_bot_permissions(context.bot, chat.id)
        if not permitted:
            await message.reply_text(f"机器人缺少必要权限，无法开启验证功能：{reason}")
            return
    toggle = await service.toggle_group(
        chat_id,
        group_name=chat.title or "未知群组",
        actor_id=UserId(user.id),
        idempotency_key=telegram_update_idempotency_key(
            update,
            "moderation.verification-toggle",
        ),
    )
    if not toggle.enabled:
        await message.reply_text("验证接管已取消。")
        return
    await message.reply_text(
        "新成员验证功能已开启。新成员加入时将被禁言并要求点击【验证】按钮验证，5分钟内有效。"
    )


async def new_member_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """@brief 为新成员启动 crash-recoverable 创建流程 / Start a crash-recoverable creation workflow for new members.

    @param update Telegram Update / Telegram Update.
    @param context PTB context / PTB context.
    @return None / None.
    """

    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return
    service = _service(context)
    if not await service.group_enabled(ChatId(chat.id)):
        return
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        invitation = await service.begin(
            VerificationKey(ChatId(chat.id), UserId(member.id)),
            member_name=member.full_name,
        )
        welcome = None
        try:
            await context.bot.restrict_chat_member(
                chat.id,
                member.id,
                ChatPermissions.no_permissions(),
            )
            callback = VerificationCallback(
                UserId(member.id),
                invitation.task.version.next(),
                invitation.token,
            ).encode()
            welcome = await message.reply_text(
                f"欢迎 {member.mention_html()} 加入群组！请点击【验证】按钮进行验证（5分钟内有效）。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("点击验证", callback_data=callback)]]
                ),
                parse_mode="HTML",
            )
            await service.activate(invitation, MessageId(welcome.message_id))
        except Exception:
            logger.exception(
                "Failed to establish verification workflow chat=%s user=%s",
                chat.id,
                member.id,
            )
            await service.abort_creation(invitation)
            if welcome is not None:
                try:
                    await welcome.edit_text("验证服务暂时不可用，已解除成员限制。")
                except Exception:
                    logger.warning(
                        "Failed to edit aborted verification welcome", exc_info=True
                    )
            else:
                await context.bot.send_message(
                    chat.id,
                    f"验证错误：无法限制成员 {member.full_name}({member.id})。",
                )


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 将版本化 callback 映射为 PASS_REQUESTED / Map a versioned callback to PASS_REQUESTED.

    @param update Telegram Update / Telegram Update.
    @param context PTB context / PTB context.
    @return None / None.
    """

    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return
    try:
        callback = VerificationCallback.decode(query.data or "")
    except TypeError, ValueError:
        await query.answer("验证已失效或 token 不正确。", show_alert=True)
        return
    if int(callback.user_id) != query.from_user.id:
        await query.answer("这不是为您准备的验证按钮。", show_alert=True)
        return
    result = await _service(context).request_pass(
        VerificationKey(ChatId(chat.id), callback.user_id),
        expected_version=callback.version,
        token=callback.token,
    )
    if isinstance(result, VerificationRejected):
        await query.answer(_rejection_text(result), show_alert=True)
        return
    if result.effect_completed:
        await query.answer("验证成功！", show_alert=True)
    else:
        await query.answer("验证时出现错误，请稍后再试。", show_alert=True)


async def handle_member_left(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """@brief 将成员离群映射为版本化取消 / Map member-left to versioned cancellation.

    @param update Telegram Update / Telegram Update.
    @param context PTB context / PTB context.
    @return None / None.
    """

    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None or message.left_chat_member is None:
        return
    member = message.left_chat_member
    service = _service(context)
    if member.id == context.bot.id:
        await service.disable_group(ChatId(chat.id))
        return
    await service.member_left(VerificationKey(ChatId(chat.id), UserId(member.id)))


async def check_bot_permissions(bot: Bot, chat_id: int) -> tuple[bool, str]:
    """@brief 检查限制成员权限 / Check member-restriction permissions.

    @param bot Telegram Bot / Telegram Bot.
    @param chat_id 群组 ID / Chat ID.
    @return 是否满足及说明 / Success flag and explanation.
    """

    member = await bot.get_chat_member(chat_id, bot.id)
    if member.status not in {"administrator", "creator"}:
        return False, "机器人需要管理员权限"
    if not getattr(member, "can_restrict_members", False):
        return False, "机器人缺少以下权限: 限制成员"
    return True, "权限检查通过"


async def _restore_member_permissions(bot: Bot, chat_id: int, user_id: int) -> None:
    """@brief 按群默认权限解除限制 / Restore a member to chat-default permissions.

    @param bot Telegram Bot / Telegram Bot.
    @param chat_id 群组 ID / Chat ID.
    @param user_id 用户 ID / User ID.
    @return None / None.
    """

    chat = await bot.get_chat(chat_id)
    permissions = chat.permissions or ChatPermissions.all_permissions()
    await bot.restrict_chat_member(chat_id, user_id, permissions)


def _service(context: ContextTypes.DEFAULT_TYPE) -> VerificationService:
    """@brief 从组合根取得验证服务 / Get verification service from the composition root.

    @param context PTB context / PTB context.
    @return 验证服务 / Verification service.
    """

    service = context.application.bot_data.get(VERIFICATION_SERVICE_DATA_KEY)
    if not isinstance(service, VerificationService):
        raise RuntimeError("verification service is not configured")
    return service


def _rejection_text(rejection: VerificationRejected) -> str:
    """@brief 渲染 callback 拒绝 / Render callback rejection.

    @param rejection 类型化拒绝 / Typed rejection.
    @return 旧产品语义兼容文案 / Product-compatible text.
    """

    del rejection
    return "验证已失效或 token 不正确。"
