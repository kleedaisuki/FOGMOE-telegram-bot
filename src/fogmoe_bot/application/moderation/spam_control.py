import html
import logging
import re
import threading
import time
from collections import defaultdict
from dataclasses import replace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from fogmoe_bot.application.moderation.service import ModerationService
from fogmoe_bot.application.telegram.command_cooldown import cooldown
from fogmoe_bot.domain.moderation import (
    ActorRole,
    ChatId,
    ContentKind,
    EnforcementFailureMode,
    EnforcementResult,
    GroupModerationPolicy,
    MessageId,
    ModerationDecision,
    ModerationRequest,
    RuleKind,
    UserId,
    Verdict,
)
from fogmoe_bot.domain.moderation.engine import MENTION_PATTERN, URL_PATTERN
from fogmoe_bot.infrastructure.config import BASE_DIR
from fogmoe_bot.infrastructure.database.repositories import moderation_repository
from fogmoe_bot.infrastructure.moderation import (
    CachedModerationConfigurationProvider,
    FileModerationRuleProvider,
)

SPAM_FILE_PATH = BASE_DIR / "resources" / "spam_words.txt"
"""@brief 全局审核规则文件路径 / Global moderation-rule file path."""

MODERATION_CONFIGURATION = CachedModerationConfigurationProvider(ttl_seconds=300.0)
"""@brief 群组审核配置 provider / Group moderation-configuration provider."""

GLOBAL_RULES = FileModerationRuleProvider(SPAM_FILE_PATH)
"""@brief 文件型全局规则 provider / File-backed global rule provider."""

MODERATION_SERVICE = ModerationService(
    MODERATION_CONFIGURATION,
    MODERATION_CONFIGURATION,
    GLOBAL_RULES,
)
"""@brief 内容审核应用服务 / Content-moderation application service."""

# 速率限制器 {chat_id: {user_id: count}}
warning_rate_limiter: defaultdict[int, defaultdict[int, int]] = defaultdict(
    lambda: defaultdict(int)
)
rate_limit_lock = threading.Lock()
WARNING_RESET_INTERVAL = 3600  # 警告计数重置时间：1小时

# 添加全局防抖字典，记录用户最后点击时间
callback_cooldown: dict[str, float] = {}
callback_lock = threading.Lock()
CALLBACK_COOLDOWN_TIME = 3  # 按钮冷却时间（秒）

# 统一的帮助文本，在多处复用
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

# 简化版帮助文本（当HTML显示失败时使用）
SPAM_CONTROL_HELP_TEXT_PLAIN = (
    "垃圾信息过滤功能命令说明：\n\n"
    "基本命令：\n"
    "/spam - 开启/关闭垃圾信息过滤\n"
    "/spam help - 显示此帮助信息\n\n"
    "过滤设置：\n"
    "/spam links on/off - 开启/关闭链接过滤\n"
    "/spam mentions on/off - 开启/关闭@提及过滤\n\n"
    "自定义垃圾词：\n"
    "/spam list - 列出自定义垃圾词\n"
    "/spam add <词> - 添加垃圾词\n"
    "/spam del <词> - 删除垃圾词\n"
)

async def load_spam_control_status(group_id: int) -> tuple[bool, bool, bool]:
    """@brief 兼容旧调用并读取类型化策略 / Read a typed policy for legacy callers.

    @param group_id Telegram 群组 ID / Telegram chat ID.
    @return 启用、链接过滤、提及过滤三元组 / Enabled, link, and mention flags.
    """

    MODERATION_CONFIGURATION.invalidate_policy(ChatId(group_id))
    policy = await MODERATION_CONFIGURATION.get_policy(ChatId(group_id))
    return policy.enabled, policy.block_links, policy.block_mentions


async def is_spam_control_enabled(group_id: int) -> bool:
    """@brief 判断群组是否启用审核 / Check whether moderation is enabled."""

    return (await MODERATION_CONFIGURATION.get_policy(ChatId(group_id))).enabled


async def is_link_blocking_enabled(group_id: int) -> bool:
    """@brief 判断群组是否拦截链接 / Check whether links are blocked."""

    return (await MODERATION_CONFIGURATION.get_policy(ChatId(group_id))).block_links


async def is_mention_blocking_enabled(group_id: int) -> bool:
    """@brief 判断群组是否拦截提及 / Check whether mentions are blocked."""

    return (await MODERATION_CONFIGURATION.get_policy(ChatId(group_id))).block_mentions


def contains_url(text: str) -> tuple[bool, str | None]:
    """@brief 兼容旧调用并检查链接 / Check links for legacy callers."""

    if not text:
        return False, None
    match = URL_PATTERN.search(text)
    return (True, match.group(0)) if match else (False, None)


def contains_mention(text: str) -> tuple[bool, str | None]:
    """@brief 兼容旧调用并检查提及 / Check mentions for legacy callers."""

    if not text:
        return False, None
    match = MENTION_PATTERN.search(text)
    return (True, match.group(0)) if match else (False, None)


def load_spam_words() -> None:
    """@brief 兼容旧初始化入口并刷新全局规则 / Refresh global rules for legacy setup."""

    GLOBAL_RULES.refresh(force=True)


async def load_custom_spam_keywords(
    group_id: int,
) -> tuple[list[str], list[re.Pattern[str]]]:
    """@brief 兼容旧调用并刷新群组规则 / Refresh group rules for legacy callers."""

    rules = await MODERATION_CONFIGURATION.refresh_group_rules(ChatId(group_id))
    keywords = [rule.pattern.lower() for rule in rules if rule.kind is RuleKind.LITERAL]
    patterns = [
        re.compile(rule.pattern, re.IGNORECASE)
        for rule in rules
        if rule.kind is RuleKind.REGEX
    ]
    return keywords, patterns


async def get_custom_spam_keywords(
    group_id: int,
) -> tuple[list[str], list[re.Pattern[str]]]:
    """@brief 兼容旧调用并读取群组规则 / Read group rules for legacy callers."""

    rules = await MODERATION_CONFIGURATION.get_group_rules(ChatId(group_id))
    keywords = [rule.pattern.lower() for rule in rules if rule.kind is RuleKind.LITERAL]
    patterns = [
        re.compile(rule.pattern, re.IGNORECASE)
        for rule in rules
        if rule.kind is RuleKind.REGEX
    ]
    return keywords, patterns


async def has_custom_spam_keywords(group_id: int) -> bool:
    """@brief 判断群组是否有自定义规则 / Check for group-specific rules."""

    return bool(await MODERATION_CONFIGURATION.get_group_rules(ChatId(group_id)))


async def is_spam_message(message_text: str, group_id: int) -> tuple[bool, str | None]:
    """@brief 兼容旧调用并返回审核命中 / Moderate text for legacy callers."""

    decision = await MODERATION_SERVICE.moderate(
        ModerationRequest(
            chat_id=ChatId(group_id),
            user_id=UserId(0),
            message_id=MessageId(0),
            content=message_text,
            content_kind=ContentKind.TEXT,
            actor_role=ActorRole.MEMBER,
        )
    )
    match = decision.primary_match
    return decision.verdict is Verdict.BLOCK, match.matched_text if match else None

def update_warning_count(chat_id, user_id):
    """更新用户警告次数，返回当前警告次数"""
    with rate_limit_lock:
        # 获取当前计数
        warning_rate_limiter[chat_id][user_id] += 1
        return warning_rate_limiter[chat_id][user_id]

@cooldown
async def toggle_spam_control(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /spam 命令"""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # 仅在群组中有效
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("此命令只能在群组中使用。")
        return

    # 检查用户是否为群组管理员
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status not in ["administrator", "creator"]:
            await update.message.reply_text("只有群组管理员才能使用此命令。")
            return
        is_admin = True  # 标记用户为管理员
    except Exception as e:
        logging.error(f"检查用户权限时出错: {str(e)}")
        await update.message.reply_text("检查权限时出错，请稍后再试。")
        return

    # 解析子命令
    if context.args:
        sub_command = context.args[0].lower()
        
        if sub_command == "links":
            if len(context.args) < 2:
                await update.message.reply_text(
                    "设置链接过滤的正确格式是：\n"
                    "/spam links on - 开启链接过滤\n"
                    "/spam links off - 关闭链接过滤"
                )
                return
            
            option = context.args[1].lower()
            if option == "on":
                await toggle_link_blocking(update, chat_id, user_id, True)
                return
            elif option == "off":
                await toggle_link_blocking(update, chat_id, user_id, False)
                return
            else:
                await update.message.reply_text("参数错误。请使用 on 或 off。")
                return
        
        elif sub_command == "mentions":
            if len(context.args) < 2:
                await update.message.reply_text(
                    "设置@提及过滤的正确格式是：\n"
                    "/spam mentions on - 开启@提及过滤\n"
                    "/spam mentions off - 关闭@提及过滤"
                )
                return
            
            option = context.args[1].lower()
            if option == "on":
                await toggle_mention_blocking(update, chat_id, user_id, True)
                return
            elif option == "off":
                await toggle_mention_blocking(update, chat_id, user_id, False)
                return
            else:
                await update.message.reply_text("参数错误。请使用 on 或 off。")
                return
        
        elif sub_command == "add":
            if len(context.args) < 2:
                await update.message.reply_text(
                    "添加自定义垃圾词的正确格式是：\n"
                    "/spam add <垃圾词>\n\n"
                    "添加正则表达式格式：\n"
                    "/spam add //正则表达式\n\n"
                    "示例：\n"
                    "/spam add 博彩\n"
                    "/spam add //\\d+元.*充值"
                )
                return
            
            keyword = " ".join(context.args[1:])
            await add_custom_spam_keyword(update, chat_id, user_id, keyword)
            return
            
        elif sub_command == "del":
            if len(context.args) < 2:
                await update.message.reply_text("删除自定义垃圾词的正确格式是：\n/spam del <垃圾词>")
                return
                
            keyword = " ".join(context.args[1:])
            await del_custom_spam_keyword(update, chat_id, keyword)
            return
            
        elif sub_command == "list":
            await list_custom_spam_keywords(update, chat_id)
            return
            
        elif sub_command == "help":
            await show_spam_control_help(update)
            return
    
    # 如果没有子命令或子命令不是add/del/list，切换垃圾信息过滤状态
    # 检查机器人是否有必要的权限
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if not bot_member.can_delete_messages:
            await update.message.reply_text("机器人需要有删除消息的权限才能启用垃圾信息过滤功能。请先授予机器人管理员权限并允许其删除消息。")
            return
    except Exception as e:
        logging.error(f"检查机器人权限时出错: {str(e)}")
        await update.message.reply_text("检查机器人权限时出错，请稍后再试。")
        return
    
    # 获取当前状态
    current_status = await is_spam_control_enabled(chat_id)
    
    # 切换状态
    new_status = not current_status
    
    try:
        current_policy = await moderation_repository.fetch_spam_control(chat_id)
        if current_policy is None:
            current_policy = GroupModerationPolicy(chat_id=ChatId(chat_id))
        
        await moderation_repository.upsert_spam_enabled(
            chat_id,
            new_status,
            current_policy.block_links,
            current_policy.block_mentions,
            user_id,
        )
        MODERATION_CONFIGURATION.put_policy(
            replace(current_policy, enabled=new_status)
        )
        
        if new_status:
            # 只对管理员显示"查看更多功能"按钮
            if is_admin:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("查看更多功能 (管理员)", callback_data="spam_help")]
                ])
                await update.message.reply_text(
                    "垃圾信息过滤功能已 ***开启***。我将自动删除可能的垃圾消息并发出警告。", 
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    "垃圾信息过滤功能已 ***开启***。我将自动删除可能的垃圾消息并发出警告。", 
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await update.message.reply_text("垃圾信息过滤功能已 ***关闭***。", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logging.error(f"更新垃圾信息过滤状态时出错: {e}")
        await update.message.reply_text(f"操作失败: {str(e)}")

async def toggle_link_blocking(update: Update, chat_id: int, user_id: int, enable: bool):
    """开启或关闭群组链接过滤功能"""
    # 检查垃圾过滤功能是否已启用，只有启用垃圾过滤功能时才能设置链接过滤
    is_enabled = await is_spam_control_enabled(chat_id)
    if not is_enabled:
        await update.message.reply_text("请先开启垃圾信息过滤功能（使用 /spam 命令），才能设置链接过滤功能。")
        return
    
    # 检查机器人是否有必要的权限
    try:
        bot_member = await update.get_bot().get_chat_member(chat_id, update.get_bot().id)
        if not bot_member.can_delete_messages:
            await update.message.reply_text("机器人需要有删除消息的权限才能使用链接过滤功能。")
            return
    except Exception as e:
        logging.error(f"检查机器人权限时出错: {str(e)}")
        await update.message.reply_text("检查机器人权限时出错，请稍后再试。")
        return
    
    try:
        await moderation_repository.set_spam_link_blocking(chat_id, enable)
        policy = await MODERATION_CONFIGURATION.get_policy(ChatId(chat_id))
        MODERATION_CONFIGURATION.put_policy(replace(policy, block_links=enable))
        
        status_text = "开启" if enable else "关闭"
        await update.message.reply_text(
            f"链接过滤功能已{status_text}。"
            f"{'所有链接消息将被视为垃圾信息处理。' if enable else ''}"
        )
    except Exception as e:
        logging.error(f"更新链接过滤状态时出错: {e}")
        await update.message.reply_text(f"操作失败: {str(e)}")

async def toggle_mention_blocking(update: Update, chat_id: int, user_id: int, enable: bool):
    """开启或关闭群组@mention过滤功能"""
    # 检查垃圾过滤功能是否已启用，只有启用垃圾过滤功能时才能设置@mention过滤
    is_enabled = await is_spam_control_enabled(chat_id)
    if not is_enabled:
        await update.message.reply_text("请先开启垃圾信息过滤功能（使用 /spam 命令），才能设置@mention过滤功能。")
        return
    
    # 检查机器人是否有必要的权限
    try:
        bot_member = await update.get_bot().get_chat_member(chat_id, update.get_bot().id)
        if not bot_member.can_delete_messages:
            await update.message.reply_text("机器人需要有删除消息的权限才能使用@mention过滤功能。")
            return
    except Exception as e:
        logging.error(f"检查机器人权限时出错: {str(e)}")
        await update.message.reply_text("检查机器人权限时出错，请稍后再试。")
        return
    
    try:
        await moderation_repository.set_spam_mention_blocking(chat_id, enable)
        policy = await MODERATION_CONFIGURATION.get_policy(ChatId(chat_id))
        MODERATION_CONFIGURATION.put_policy(replace(policy, block_mentions=enable))
        
        status_text = "开启" if enable else "关闭"
        await update.message.reply_text(
            f"@mention过滤功能已{status_text}。"
            f"{'所有包含@mention的消息将被自动删除。' if enable else ''}"
        )
    except Exception as e:
        logging.error(f"更新@mention过滤状态时出错: {e}")
        await update.message.reply_text(f"操作失败: {str(e)}")

async def add_custom_spam_keyword(update: Update, chat_id: int, user_id: int, keyword: str):
    """添加自定义垃圾词"""
    # 检查是否为正则表达式
    is_regex = False
    if keyword.startswith('//'):
        is_regex = True
        keyword = keyword[2:].strip()  # 去除前缀
        
        # 验证正则表达式是否有效
        try:
            re.compile(keyword)
        except re.error:
            await update.message.reply_text(f"无效的正则表达式: {keyword}")
            return
    
    # 检查关键词长度
    if len(keyword) > 255:
        await update.message.reply_text("垃圾词太长，请不要超过255个字符。")
        return
    
    try:
        existing_keyword = await moderation_repository.group_spam_keyword_exists(
            chat_id,
            keyword,
        )
        
        # 检查自定义垃圾词数量是否达到上限
        if not existing_keyword:
            count = await moderation_repository.count_group_spam_keywords(chat_id)
            
            if count >= 10:
                await update.message.reply_text("每个群组最多只能设置10个自定义垃圾词，请先删除一些再添加。")
                return
        
        await moderation_repository.upsert_group_spam_keyword(
            chat_id,
            keyword,
            is_regex,
            user_id,
        )
        
        # 更新缓存
        await load_custom_spam_keywords(chat_id)
        
        if existing_keyword:
            await update.message.reply_text(f"已更新自定义垃圾词: '{keyword}'")
        else:
            await update.message.reply_text(f"已添加自定义垃圾词: '{keyword}'")
    except Exception as e:
        logging.error(f"添加自定义垃圾词时出错: {e}")
        await update.message.reply_text(f"添加自定义垃圾词时出错: {str(e)}")

async def del_custom_spam_keyword(update: Update, chat_id: int, keyword: str):
    """删除自定义垃圾词"""
    try:
        # 如果输入的是正则表达式格式，去掉前缀
        if keyword.startswith('//'):
            keyword = keyword[2:].strip()
            
        rowcount = await moderation_repository.delete_group_spam_keyword(chat_id, keyword)
        
        if rowcount > 0:
            # 更新缓存
            await load_custom_spam_keywords(chat_id)
            await update.message.reply_text(f"已删除自定义垃圾词: '{keyword}'")
        else:
            await update.message.reply_text(f"未找到自定义垃圾词: '{keyword}'")
    except Exception as e:
        logging.error(f"删除自定义垃圾词时出错: {e}")
        await update.message.reply_text(f"删除自定义垃圾词时出错: {str(e)}")

async def list_custom_spam_keywords(update: Update, chat_id: int):
    """列出群组的自定义垃圾词"""
    try:
        keywords = await moderation_repository.fetch_group_spam_keywords(chat_id)
        
        if not keywords:
            await update.message.reply_text("当前群组没有设置任何自定义垃圾词。")
            return
            
        message = "当前群组的自定义垃圾词列表：\n\n"
        for idx, rule in enumerate(keywords, 1):
            if rule.kind is RuleKind.REGEX:
                message += f"{idx}. 正则: '//{rule.pattern}'\n"
            else:
                message += f"{idx}. 关键词: '{rule.pattern}'\n"
                
        message += "\n使用 /spam add <垃圾词> 添加垃圾词\n"
        message += "使用 /spam del <垃圾词> 删除垃圾词"
        
        await update.message.reply_text(message)
    except Exception as e:
        logging.error(f"获取自定义垃圾词列表时出错: {e}")
        await update.message.reply_text(f"获取自定义垃圾词列表时出错: {str(e)}")

async def show_spam_control_help(update: Update):
    """显示垃圾信息过滤功能的帮助信息"""
    try:
        await update.message.reply_text(SPAM_CONTROL_HELP_TEXT, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"发送帮助信息时出错: {str(e)}")
        # 尝试不使用解析模式发送
        await update.message.reply_text(SPAM_CONTROL_HELP_TEXT_PLAIN)

async def spam_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理垃圾过滤帮助按钮回调，并验证点击者是否为管理员"""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = update.effective_chat.id
    
    # 防抖处理：检查是否在冷却期内
    current_time = time.time()
    with callback_lock:
        user_key = f"{user_id}:{chat_id}:spam_help"
        last_click_time = callback_cooldown.get(user_key, 0)
        
        # 如果上次点击时间距现在小于冷却时间，则忽略此次点击
        if current_time - last_click_time < CALLBACK_COOLDOWN_TIME:
            await query.answer("请不要频繁点击按钮", show_alert=True)
            return
            
        # 记录本次点击时间
        callback_cooldown[user_key] = current_time
        
        # 清理过期的冷却记录（可选，提高内存效率）：
        for key in list(callback_cooldown.keys()):
            if current_time - callback_cooldown[key] > CALLBACK_COOLDOWN_TIME * 2:
                callback_cooldown.pop(key, None)
    
    # 验证点击者是否为管理员
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status not in ["administrator", "creator"]:
            await query.answer("只有管理员可以查看此功能", show_alert=True)
            return
    except Exception as e:
        logging.error(f"验证用户权限时出错: {str(e)}")
        await query.answer("验证权限时出错，请稍后再试", show_alert=True)
        return
    
    # 显示"正在处理"状态
    await query.answer("正在加载帮助信息...")
    
    try:
        await query.edit_message_text(text=SPAM_CONTROL_HELP_TEXT, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"编辑消息时出错: {str(e)}")
        # 如果编辑失败，尝试发送新消息
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=SPAM_CONTROL_HELP_TEXT,
                parse_mode=ParseMode.HTML
            )
        except Exception as send_error:
            logging.error(f"发送帮助消息时出错: {str(send_error)}")
            # 最后尝试不使用解析模式发送
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=SPAM_CONTROL_HELP_TEXT_PLAIN
            )

def get_effective_message(update: Update):
    """@brief 获取普通或编辑后的消息 / Get a normal or edited message."""

    return update.message or update.edited_message

async def _resolve_actor_role(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
) -> ActorRole:
    """@brief 读取并短期缓存消息发送者角色 / Resolve and cache the author role.

    @param context Telegram handler 上下文 / Telegram handler context.
    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param user_id Telegram 用户 ID / Telegram user ID.
    @return 类型化发送者角色 / Typed author role.
    """

    cache_key = f"moderation_role:{chat_id}:{user_id}"
    cached = context.chat_data.get(cache_key)
    expires_at = context.chat_data.get(f"{cache_key}:expires_at", 0.0)
    if isinstance(cached, ActorRole) and time.time() <= expires_at:
        return cached

    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status == "creator":
            role = ActorRole.OWNER
        elif chat_member.status == "administrator":
            role = ActorRole.ADMINISTRATOR
        else:
            role = ActorRole.MEMBER
    except Exception as exc:
        logging.error("获取审核用户角色失败: %s", exc)
        role = ActorRole.MEMBER

    context.chat_data[cache_key] = role
    context.chat_data[f"{cache_key}:expires_at"] = time.time() + 300
    return role


async def enforce_moderation_decision(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    decision: ModerationDecision,
    policy: GroupModerationPolicy,
) -> EnforcementResult:
    """@brief 执行删除和警告副作用 / Execute deletion and warning effects.

    @param update Telegram 更新 / Telegram update.
    @param context Telegram handler 上下文 / Telegram handler context.
    @param decision 审核判决 / Moderation decision.
    @param policy 作出判决的群组策略 / Policy used for the decision.
    @return 类型化处置结果 / Typed enforcement result.
    """

    message = get_effective_message(update)
    match = decision.primary_match
    if message is None or match is None or update.effective_user is None:
        return EnforcementResult(
            decision=decision,
            message_deleted=False,
            warning_sent=False,
            downstream_stopped=policy.failure_mode is EnforcementFailureMode.FAIL_CLOSED,
            error="missing Telegram message or moderation evidence",
        )

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message.message_id,
        )
    except Exception as exc:
        logging.error("执行审核删除失败: chat=%s user=%s error=%s", chat_id, user_id, exc)
        return EnforcementResult(
            decision=decision,
            message_deleted=False,
            warning_sent=False,
            downstream_stopped=policy.failure_mode is EnforcementFailureMode.FAIL_CLOSED,
            error=str(exc),
        )

    warning_count = update_warning_count(chat_id, user_id)
    if match.rule.kind is RuleKind.LINK:
        category = "链接"
        policy_text = "本群组禁止发送链接。"
    elif match.rule.kind is RuleKind.MENTION:
        category = "@提及"
        policy_text = "本群组禁止@提及用户。"
    else:
        category = "垃圾内容"
        policy_text = "持续发送垃圾信息可能导致被禁言或移出群组。"

    warning_sent = False
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ 注意: {update.effective_user.mention_html()} 发送的消息包含{category} "
                f"<tg-spoiler>{html.escape(match.matched_text)}</tg-spoiler>，已被自动删除。\n"
                f"{policy_text}这是第 {warning_count} 次警告。"
            ),
            parse_mode=ParseMode.HTML,
        )
        warning_sent = True
    except Exception as exc:
        logging.error("发送审核警告失败: chat=%s user=%s error=%s", chat_id, user_id, exc)

    logging.info(
        "审核处置完成: chat=%s user=%s kind=%s match=%r",
        chat_id,
        user_id,
        match.rule.kind.name,
        match.matched_text,
    )
    return EnforcementResult(
        decision=decision,
        message_deleted=True,
        warning_sent=warning_sent,
        downstream_stopped=decision.stop_downstream,
    )


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 将 Telegram 消息映射、审核并处置 / Map, moderate, and enforce a Telegram message."""

    message = get_effective_message(update)
    if (
        message is None
        or update.effective_chat is None
        or update.effective_chat.type not in {"group", "supergroup"}
        or update.effective_user is None
    ):
        return

    content = message.text or message.caption or ""
    if not content:
        return

    chat_id = ChatId(update.effective_chat.id)
    policy = await MODERATION_CONFIGURATION.get_policy(chat_id)
    if not policy.enabled:
        return

    actor_role = await _resolve_actor_role(
        context,
        int(chat_id),
        update.effective_user.id,
    )
    if message.caption is not None and message.text is None:
        content_kind = ContentKind.CAPTION
    elif content.startswith("/"):
        content_kind = ContentKind.COMMAND
    else:
        content_kind = ContentKind.TEXT

    decision = await MODERATION_SERVICE.moderate(
        ModerationRequest(
            chat_id=chat_id,
            user_id=UserId(update.effective_user.id),
            message_id=MessageId(message.message_id),
            content=content,
            content_kind=content_kind,
            actor_role=actor_role,
            is_edited=update.edited_message is message,
        )
    )
    if decision.verdict is Verdict.ALLOW:
        return

    result = await enforce_moderation_decision(update, context, decision, policy)
    if result.downstream_stopped:
        raise ApplicationHandlerStop

def setup_spam_control_handlers(dispatcher):
    """注册垃圾信息过滤处理器，不再尝试创建数据库表"""
    # 初始化垃圾词列表
    load_spam_words()
    
    # 添加命令处理器
    dispatcher.add_handler(CommandHandler("spam", toggle_spam_control))
    
    # 添加回调查询处理器
    dispatcher.add_handler(CallbackQueryHandler(spam_help_callback, pattern=r"^spam_help$"))
    
    # 在默认 group=0 的 AI 对话处理器之前运行；命中并成功删除后会停止后续传播。
    # 修改过滤器以包含编辑后的消息
    dispatcher.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & filters.ChatType.GROUPS &
            (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
            process_message
        ),
        group=-10,
    )
    
    # 定期清理警告计数器
    def reset_warning_counters():
        with rate_limit_lock:
            warning_rate_limiter.clear()
            logging.info("已清理垃圾信息警告计数器")
        # 递归设置下一次清理任务
        from threading import Timer
        timer = Timer(WARNING_RESET_INTERVAL, reset_warning_counters)
        timer.daemon = True
        timer.start()
    
    # 设置定时任务清理警告计数
    from threading import Timer
    timer = Timer(WARNING_RESET_INTERVAL, reset_warning_counters)
    timer.daemon = True
    timer.start()
