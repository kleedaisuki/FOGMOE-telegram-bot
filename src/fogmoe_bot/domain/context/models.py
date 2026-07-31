from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Self
from uuid import UUID

from fogmoe_bot.domain.accounts.plan import AccountPlan
from fogmoe_bot.domain.assistant.messages import CanonicalMessage
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
    @param user_id Telegram 用户 ID / Telegram user identifier visible to the model.
    @param username Telegram username / Telegram username visible to the model.
    @param display_name Telegram 显示名 / Telegram display name visible to the model.
    """

    coins: int
    plan: AccountPlan
    permission: int
    profile: UserProfileSnapshot | None = None
    personal_info: str = ""
    diary_exists: bool = False
    user_id: int | None = None
    username: str | None = None
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class ChatMessageContext:
    """@brief 聊天消息上下文 / Chat message context.

    @param chat_type Telegram 聊天类型 / Telegram chat type.
    @param chat_title 群聊标题 / Group chat title.
    @param timestamp 消息时间戳文本 / Message timestamp text.
    @param user_name 发送者显示名 / Sender display name.
    @param username 可选 Telegram username / Optional Telegram username.
    @param user_id Telegram 用户 ID / Telegram user identifier.
    @param message_text 用户消息正文 / User message body.
    @param message_id Telegram 消息 ID / Telegram message id.
    @param message_thread_id Telegram Topic ID / Telegram topic identifier.
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
    username: str | None = None
    user_id: int | None = None
    message_id: str | int | None = None
    message_thread_id: int | None = None
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
    @param message_thread_id 当前 Telegram Topic ID / Current Telegram topic identifier.
    """

    user_id: int
    is_group: bool = False
    group_id: int | None = None
    message_id: int | None = None
    message_thread_id: int | None = None

    def __post_init__(self) -> None:
        """@brief 校验私人/群/Topic 作用域闭集 / Validate the private/group/topic scope sum type.

        @return None / None.
        @raise ValueError ID 或作用域组合非法 / An identifier or scope combination is invalid.
        """

        if isinstance(self.user_id, bool) or self.user_id <= 0:
            raise ValueError("ConversationScope user_id must be positive")
        if self.is_group != (self.group_id is not None):
            raise ValueError("Group ConversationScope requires exactly one group_id")
        if isinstance(self.group_id, bool) or self.group_id == 0:
            raise ValueError("ConversationScope group_id cannot be zero")
        if not self.is_group and self.message_thread_id is not None:
            raise ValueError(
                "Private ConversationScope cannot have a message_thread_id"
            )
        if self.message_id is not None and (
            isinstance(self.message_id, bool) or self.message_id <= 0
        ):
            raise ValueError("ConversationScope message_id must be positive")
        if self.message_thread_id is not None and (
            isinstance(self.message_thread_id, bool) or self.message_thread_id <= 0
        ):
            raise ValueError("ConversationScope message_thread_id must be positive")


@dataclass(frozen=True, slots=True)
class RuntimeMessageReplacement:
    """@brief 运行时消息替换 / Runtime message replacement.

    @param persisted_message 持久化历史中的规范消息 / Canonical message persisted in history.
    @param runtime_message 发给模型的规范消息 / Canonical message sent to the model.
    @note 用于图片等多模态消息：数据库保存可读文本，模型调用可替换为带 image part 的
        canonical message。/ Used for multimodal messages: the database stores readable text
        while model calls may replace it with a canonical message carrying an image part.
    """

    persisted_message: CanonicalMessage
    runtime_message: CanonicalMessage


class ContextState:
    """@brief 一次推理尝试的独立上下文快照 / Independent context snapshot for one inference attempt.

    @note 该实体只能经 ``create`` 建立；消息链、当前 Query 与 cache prefix 只能通过具名
        领域动作改变，读取始终返回不可变快照。/ This entity can only be established through
        ``create``. Its message history, current query, and cache prefix change only through named
        domain operations, while reads always expose immutable snapshots.
    """

    _context_id: UUID
    """@brief ContextState 实体标识 / ContextState entity identity."""
    _scope: ConversationScope
    """@brief 本回合可信会话范围 / Trusted conversation scope for this turn."""
    _user_state: UserState
    """@brief 本回合冻结用户状态 / User state frozen for this turn."""
    _messages: list[CanonicalMessage]
    """@brief 聚合内部 canonical 消息链 / Aggregate-internal canonical message history."""
    _tool_context: Mapping[str, object]
    """@brief 不可变工具范围映射 / Immutable tool-scope mapping."""
    _text_fallback_messages: list[CanonicalMessage] | None
    """@brief 可选纯文本模型消息链 / Optional text-only model history."""
    _current_user_text: str | None
    """@brief 当前 Turn 未改写 Query / Unrewritten query of the current Turn."""
    _stable_prefix_message_count: int | None
    """@brief provider cache 可复用前缀长度 / Provider-cache reusable prefix length."""

    __slots__ = (
        "_context_id",
        "_current_user_text",
        "_messages",
        "_scope",
        "_stable_prefix_message_count",
        "_text_fallback_messages",
        "_tool_context",
        "_user_state",
    )
    """@brief ContextState 私有状态槽 / Private ContextState slots."""

    def __init__(self) -> None:
        """@brief 拒绝绕过领域构造器 / Reject bypassing the domain constructor.

        @return 永不返回 / Never returns.
        @raise TypeError 必须使用 ``create`` / ``create`` must be used.
        """

        raise TypeError("Use ContextState.create()")

    @classmethod
    def create(
        cls,
        *,
        context_id: UUID,
        scope: ConversationScope,
        user_state: UserState,
        messages: Iterable[CanonicalMessage],
        tool_context: Mapping[str, object],
        text_fallback_messages: Iterable[CanonicalMessage] | None = None,
        current_user_text: str | None = None,
        stable_prefix_message_count: int | None = None,
    ) -> Self:
        """@brief 建立一个已验证的 attempt-local Context / Establish a validated attempt-local Context.

        @param context_id ContextState 实体标识 / ContextState entity identifier.
        @param scope 本回合可信作用域 / Trusted scope for this turn.
        @param user_state 本回合冻结用户状态 / User state frozen for this turn.
        @param messages 初始 canonical 消息链 / Initial canonical message history.
        @param tool_context Agent 工具的显式只读范围 / Explicit read-only scope for Agent tools.
        @param text_fallback_messages 可选纯文本降级消息链 / Optional text-only fallback history.
        @param current_user_text 当前 Turn 未改写 Query / Unrewritten current-Turn query.
        @param stable_prefix_message_count provider cache 可复用前缀长度 /
            Provider-cache reusable prefix length.
        @return 新 ContextState / New ContextState.
        @raise TypeError 字段类型不符合领域契约时抛出 / Raised when field types violate the domain contract.
        @raise ValueError 身份、Query 或 prefix 非法时抛出 / Raised for an invalid identity, query, or prefix.
        """

        if not isinstance(context_id, UUID):
            raise TypeError("ContextState context_id must be UUID")
        if context_id.int == 0:
            raise ValueError("ContextState context_id cannot be nil")
        if not isinstance(scope, ConversationScope):
            raise TypeError("ContextState scope must be ConversationScope")
        if not isinstance(user_state, UserState):
            raise TypeError("ContextState user_state must be UserState")
        if not isinstance(tool_context, Mapping):
            raise TypeError("ContextState tool_context must be a mapping")
        if not all(isinstance(key, str) for key in tool_context):
            raise TypeError("ContextState tool_context keys must be strings")

        canonical_messages = _validated_context_messages(messages, label="messages")
        fallback = (
            None
            if text_fallback_messages is None
            else _validated_context_messages(
                text_fallback_messages,
                label="text_fallback_messages",
            )
        )
        _validate_current_user_text(current_user_text)
        _validate_stable_prefix(stable_prefix_message_count, canonical_messages)

        state = object.__new__(cls)
        state._context_id = context_id
        state._scope = scope
        state._user_state = user_state
        state._messages = canonical_messages
        state._tool_context = MappingProxyType(dict(tool_context))
        state._text_fallback_messages = fallback
        state._current_user_text = current_user_text
        state._stable_prefix_message_count = stable_prefix_message_count
        return state

    @property
    def context_id(self) -> UUID:
        """@brief 返回实体标识 / Return the entity identity.

        @return Context UUID / Context UUID.
        """

        return self._context_id

    @property
    def scope(self) -> ConversationScope:
        """@brief 返回可信会话范围 / Return the trusted conversation scope.

        @return 会话范围 / Conversation scope.
        """

        return self._scope

    @property
    def user_state(self) -> UserState:
        """@brief 返回冻结用户状态 / Return the frozen user state.

        @return 用户状态 / User state.
        """

        return self._user_state

    @property
    def messages(self) -> tuple[CanonicalMessage, ...]:
        """@brief 返回不可变 canonical 消息快照 / Return an immutable canonical-message snapshot.

        @return 有序消息元组 / Ordered message tuple.
        """

        return tuple(self._messages)

    @property
    def tool_context(self) -> Mapping[str, object]:
        """@brief 返回不可变工具范围 / Return the immutable tool scope.

        @return 只读范围映射 / Read-only scope mapping.
        """

        return self._tool_context

    @property
    def text_fallback_messages(self) -> tuple[CanonicalMessage, ...] | None:
        """@brief 返回可选纯文本消息快照 / Return the optional text-only message snapshot.

        @return 消息元组或 None / Message tuple or None.
        """

        if self._text_fallback_messages is None:
            return None
        return tuple(self._text_fallback_messages)

    @property
    def current_user_text(self) -> str | None:
        """@brief 返回当前未改写 Query / Return the current unrewritten query.

        @return Query 或 None / Query or None.
        """

        return self._current_user_text

    @property
    def stable_prefix_message_count(self) -> int | None:
        """@brief 返回 provider cache 稳定前缀长度 / Return the provider-cache stable-prefix length.

        @return 前缀长度或 None / Prefix length or None.
        """

        return self._stable_prefix_message_count

    def fork_for_route(
        self,
        messages: Iterable[CanonicalMessage] | None = None,
    ) -> Self:
        """@brief 为一个候选 route 建立隔离分支 / Fork an isolated branch for one candidate route.

        @param messages 可选候选模型消息视图 / Optional candidate-model message view.
        @return 身份与可信状态相同、消息可独立演进的新分支 /
            New branch with the same identity and trusted state but independent messages.
        """

        return type(self).create(
            context_id=self._context_id,
            scope=self._scope,
            user_state=self._user_state,
            messages=self._messages if messages is None else messages,
            tool_context=self._tool_context,
            text_fallback_messages=self._text_fallback_messages,
            current_user_text=self._current_user_text,
            stable_prefix_message_count=self._stable_prefix_message_count,
        )

    def select_model_messages(self, messages: Iterable[CanonicalMessage]) -> None:
        """@brief 为当前 route 选择候选模型消息视图 / Select a candidate-model message view for this route.

        @param messages 与当前 Context 身份相同的候选消息链 / Candidate history for the same Context identity.
        @return None / None.
        """

        self._replace_messages(messages)

    def record_agent_history(self, messages: Iterable[CanonicalMessage]) -> None:
        """@brief 记录本 attempt 已形成的 Agent canonical 历史 / Record canonical Agent history formed in this attempt.

        @param messages 包含新稳定事件的完整消息链 / Complete history containing newly stable events.
        @return None / None.
        """

        self._replace_messages(messages)

    def adopt_route_history(self, route_context: ContextState) -> None:
        """@brief 接纳成功 route 的消息历史 / Adopt the successful route's message history.

        @param route_context 同一实体的成功 route 分支 / Successful route branch of the same entity.
        @return None / None.
        @raise ValueError 分支属于另一 Context 或可信范围时抛出 /
            Raised when the branch belongs to another Context or trusted scope.
        """

        if (
            route_context.context_id != self._context_id
            or route_context.scope != self._scope
            or route_context.user_state != self._user_state
            or route_context.tool_context != self._tool_context
        ):
            raise ValueError("Cannot adopt history from another ContextState")
        self._replace_messages(route_context.messages)
        fallback = route_context.text_fallback_messages
        self._text_fallback_messages = None if fallback is None else list(fallback)

    def identify_current_user_text(self, text: str) -> None:
        """@brief 记录当前 Turn 未改写 Query / Identify the current Turn's unrewritten query.

        @param text Working Memory 检索使用的原始文本 / Original text used for Working Memory retrieval.
        @return None / None.
        @raise ValueError 文本为空时抛出 / Raised when the text is blank.
        """

        _validate_current_user_text(text)
        self._current_user_text = text

    def define_stable_prefix(self, message_count: int) -> None:
        """@brief 定义 provider cache 可复用的稳定前缀 / Define the stable prefix reusable by provider caching.

        @param message_count 当前消息链的前缀长度 / Prefix length within the current history.
        @return None / None.
        @raise ValueError 前缀超出当前消息链时抛出 / Raised when the prefix exceeds the current history.
        """

        _validate_stable_prefix(message_count, self._messages)
        self._stable_prefix_message_count = message_count

    def _replace_messages(self, messages: Iterable[CanonicalMessage]) -> None:
        """@brief 原子替换内部消息链并重验 prefix / Atomically replace internal history and revalidate its prefix.

        @param messages 新 canonical 消息链 / New canonical message history.
        @return None / None.
        """

        replacement = _validated_context_messages(messages, label="messages")
        _validate_stable_prefix(self._stable_prefix_message_count, replacement)
        self._messages = replacement


def _validated_context_messages(
    messages: Iterable[CanonicalMessage],
    *,
    label: str,
) -> list[CanonicalMessage]:
    """@brief 物化并校验 Context canonical 消息 / Materialize and validate Context canonical messages.

    @param messages 候选消息 iterable / Candidate message iterable.
    @param label 面向开发者的字段名 / Developer-facing field label.
    @return 独立 canonical 消息列表 / Independent canonical message list.
    @raise TypeError 输入不可迭代或包含非 canonical 消息时抛出 /
        Raised when input is not iterable or contains a non-canonical message.
    """

    try:
        materialized = list(messages)
    except TypeError as error:
        raise TypeError(
            f"ContextState {label} must be canonical V2 messages"
        ) from error
    if not all(isinstance(message, CanonicalMessage) for message in materialized):
        raise TypeError(f"ContextState {label} must be canonical V2 messages")
    return materialized


def _validate_current_user_text(text: str | None) -> None:
    """@brief 校验可选当前 Query / Validate the optional current query.

    @param text 候选 Query / Candidate query.
    @return None / None.
    @raise TypeError Query 不是字符串或 None 时抛出 / Raised when query is neither a string nor None.
    @raise ValueError Query 为空时抛出 / Raised when query is blank.
    """

    if text is not None and not isinstance(text, str):
        raise TypeError("ContextState current_user_text must be a string or None")
    if text is not None and not text.strip():
        raise ValueError("ContextState current_user_text cannot be blank")


def _validate_stable_prefix(
    message_count: int | None,
    messages: list[CanonicalMessage],
) -> None:
    """@brief 校验 cache prefix 位于消息链内 / Validate that a cache prefix lies within its history.

    @param message_count 候选前缀长度 / Candidate prefix length.
    @param messages 当前 canonical 消息链 / Current canonical history.
    @return None / None.
    @raise TypeError 前缀不是整数或 None 时抛出 / Raised when the prefix is neither an integer nor None.
    @raise ValueError 前缀超出消息链时抛出 / Raised when the prefix falls outside the history.
    """

    if message_count is not None and (
        isinstance(message_count, bool) or not isinstance(message_count, int)
    ):
        raise TypeError("ContextState stable_prefix_message_count must be int or None")
    if message_count is not None and not 0 <= message_count <= len(messages):
        raise ValueError(
            "ContextState stable_prefix_message_count must be within messages"
        )
