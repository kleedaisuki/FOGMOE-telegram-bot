"""@brief Telegram 治理命令的薄解析与渲染 / Thin parsing and rendering for Telegram moderation commands."""

from __future__ import annotations

import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import ContextTypes

from fogmoe_bot.domain.moderation.aggregate import (
    GroupModeration,
    ModerationLimitExceeded,
)
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    MessageId,
    RuleKind,
    UserId,
)
from fogmoe_bot.domain.moderation.reporting import (
    ReportKey,
    ReportRegistration,
    ReportRequest,
)

from .idempotency import telegram_update_idempotency_key
from .moderation_composition import (
    MODERATION_CAPABILITY_DATA_KEY,
    TelegramModerationCapability,
)


logger = logging.getLogger(__name__)

SPAM_HELP_CALLBACK_DATA = "spam_help"
"""@brief 历史 spam 帮助 callback namespace / Historical spam-help callback namespace."""

SPAM_CONTROL_HELP_TEXT = (
    "🛡️ <b>垃圾信息过滤功能使用说明</b> 🛡️\n\n"
    "<b>基本命令：</b>\n"
    "/spam - 开启/关闭垃圾信息过滤\n"
    "/spam help - 显示此帮助信息\n\n"
    "<b>过滤设置：</b>\n"
    "/spam links on - 开启链接过滤\n"
    "/spam links off - 关闭链接过滤\n"
    "/spam mentions on - 开启@提及过滤\n"
    "/spam mentions off - 关闭@提及过滤\n\n"
    "<b>自定义垃圾词：</b>\n"
    "/spam list - 列出自定义垃圾词\n"
    "/spam add &lt;关键词&gt; - 添加自定义垃圾词\n"
    "/spam add //&lt;正则表达式&gt; - 添加正则表达式匹配\n"
    "/spam del &lt;关键词&gt; - 删除自定义垃圾词\n\n"
    "<b>注意事项：</b>\n"
    "• 启用链接过滤后，所有包含链接的消息将被自动删除\n"
    "• 启用@提及过滤后，所有包含@用户名的消息将被自动删除\n"
    "• 每个群组最多可设置10个自定义垃圾词\n"
    "• 有自定义垃圾词时，全局垃圾词库将不生效\n"
    "• 管理员发送的消息不会被检测\n"
    "• 使用前请确保机器人有删除消息的权限"
)
"""@brief spam HTML 帮助文本 / Spam HTML help text."""

SPAM_CONTROL_HELP_TEXT_PLAIN = (
    "垃圾信息过滤功能命令说明：\n\n"
    "/spam - 开启/关闭垃圾信息过滤\n"
    "/spam help - 显示此帮助信息\n"
    "/spam links on/off - 开启/关闭链接过滤\n"
    "/spam mentions on/off - 开启/关闭@提及过滤\n"
    "/spam list - 列出自定义垃圾词\n"
    "/spam add <词> - 添加垃圾词\n"
    "/spam del <词> - 删除垃圾词"
)
"""@brief spam 纯文本帮助 / Spam plain-text help."""

_ALLOWED_HTML_TAGS = frozenset(
    {
        "a",
        "b",
        "code",
        "del",
        "em",
        "i",
        "ins",
        "pre",
        "s",
        "span",
        "strike",
        "strong",
        "tg-spoiler",
        "u",
    }
)
"""@brief 关键词回复允许的 Telegram HTML tag / Telegram HTML tags allowed in keyword replies."""

_HTML_TAG = re.compile(r"</?([a-zA-Z0-9-]+)(?:\s[^>]*)?>")
"""@brief HTML tag 识别表达式 / HTML-tag recognition expression."""


async def toggle_spam_control(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 处理 ``/spam`` 配置命令 / Handle the ``/spam`` configuration command.

    @param update Telegram Update / Telegram Update.
    @param context PTB context / PTB context.
    @return None / None.
    """

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    capability = _capability(context)
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.reply_text("此命令只能在群组中使用。")
        return
    if not await _is_administrator(context, chat.id, user.id):
        await message.reply_text("只有群组管理员才能使用此命令。")
        return

    args = tuple(context.args or ())
    subcommand = args[0].casefold() if args else None
    try:
        if subcommand == "help":
            await _show_spam_help(message)
            return
        if subcommand == "list":
            await _list_spam_rules(
                message,
                await capability.commands.read(ChatId(chat.id)),
            )
            return
        if subcommand == "add":
            if len(args) < 2:
                await message.reply_text(
                    "添加自定义垃圾词的正确格式是：\n"
                    "/spam add <垃圾词>\n\n"
                    "添加正则表达式格式：\n/spam add //正则表达式"
                )
                return
            pattern = " ".join(args[1:])
            regex = pattern.startswith("//")
            if regex:
                pattern = pattern[2:].strip()
            group = await capability.commands.put_spam_rule(
                ChatId(chat.id),
                UserId(user.id),
                pattern,
                regex=regex,
            )
            del group
            await message.reply_text(f"已添加或更新自定义垃圾词: '{pattern}'")
            return
        if subcommand == "del":
            if len(args) < 2:
                await message.reply_text(
                    "删除自定义垃圾词的正确格式是：\n/spam del <垃圾词>"
                )
                return
            pattern = " ".join(args[1:])
            if pattern.startswith("//"):
                pattern = pattern[2:].strip()
            removed = await capability.commands.remove_spam_rule(
                ChatId(chat.id),
                UserId(user.id),
                pattern,
            )
            await message.reply_text(
                f"已删除自定义垃圾词: '{pattern}'"
                if removed
                else f"未找到自定义垃圾词: '{pattern}'"
            )
            return
        if subcommand in {"links", "mentions"}:
            if len(args) < 2 or args[1].casefold() not in {"on", "off"}:
                label = "链接" if subcommand == "links" else "@提及"
                await message.reply_text(
                    f"设置{label}过滤的正确格式是：\n"
                    f"/spam {subcommand} on\n/spam {subcommand} off"
                )
                return
            if not await _bot_can_delete(context, chat.id):
                await message.reply_text(
                    "机器人需要有删除消息的权限才能使用此过滤功能。"
                )
                return
            enabled = args[1].casefold() == "on"
            if subcommand == "links":
                await capability.commands.set_link_blocking(
                    ChatId(chat.id),
                    UserId(user.id),
                    enabled=enabled,
                )
                label = "链接"
            else:
                await capability.commands.set_mention_blocking(
                    ChatId(chat.id),
                    UserId(user.id),
                    enabled=enabled,
                )
                label = "@mention"
            await message.reply_text(
                f"{label}过滤功能已{'开启' if enabled else '关闭'}。"
            )
            return

        if not await _bot_can_delete(context, chat.id):
            await message.reply_text(
                "机器人需要有删除消息的权限才能启用垃圾信息过滤功能。"
                "请先授予机器人管理员权限并允许其删除消息。"
            )
            return
        toggle = await capability.commands.toggle(
            ChatId(chat.id),
            UserId(user.id),
            idempotency_key=telegram_update_idempotency_key(
                update,
                "moderation.spam-toggle",
            ),
        )
        if toggle.enabled:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "查看更多功能 (管理员)",
                            callback_data=SPAM_HELP_CALLBACK_DATA,
                        )
                    ]
                ]
            )
            await message.reply_text(
                "垃圾信息过滤功能已 ***开启***。我将自动删除可能的垃圾消息并发出警告。",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
        else:
            await message.reply_text(
                "垃圾信息过滤功能已 ***关闭***。",
                parse_mode=ParseMode.MARKDOWN,
            )
    except ModerationLimitExceeded:
        await message.reply_text("每个群组最多只能设置10条，请先删除一些再添加。")
    except ValueError as error:
        if str(error) == "Spam control must be enabled first":
            await message.reply_text(
                "请先开启垃圾信息过滤功能（使用 /spam 命令），才能设置过滤功能。"
            )
        else:
            await message.reply_text(f"操作失败: {error}")


async def spam_help_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 处理稳定 ``spam_help`` callback / Handle the stable ``spam_help`` callback.

    @param update Telegram Update / Telegram Update.
    @param context PTB context / PTB context.
    @return None / None.
    """

    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return
    capability = _capability(context)
    key = (SPAM_HELP_CALLBACK_DATA, chat.id, query.from_user.id)
    if not capability.callback_cooldown.try_acquire(key):
        await query.answer("请不要频繁点击按钮", show_alert=True)
        return
    if not await _is_administrator(context, chat.id, query.from_user.id):
        await query.answer("只有管理员可以查看此功能", show_alert=True)
        return
    await query.answer("正在加载帮助信息...")
    try:
        await query.edit_message_text(
            SPAM_CONTROL_HELP_TEXT,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat.id,
            text=SPAM_CONTROL_HELP_TEXT_PLAIN,
        )


async def keyword_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 处理 ``/keyword`` 配置命令 / Handle the ``/keyword`` configuration command.

    @param update Telegram Update / Telegram Update.
    @param context PTB context / PTB context.
    @return None / None.
    """

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    capability = _capability(context)
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.reply_text(
            "此命令只能在群组中使用。\nThis command can only be used in groups."
        )
        return
    if not await _is_administrator(context, chat.id, user.id):
        await message.reply_text(
            "只有群组管理员才能使用此命令。\nOnly group administrators can use this command."
        )
        return

    args = tuple(context.args or ())
    if not args:
        await _show_keywords(
            message,
            await capability.commands.read(ChatId(chat.id)),
        )
        return
    subcommand = args[0].casefold()
    try:
        if subcommand == "add":
            if len(args) < 3:
                await message.reply_text(
                    "添加关键词的正确格式是：\n"
                    "/keyword add <触发关键词> <回复内容>\n\n"
                    "回复内容支持 Telegram HTML 格式。"
                )
                return
            keyword = args[1]
            response = _sanitize_html(" ".join(args[2:]))
            before = await capability.commands.read(ChatId(chat.id))
            existed = any(
                item.keyword.casefold() == keyword.casefold()
                for item in before.keyword_replies
            )
            await capability.commands.put_keyword_reply(
                ChatId(chat.id),
                UserId(user.id),
                keyword,
                response,
            )
            await message.reply_text(
                f"已{'更新' if existed else '添加'}关键词触发器：'{keyword}'"
            )
            return
        if subcommand == "del":
            if len(args) < 2:
                await message.reply_text(
                    "删除关键词的正确格式是：\n/keyword del <触发关键词>"
                )
                return
            removed = await capability.commands.remove_keyword_reply(
                ChatId(chat.id),
                UserId(user.id),
                args[1],
            )
            await message.reply_text(
                f"已删除关键词触发器：'{args[1]}'"
                if removed
                else f"未找到关键词：'{args[1]}'"
            )
            return
        await message.reply_text(
            "未知的子命令。可用的命令有：\n"
            "/keyword - 显示关键词列表\n"
            "/keyword add <触发关键词> <回复内容> - 添加关键词\n"
            "/keyword del <触发关键词> - 删除关键词"
        )
    except ModerationLimitExceeded:
        await message.reply_text(
            "每个群组最多只能设置10个关键词，请先删除一些关键词再添加。"
        )
    except ValueError as error:
        await message.reply_text(f"操作失败: {error}")


async def report_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """@brief 将回复目标持久化举报给管理员 / Persistently report a replied-to message to administrators.

    @param update Telegram Update / Telegram Update.
    @param context PTB context / PTB context.
    @return None / None.
    """

    message = update.message
    chat = update.effective_chat
    reporter = update.effective_user
    if message is None or chat is None or reporter is None:
        return
    capability = _capability(context)
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.reply_text("此命令只能在群组中使用。")
        return
    target = message.reply_to_message
    if target is None or target.from_user is None:
        await message.reply_text("请回复您要举报的消息，并附带 /report 命令。")
        return
    try:
        outcome = await capability.reports.report(
            ReportRequest(
                key=ReportKey(
                    chat_id=ChatId(chat.id),
                    message_id=MessageId(target.message_id),
                    reporter_id=UserId(reporter.id),
                ),
                reported_user_id=UserId(target.from_user.id),
                reported_user_name=target.from_user.full_name,
                reporter_name=reporter.full_name,
                chat_title=chat.title or "未知群组",
                reported_text=target.text or target.caption or "消息内容无法获取",
            )
        )
    except Exception as error:
        logger.error("举报处理失败: %s", error)
        await message.reply_text("举报处理过程中出错，请稍后再试。")
        return
    if outcome.registration is ReportRegistration.DUPLICATE:
        await message.reply_text("您已经举报过这条消息了。")
        return
    delivery = outcome.delivery
    if delivery is not None and delivery.delivered_count:
        await message.reply_text(
            f"您的举报已发送给群组管理员({delivery.delivered_count}/{delivery.administrator_count})。"
        )
    else:
        await message.reply_text("无法发送举报信息给管理员，请直接联系群组管理员处理。")


def _capability(
    context: ContextTypes.DEFAULT_TYPE,
) -> TelegramModerationCapability:
    """@brief 从 bot_data 读取治理 capability / Read the moderation capability from bot_data.

    @param context PTB context / PTB context.
    @return 治理 capability / Moderation capability.
    @raises RuntimeError capability 未装配 / If the capability is not composed.
    """

    value = context.bot_data.get(MODERATION_CAPABILITY_DATA_KEY)
    if not isinstance(value, TelegramModerationCapability):
        raise RuntimeError("Telegram moderation capability is not configured")
    return value


async def _is_administrator(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
) -> bool:
    """@brief 读取管理员权限 / Read administrator permission.

    @param context PTB context / PTB context.
    @param chat_id 群组 ID / Group identifier.
    @param user_id 用户 ID / User identifier.
    @return 管理员或 owner 为 True / True for administrator or owner.
    """

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception as error:
        logger.error("检查群管理员权限失败: %s", error)
        return False
    return member.status in {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }


async def _bot_can_delete(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> bool:
    """@brief 检查 Bot 删除权限 / Check the Bot's deletion permission.

    @param context PTB context / PTB context.
    @param chat_id 群组 ID / Group identifier.
    @return 可删除为 True / True when deletion is permitted.
    """

    try:
        member = await context.bot.get_chat_member(chat_id, context.bot.id)
    except Exception as error:
        logger.error("检查 Bot 删除权限失败: %s", error)
        return False
    return bool(getattr(member, "can_delete_messages", False))


async def _show_spam_help(message: Message) -> None:
    """@brief 发送 spam 帮助 / Send spam help.

    @param message 回复消息 / Reply message.
    @return None / None.
    """

    try:
        await message.reply_text(
            SPAM_CONTROL_HELP_TEXT,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        await message.reply_text(SPAM_CONTROL_HELP_TEXT_PLAIN)


async def _list_spam_rules(message: Message, group: GroupModeration) -> None:
    """@brief 渲染垃圾规则列表 / Render the spam-rule list.

    @param message 回复消息 / Reply message.
    @param group 群组聚合 / Group aggregate.
    @return None / None.
    """

    if not group.spam_rules:
        await message.reply_text("当前群组没有设置任何自定义垃圾词。")
        return
    lines = ["当前群组的自定义垃圾词列表：", ""]
    for index, rule in enumerate(group.spam_rules, 1):
        label = "正则" if rule.kind is RuleKind.REGEX else "关键词"
        value = f"//{rule.pattern}" if rule.kind is RuleKind.REGEX else rule.pattern
        lines.append(f"{index}. {label}: '{value}'")
    lines.extend(
        [
            "",
            "使用 /spam add <垃圾词> 添加垃圾词",
            "使用 /spam del <垃圾词> 删除垃圾词",
        ]
    )
    await message.reply_text("\n".join(lines))


async def _show_keywords(message: Message, group: GroupModeration) -> None:
    """@brief 渲染关键词回复列表 / Render the keyword-reply list.

    @param message 回复消息 / Reply message.
    @param group 群组聚合 / Group aggregate.
    @return None / None.
    """

    if not group.keyword_replies:
        await message.reply_text(
            "此群组尚未设置任何关键词。\n\n"
            "使用 /keyword add <触发关键词> <回复内容> 添加关键词\n"
            "使用 /keyword del <触发关键词> 删除关键词\n\n"
            "回复内容支持 Telegram HTML 格式。"
        )
        return
    lines = ["当前群组的关键词列表：", ""]
    for index, item in enumerate(group.keyword_replies, 1):
        response = (
            f"{item.response[:30]}..." if len(item.response) > 30 else item.response
        )
        lines.extend(
            [
                f"{index}. 触发词: '{item.keyword}'",
                f"   回复: '{response}'",
                "",
            ]
        )
    lines.extend(
        [
            "使用 /keyword add <触发关键词> <回复内容> 添加关键词",
            "使用 /keyword del <触发关键词> 删除关键词",
        ]
    )
    await message.reply_text("\n".join(lines))


def _sanitize_html(content: str) -> str:
    """@brief 转义非白名单 HTML tag / Escape non-allowlisted HTML tags.

    @param content 用户配置回复 / User-configured response.
    @return 可存储回复 / Storable response.
    """

    def replace(match: re.Match[str]) -> str:
        """@brief 逐 tag 决定保留或转义 / Keep or escape one tag.

        @param match tag match / Tag match.
        @return 原 tag 或 HTML 转义文本 / Original tag or escaped text.
        """

        return (
            match.group(0)
            if match.group(1).casefold() in _ALLOWED_HTML_TAGS
            else html.escape(match.group(0))
        )

    return _HTML_TAG.sub(replace, content)


__all__ = [
    "SPAM_HELP_CALLBACK_DATA",
    "keyword_command",
    "report_command",
    "spam_help_callback",
    "toggle_spam_control",
]
