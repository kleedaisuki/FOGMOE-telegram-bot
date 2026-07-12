"""@brief Telegram 治理映射、效果与举报适配器 / Telegram moderation mapping, effect, and reporting adapters."""

from __future__ import annotations

import html
import logging

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, TelegramError

from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.domain.moderation.effects import KeywordReplyPlan, SpamEnforcementPlan
from fogmoe_bot.domain.moderation.models import (
    ActorRole,
    ChatId,
    ContentKind,
    MessageId,
    ModerationRequest,
    RuleKind,
    UserId,
)
from fogmoe_bot.domain.moderation.reporting import (
    ReportDeliveryResult,
    ReportRequest,
)


logger = logging.getLogger(__name__)


class TelegramModerationMapper:
    """@brief 将 durable payload 映射为治理输入 / Map durable payloads to moderation inputs.

    @param bot 用于重建 Update 与读取成员角色 / Bot used to reconstruct Updates and read member roles.
    """

    def __init__(self, bot: Bot) -> None:
        """@brief 注入 Bot / Inject the Bot.

        @param bot Telegram Bot / Telegram Bot.
        @return None / None.
        """

        self._bot = bot

    async def moderation_request(
        self,
        update: InboundUpdate,
    ) -> ModerationRequest | None:
        """@brief 映射群文本或 caption 并解析角色 / Map group text/caption and resolve role.

        @param update durable Update / Durable Update.
        @return 审核请求或 None / Moderation request or None.
        """

        telegram_update = self._reconstruct(update)
        request = _request_from_telegram(telegram_update, include_commands=True)
        if request is None:
            return None
        user = telegram_update.effective_user
        if user is not None and user.is_bot:
            role = ActorRole.BOT
        else:
            try:
                member = await self._bot.get_chat_member(
                    int(request.chat_id),
                    int(request.user_id),
                )
            except TelegramError as error:
                logger.warning(
                    "治理角色解析失败，按普通成员处理: chat=%s user=%s error=%s",
                    int(request.chat_id),
                    int(request.user_id),
                    error,
                )
                role = ActorRole.MEMBER
            else:
                if member.status is ChatMemberStatus.OWNER:
                    role = ActorRole.OWNER
                elif member.status is ChatMemberStatus.ADMINISTRATOR:
                    role = ActorRole.ADMINISTRATOR
                else:
                    role = ActorRole.MEMBER
        return ModerationRequest(
            chat_id=request.chat_id,
            user_id=request.user_id,
            message_id=request.message_id,
            content=request.content,
            content_kind=request.content_kind,
            actor_role=role,
            is_edited=request.is_edited,
        )

    def keyword_request(self, update: InboundUpdate) -> ModerationRequest | None:
        """@brief 映射非命令群文本 / Map non-command group text.

        @param update durable Update / Durable Update.
        @return 关键词观察请求或 None / Keyword-observer request or None.
        """

        return _request_from_telegram(
            self._reconstruct(update),
            include_commands=False,
        )

    def _reconstruct(self, update: InboundUpdate) -> Update:
        """@brief 从已校验 JSON 重建 SDK Update / Reconstruct an SDK Update from validated JSON.

        @param update durable Update / Durable Update.
        @return PTB Update / PTB Update.
        """

        return Update.de_json(dict(update.payload), self._bot)


class TelegramModerationEffectSink:
    """@brief Telegram 删除、警告与自动回复适配器 / Telegram deletion, warning, and auto-reply adapter.

    @param bot Telegram Bot / Telegram Bot.
    """

    def __init__(self, bot: Bot) -> None:
        """@brief 注入 Bot / Inject the Bot.

        @param bot Telegram Bot / Telegram Bot.
        @return None / None.
        """

        self._bot = bot

    async def delete_spam(self, plan: SpamEnforcementPlan) -> None:
        """@brief 删除命中消息 / Delete a matched message.

        @param plan 垃圾处置意图 / Spam-enforcement intent.
        @return None / None.
        @note “消息不存在”视为幂等成功，以覆盖远端删除成功、本地阶段提交前崩溃的窗口。/
        “Message not found” is treated as idempotent success, covering a crash after remote
        deletion but before the local stage commit.
        """

        try:
            await self._bot.delete_message(
                chat_id=int(plan.chat_id),
                message_id=int(plan.message_id),
            )
        except BadRequest as error:
            if "message to delete not found" not in str(error).casefold():
                raise

    async def send_spam_warning(
        self,
        plan: SpamEnforcementPlan,
        *,
        warning_count: int,
    ) -> None:
        """@brief 发送安全转义的警告 / Send an escaped warning.

        @param plan 垃圾处置意图 / Spam-enforcement intent.
        @param warning_count 当前窗口警告序号 / Current-window warning ordinal.
        @return None / None.
        """

        if plan.rule_kind is RuleKind.LINK:
            category = "链接"
            policy_text = "本群组禁止发送链接。"
        elif plan.rule_kind is RuleKind.MENTION:
            category = "@提及"
            policy_text = "本群组禁止@提及用户。"
        else:
            category = "垃圾内容"
            policy_text = "持续发送垃圾信息可能导致被禁言或移出群组。"
        mention = f'<a href="tg://user?id={int(plan.user_id)}">用户</a>'
        await self._bot.send_message(
            chat_id=int(plan.chat_id),
            text=(
                f"⚠️ 注意: {mention} 发送的消息包含{category} "
                f"<tg-spoiler>{html.escape(plan.matched_text)}</tg-spoiler>，已被自动删除。\n"
                f"{policy_text}这是第 {warning_count} 次警告。"
            ),
            parse_mode=ParseMode.HTML,
        )

    async def send_keyword_reply(self, plan: KeywordReplyPlan) -> None:
        """@brief 发送关键词回复，HTML schema 错误时退回纯文本 / Send a keyword reply with plain-text fallback for invalid HTML.

        @param plan 关键词回复意图 / Keyword-reply intent.
        @return None / None.
        """

        try:
            await self._bot.send_message(
                chat_id=int(plan.chat_id),
                text=plan.response,
                parse_mode=ParseMode.HTML,
                reply_parameters=ReplyParameters(message_id=int(plan.message_id)),
            )
        except BadRequest:
            await self._bot.send_message(
                chat_id=int(plan.chat_id),
                text=f"【回复内容HTML格式错误】\n\n{plan.response}",
                reply_parameters=ReplyParameters(message_id=int(plan.message_id)),
            )


class TelegramReportDelivery:
    """@brief 将举报私聊投递给非 Bot 管理员 / Deliver reports privately to non-bot administrators.

    @param bot Telegram Bot / Telegram Bot.
    """

    def __init__(self, bot: Bot) -> None:
        """@brief 注入 Bot / Inject the Bot.

        @param bot Telegram Bot / Telegram Bot.
        @return None / None.
        """

        self._bot = bot

    async def deliver(self, request: ReportRequest) -> ReportDeliveryResult:
        """@brief 通知所有可联系管理员 / Notify all contactable administrators.

        @param request 举报请求 / Report request.
        @return 投递统计 / Delivery statistics.
        """

        administrators = tuple(
            member
            for member in await self._bot.get_chat_administrators(
                int(request.key.chat_id)
            )
            if not member.user.is_bot
        )
        link = _message_link(
            int(request.key.chat_id),
            int(request.key.message_id),
        )
        keyboard = (
            InlineKeyboardMarkup([[InlineKeyboardButton("查看被举报消息", url=link)]])
            if link is not None
            else None
        )
        text = _render_report(request)
        delivered = 0
        for administrator in administrators:
            try:
                await self._bot.send_message(
                    chat_id=administrator.user.id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except TelegramError as error:
                logger.warning(
                    "举报通知投递失败: admin=%s error=%s",
                    administrator.user.id,
                    error,
                )
            else:
                delivered += 1
        return ReportDeliveryResult(len(administrators), delivered)


def _request_from_telegram(
    update: Update,
    *,
    include_commands: bool,
) -> ModerationRequest | None:
    """@brief 从 PTB Update 提取群消息 / Extract a group message from a PTB Update.

    @param update PTB Update / PTB Update.
    @param include_commands 是否包含命令 / Whether commands are included.
    @return 类型化请求或 None / Typed request or None.
    """

    message = update.message or update.edited_message
    chat = update.effective_chat
    user = update.effective_user
    if (
        message is None
        or chat is None
        or user is None
        or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}
    ):
        return None
    content = message.text or message.caption or ""
    if not content or (not include_commands and content.startswith("/")):
        return None
    if message.caption is not None and message.text is None:
        kind = ContentKind.CAPTION
    elif content.startswith("/"):
        kind = ContentKind.COMMAND
    else:
        kind = ContentKind.TEXT
    return ModerationRequest(
        chat_id=ChatId(chat.id),
        user_id=UserId(user.id),
        message_id=MessageId(message.message_id),
        content=content,
        content_kind=kind,
        actor_role=ActorRole.MEMBER,
        is_edited=update.edited_message is message,
    )


def _message_link(chat_id: int, message_id: int) -> str | None:
    """@brief 构造超级群消息链接 / Build a supergroup message link.

    @param chat_id 群组 ID / Group identifier.
    @param message_id 消息 ID / Message identifier.
    @return 链接或 None / Link or None.
    """

    encoded = str(chat_id)
    return (
        f"https://t.me/c/{encoded[4:]}/{message_id}"
        if encoded.startswith("-100")
        else None
    )


def _render_report(request: ReportRequest) -> str:
    """@brief 渲染安全举报 HTML / Render safe report HTML.

    @param request 举报请求 / Report request.
    @return Telegram HTML / Telegram HTML.
    """

    text = request.reported_text[:300]
    suffix = "…" if len(request.reported_text) > 300 else ""
    return (
        "<b>== 举报信息 ==</b>\n\n"
        f"<b>群组:</b> {html.escape(request.chat_title or '未知群组')}\n"
        f"<b>群组 ID:</b> <code>{int(request.key.chat_id)}</code>\n\n"
        f"<b>被举报用户:</b> {html.escape(request.reported_user_name)}\n"
        f"<b>用户 ID:</b> <code>{int(request.reported_user_id)}</code>\n\n"
        f"<b>被举报消息:</b>\n{html.escape(text)}{suffix}\n\n"
        f"<b>举报人:</b> {html.escape(request.reporter_name)}\n"
        f"<b>举报人 ID:</b> <code>{int(request.key.reporter_id)}</code>"
    )


__all__ = [
    "TelegramModerationEffectSink",
    "TelegramModerationMapper",
    "TelegramReportDelivery",
]
