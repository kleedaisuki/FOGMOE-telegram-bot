from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fogmoe_bot.domain.user_profile.models import UserProfileSnapshot


@dataclass(frozen=True, slots=True)
class UserState:
    """@brief 用户运行时状态 / Runtime user state.

    @param coins 用户当前硬币数 / Current user coin balance.
    @param plan 用户订阅计划 / User subscription plan.
    @param permission 用户权限等级 / User permission level.
    @param profile acceptance 时冻结的 User Profile / User Profile frozen at acceptance.
    @param personal_info 用户自定义个人信息 / User-defined personal information.
    @param diary_exists 是否存在用户日记 / Whether user diary exists.
    """

    coins: int
    plan: str
    permission: int
    profile: UserProfileSnapshot | None = None
    personal_info: str = ""
    diary_exists: bool = False


@dataclass(frozen=True, slots=True)
class ChatMessageContext:
    """@brief 聊天消息上下文 / Chat message context.

    @param chat_type Telegram 聊天类型 / Telegram chat type.
    @param chat_title 群聊标题 / Group chat title.
    @param timestamp 消息时间戳文本 / Message timestamp text.
    @param user_name Telegram 用户名 / Telegram username.
    @param message_text 用户消息正文 / User message body.
    @param message_id Telegram 消息 ID / Telegram message id.
    @param edited 是否为编辑消息 / Whether the message is edited.
    @param edited_at 编辑时间 / Edit timestamp.
    @param forward_type 转发来源类型 / Forward origin type.
    @param forward_origin_timestamp 转发来源时间 / Forward origin timestamp.
    @param forward_user 转发来源用户 / Forward origin user.
    @param forward_name 转发来源名称 / Forward origin name.
    @param forward_chat 转发来源聊天 / Forward origin chat.
    @param forward_message_id 转发来源消息 ID / Forward origin message id.
    @param forward_author_signature 转发作者签名 / Forward author signature.
    @param reply_user 被回复用户 / Replied user.
    @param reply_text 被回复文本 / Replied text.
    @param reply_type 被回复消息类型 / Replied message type.
    @param reply_caption 被回复媒体说明 / Replied media caption.
    @param reply_summary 被回复媒体摘要 / Replied media summary.
    @param reply_emoji 被回复贴纸表情 / Replied sticker emoji.
    @param media_type 媒体类型 / Media type.
    @param media_description 媒体描述 / Media description.
    @param media_emoji 媒体表情 / Media emoji.
    """

    chat_type: str
    chat_title: str | None
    timestamp: str
    user_name: str
    message_text: str
    message_id: str | int | None = None
    edited: bool = False
    edited_at: str | None = None
    forward_type: str | None = None
    forward_origin_timestamp: str | None = None
    forward_user: str | None = None
    forward_name: str | None = None
    forward_chat: str | None = None
    forward_message_id: str | None = None
    forward_author_signature: str | None = None
    reply_user: str | None = None
    reply_text: str | None = None
    reply_type: str | None = None
    reply_caption: str | None = None
    reply_summary: str | None = None
    reply_emoji: str | None = None
    media_type: str | None = None
    media_description: str | None = None
    media_emoji: str | None = None


@dataclass(frozen=True, slots=True)
class ScheduledTaskContext:
    """@brief 定时任务上下文 / Scheduled task context.

    @param timestamp 触发时刻 / Trigger timestamp.
    @param scheduled_at 任务创建时刻 / Schedule creation timestamp.
    @param scheduled_for 计划触发时刻 / Planned trigger timestamp.
    @param trigger_reason 触发原因 / Trigger reason.
    @param context_text 创建任务时的上下文 / Context captured when scheduling.
    @param instruction 执行指令 / Execution instruction.
    """

    timestamp: datetime
    scheduled_at: datetime | None
    scheduled_for: datetime | None
    trigger_reason: str
    context_text: str | None
    instruction: str


@dataclass(frozen=True, slots=True)
class ConversationScope:
    """@brief 对话作用域 / Conversation scope.

    @param user_id Telegram 用户 ID / Telegram user id.
    @param is_group 是否来自群聊 / Whether the turn is from a group chat.
    @param group_id 群聊 ID / Group chat id.
    @param message_id 当前消息 ID / Current message id.
    """

    user_id: int
    is_group: bool = False
    group_id: int | None = None
    message_id: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeMessageReplacement:
    """@brief 运行时消息替换 / Runtime message replacement.

    @param persisted_content 持久化历史中的文本内容 / Text content persisted in history.
    @param runtime_message 发给模型的运行时消息 / Runtime message sent to the model.
    @note 用于图片等多模态消息：数据库保存可读文本，模型调用可替换为多模态 payload。
    / Used for multimodal messages: the database stores readable text while model calls may use multimodal payloads.
    """

    persisted_content: str
    runtime_message: dict[str, object]


@dataclass(slots=True)
class ContextState:
    """@brief 一次推理尝试的独立上下文快照 / Independent context snapshot for one inference attempt.

    @param scope 本次回合的会话作用域 / Conversation scope for this turn.
    @param user_state 本次回合可见的用户状态 / User state visible to this turn.
    @param messages 已提交或当前回合临时的模型消息链 / Committed or in-turn model message chain.
    @param tool_context 传给 Agent 工具的显式作用域 / Explicit scope passed to Agent tools.
    @param text_fallback_messages 纯文本模型的降级消息链 / Text-only fallback message chain.
    """

    scope: ConversationScope
    user_state: UserState
    messages: list[dict[str, object]]
    tool_context: dict[str, object]
    text_fallback_messages: list[dict[str, object]] | None = None
