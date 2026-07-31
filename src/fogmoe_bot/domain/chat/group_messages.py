"""@brief 群聊消息身份、修订与上下文窗口规则 / Group-message identity, revision, and context-window rules.

本模块拥有群消息投影的业务语义；传输层只负责把 provider payload 解析为这些
不可变值，持久化 adapter 只负责原子地落实同一修订规则。/ This module owns the
business semantics of the group-message projection. The transport layer only parses provider
payloads into these immutable values, while persistence adapters atomically enforce the same
revision rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

DEFAULT_GROUP_CONTEXT_MESSAGES = 256
"""@brief 默认群聊短消息窗口 / Default group-context window for short human messages."""

MAX_GROUP_CONTEXT_MESSAGES = 512
"""@brief 单次群聊上下文条数硬上限 / Hard per-page group-context message limit."""

MAX_GROUP_MESSAGE_CHARACTERS = 20_000
"""@brief 单条规范群消息字符上限 / Character limit for one canonical group message."""

GROUP_ATTACHMENT_MARKER = "<group_attachment />"
"""@brief 未导入 Workspace 的惰性群附件标记 / Inert marker for a group attachment not imported into a Workspace."""

GROUP_SERVICE_MESSAGE_MARKER = "[service message]"
"""@brief 非媒体 service 更新的固定可读标记 / Stable readable marker for a non-media service update."""


class GroupMessageKind(StrEnum):
    """@brief 可投影的群消息种类 / Projectable group-message kinds."""

    TEXT = "text"
    """@brief 人类文本 / Human text."""

    PHOTO = "photo"
    """@brief 未导入的图片 / Unimported photo."""

    STICKER = "sticker"
    """@brief 未导入的贴纸 / Unimported sticker."""

    VOICE = "voice"
    """@brief 未导入的语音或音频 / Unimported voice or audio."""

    VIDEO = "video"
    """@brief 未导入的视频或动画 / Unimported video or animation."""

    DOCUMENT = "document"
    """@brief 未导入的文档 / Unimported document."""

    OTHER = "other"
    """@brief 不含用户内容的 service 消息 / Service message containing no user content."""

    @property
    def is_attachment(self) -> bool:
        """@brief 判断种类是否代表未导入附件 / Whether this kind represents an unimported attachment.

        @return 图片、贴纸、音频、视频或文档为 True / True for photos, stickers, audio, video, or documents.
        """

        return self is not GroupMessageKind.TEXT and self is not GroupMessageKind.OTHER


@dataclass(frozen=True, slots=True, order=True)
class GroupConversationScope:
    """@brief 一个群或群 Topic 的稳定会话范围 / Stable scope of a group or group topic.

    @param group_id 非零群 chat ID / Non-zero group-chat identifier.
    @param message_thread_id 可选正 Topic ID / Optional positive topic identifier.
    """

    group_id: int
    """@brief 群 chat ID / Group-chat identifier."""

    message_thread_id: int | None = None
    """@brief Topic ID；None 表示群的普通消息流 / Topic identifier; None means the group's ordinary stream."""

    def __post_init__(self) -> None:
        """@brief 守卫群与 Topic 身份 / Guard group and topic identities.

        @return None / None.
        @raise TypeError 标识符不是严格整数时抛出 / Raised when an identifier is not a strict integer.
        @raise ValueError 标识符超出业务范围时抛出 / Raised when an identifier is outside its business range.
        """

        _nonzero_integer(self.group_id, "group_id")
        if self.message_thread_id is not None:
            _positive_integer(self.message_thread_id, "message_thread_id")


@dataclass(frozen=True, slots=True, order=True)
class GroupMessageIdentity:
    """@brief 群内消息的完整身份 / Complete identity of a message inside a group.

    @param scope 群或 Topic 范围 / Group or topic scope.
    @param message_id 群内正消息 ID / Positive message identifier within the group.
    """

    scope: GroupConversationScope
    """@brief 消息所属群或 Topic / Owning group or topic."""

    message_id: int
    """@brief 群内消息 ID / Message identifier within the group."""

    def __post_init__(self) -> None:
        """@brief 守卫消息身份 / Guard the message identity.

        @return None / None.
        @raise TypeError scope 或消息 ID 类型非法时抛出 / Raised for an invalid scope or message-ID type.
        @raise ValueError 消息 ID 不为正时抛出 / Raised when the message ID is not positive.
        """

        if not isinstance(self.scope, GroupConversationScope):
            raise TypeError("scope must be a GroupConversationScope")
        _positive_integer(self.message_id, "message_id")

    @property
    def group_id(self) -> int:
        """@brief 返回群 ID / Return the group identifier.

        @return 非零群 ID / Non-zero group identifier.
        """

        return self.scope.group_id

    @property
    def message_thread_id(self) -> int | None:
        """@brief 返回可选 Topic ID / Return the optional topic identifier.

        @return Topic ID 或 None / Topic identifier or None.
        """

        return self.scope.message_thread_id


@dataclass(frozen=True, slots=True)
class GroupMessageObservation:
    """@brief durable ingress 观察到的一次群消息修订 / One group-message revision observed by durable ingress.

    @param source_update_id provider Update 的单调幂等序号 / Monotonic idempotency sequence of the provider update.
    @param identity 群消息完整身份 / Complete group-message identity.
    @param sender_user_id 可选发送者 ID / Optional sender identifier.
    @param kind 内容种类 / Content kind.
    @param content 面向上下文的规范文本 / Canonical context text.
    @param created_at 原消息时间 / Original message time.
    @param updated_at 最近编辑或原消息时间 / Latest edit or original message time.
    @param edited 是否来自编辑事件 / Whether this revision came from an edit event.
    @param sender_name 可选发送者显示名 / Optional sender display name.
    @param sender_username 可选 provider username / Optional provider username.
    @note 非文本消息只能携带领域固定的惰性标记，不能把 caption、文件名、emoji 或
        provider capability 泄漏给模型。/ Non-text messages may carry only domain-defined
        inert markers; captions, filenames, emoji, and provider capabilities cannot leak to the model.
    """

    source_update_id: int
    identity: GroupMessageIdentity
    sender_user_id: int | None
    kind: GroupMessageKind
    content: str
    created_at: datetime
    updated_at: datetime
    edited: bool
    sender_name: str | None = None
    sender_username: str | None = None

    def __post_init__(self) -> None:
        """@brief 校验并规范化一次消息修订 / Validate and normalize one message revision.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised for an invalid field type.
        @raise ValueError 字段组合违反领域规则时抛出 / Raised when fields violate domain rules.
        """

        _nonnegative_integer(self.source_update_id, "source_update_id")
        if not isinstance(self.identity, GroupMessageIdentity):
            raise TypeError("identity must be a GroupMessageIdentity")
        if self.sender_user_id is not None:
            _positive_integer(self.sender_user_id, "sender_user_id")
        if not isinstance(self.kind, GroupMessageKind):
            raise TypeError("kind must be a GroupMessageKind")
        content = _content(self.content)
        if self.kind.is_attachment and content != GROUP_ATTACHMENT_MARKER:
            raise ValueError("group attachments must use the inert attachment marker")
        if (
            self.kind is GroupMessageKind.OTHER
            and content != GROUP_SERVICE_MESSAGE_MARKER
        ):
            raise ValueError("service messages must use the stable service marker")
        created_at = _utc(self.created_at, "created_at")
        updated_at = _utc(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise ValueError("updated_at cannot precede created_at")
        if not isinstance(self.edited, bool):
            raise TypeError("edited must be a bool")
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(
            self, "sender_name", _optional_name(self.sender_name, 256, "sender_name")
        )
        object.__setattr__(
            self,
            "sender_username",
            _optional_name(self.sender_username, 64, "sender_username"),
        )

    @property
    def group_id(self) -> int:
        """@brief 返回所属群 ID / Return the owning group identifier.

        @return 非零群 ID / Non-zero group identifier.
        """

        return self.identity.group_id

    @property
    def message_id(self) -> int:
        """@brief 返回群内消息 ID / Return the message identifier within the group.

        @return 正消息 ID / Positive message identifier.
        """

        return self.identity.message_id

    @property
    def message_thread_id(self) -> int | None:
        """@brief 返回可选 Topic ID / Return the optional topic identifier.

        @return Topic ID 或 None / Topic identifier or None.
        """

        return self.identity.message_thread_id

    def supersedes(self, current: GroupMessageObservation) -> bool:
        """@brief 判断本修订能否推进当前规范消息 / Whether this revision may advance the current canonical message.

        @param current 当前规范修订 / Current canonical revision.
        @return 同一消息且 source Update 更大时为 True / True for the same message and a greater source update.
        @raise ValueError 两个修订不属于同一消息时抛出 / Raised when the revisions identify different messages.
        @note 相等序号是 replay，较小序号是乱序陈旧事件；两者都不能覆盖当前修订。/
        An equal sequence is a replay and a lower sequence is a stale out-of-order event;
        neither may overwrite the current revision.
        """

        if not isinstance(current, GroupMessageObservation):
            raise TypeError("current must be a GroupMessageObservation")
        if self.identity != current.identity:
            raise ValueError("cannot compare revisions of different group messages")
        return self.source_update_id > current.source_update_id


@dataclass(frozen=True, slots=True)
class GroupMessage:
    """@brief Assistant 可读取的规范群消息快照 / Canonical group-message snapshot readable by the Assistant.

    @param identity 群消息完整身份 / Complete group-message identity.
    @param sender_user_id 可选发送者 ID / Optional sender identifier.
    @param sender_name 可选已注册名称 / Optional registered sender name.
    @param kind 内容种类 / Content kind.
    @param content 已解码持久文本 / Decoded persisted content.
    @param created_at 原消息时间 / Original message time.
    @param edited 是否已经编辑 / Whether the message was edited.
    @param sender_username 可选 provider username / Optional provider username.
    @note 读取快照允许历史 non-text 文本，以便无损读取迁移前的 base64 数据；新观察值
        则由 ``GroupMessageObservation`` 执行更严格的惰性标记规则。/ Read snapshots admit
        historical non-text content for lossless legacy base64 reads; new observations enforce
        the stricter inert-marker rule in ``GroupMessageObservation``.
    """

    identity: GroupMessageIdentity
    sender_user_id: int | None
    sender_name: str | None
    kind: GroupMessageKind
    content: str
    created_at: datetime
    edited: bool
    sender_username: str | None = None

    def __post_init__(self) -> None:
        """@brief 校验并规范化读取快照 / Validate and normalize a read snapshot.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised for an invalid field type.
        @raise ValueError 字段超出业务范围时抛出 / Raised when a field is outside its business range.
        """

        if not isinstance(self.identity, GroupMessageIdentity):
            raise TypeError("identity must be a GroupMessageIdentity")
        if self.sender_user_id is not None:
            _positive_integer(self.sender_user_id, "sender_user_id")
        if not isinstance(self.kind, GroupMessageKind):
            raise TypeError("kind must be a GroupMessageKind")
        object.__setattr__(self, "content", _content(self.content))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if not isinstance(self.edited, bool):
            raise TypeError("edited must be a bool")
        object.__setattr__(
            self, "sender_name", _optional_name(self.sender_name, 256, "sender_name")
        )
        object.__setattr__(
            self,
            "sender_username",
            _optional_name(self.sender_username, 64, "sender_username"),
        )

    @property
    def group_id(self) -> int:
        """@brief 返回所属群 ID / Return the owning group identifier.

        @return 非零群 ID / Non-zero group identifier.
        """

        return self.identity.group_id

    @property
    def message_id(self) -> int:
        """@brief 返回群内消息 ID / Return the message identifier within the group.

        @return 正消息 ID / Positive message identifier.
        """

        return self.identity.message_id

    @property
    def message_thread_id(self) -> int | None:
        """@brief 返回可选 Topic ID / Return the optional topic identifier.

        @return Topic ID 或 None / Topic identifier or None.
        """

        return self.identity.message_thread_id


@dataclass(frozen=True, slots=True)
class GroupContextQuery:
    """@brief 读取某 Topic 中当前消息之前的有界窗口 / Bounded query before a current message in one topic.

    @param scope 目标群或 Topic / Target group or topic.
    @param before_message_id 可选排他消息上界 / Optional exclusive message-ID bound.
    @param limit 请求条数 / Requested row count.
    """

    scope: GroupConversationScope
    """@brief 只允许读取的群或 Topic / Sole group or topic that may be read."""

    before_message_id: int | None
    """@brief 排他消息 ID 上界 / Exclusive message-ID upper bound."""

    limit: int = DEFAULT_GROUP_CONTEXT_MESSAGES
    """@brief 一到硬上限之间的条数 / Row count between one and the hard limit."""

    def __post_init__(self) -> None:
        """@brief 守卫 Topic 隔离与有界读取 / Guard topic isolation and bounded reads.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised for an invalid field type.
        @raise ValueError 边界或条数非法时抛出 / Raised for an invalid boundary or count.
        """

        if not isinstance(self.scope, GroupConversationScope):
            raise TypeError("scope must be a GroupConversationScope")
        if self.before_message_id is not None:
            _positive_integer(self.before_message_id, "before_message_id")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("limit must be an integer")
        if not 1 <= self.limit <= MAX_GROUP_CONTEXT_MESSAGES:
            raise ValueError(
                f"limit must be between 1 and {MAX_GROUP_CONTEXT_MESSAGES}"
            )

    def includes(self, message: GroupMessage) -> bool:
        """@brief 判断消息是否属于查询的身份边界 / Whether a message belongs to this query's identity boundary.

        @param message 待读取消息 / Candidate message.
        @return 同一 Topic 且低于排他上界时为 True / True in the same topic and below the exclusive bound.
        """

        if not isinstance(message, GroupMessage):
            raise TypeError("message must be a GroupMessage")
        return message.identity.scope == self.scope and (
            self.before_message_id is None
            or message.message_id < self.before_message_id
        )


def _nonnegative_integer(value: int, field: str) -> None:
    """@brief 校验非负严格整数 / Validate a non-negative strict integer.

    @param value 候选值 / Candidate value.
    @param field 错误字段名 / Field name for errors.
    @return None / None.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")


def _positive_integer(value: int, field: str) -> None:
    """@brief 校验正严格整数 / Validate a positive strict integer.

    @param value 候选值 / Candidate value.
    @param field 错误字段名 / Field name for errors.
    @return None / None.
    """

    _nonnegative_integer(value, field)
    if value == 0:
        raise ValueError(f"{field} must be positive")


def _nonzero_integer(value: int, field: str) -> None:
    """@brief 校验非零严格整数 / Validate a non-zero strict integer.

    @param value 候选值 / Candidate value.
    @param field 错误字段名 / Field name for errors.
    @return None / None.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value == 0:
        raise ValueError(f"{field} must be non-zero")


def _content(value: str) -> str:
    """@brief 校验规范内容预算 / Validate the canonical content budget.

    @param value 消息内容 / Message content.
    @return 原内容 / Original content.
    """

    if not isinstance(value, str):
        raise TypeError("content must be a string")
    if len(value) > MAX_GROUP_MESSAGE_CHARACTERS:
        raise ValueError(
            f"group-message content cannot exceed {MAX_GROUP_MESSAGE_CHARACTERS} characters"
        )
    return value


def _optional_name(value: str | None, maximum: int, field: str) -> str | None:
    """@brief 规范化可选人类名称 / Normalize an optional human name.

    @param value 可选原始名称 / Optional raw name.
    @param maximum 最大字符数 / Maximum character count.
    @param field 错误字段名 / Field name for errors.
    @return 去空白名称或 None / Stripped name or None.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string when present")
    normalized = value.strip()
    if not 1 <= len(normalized) <= maximum:
        raise ValueError(f"{field} must contain 1-{maximum} characters")
    return normalized


def _utc(value: datetime, field: str) -> datetime:
    """@brief 规范化 aware UTC 时间 / Normalize an aware timestamp to UTC.

    @param value 候选时间 / Candidate timestamp.
    @param field 错误字段名 / Field name for errors.
    @return UTC aware 时间 / UTC-aware timestamp.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "DEFAULT_GROUP_CONTEXT_MESSAGES",
    "GROUP_ATTACHMENT_MARKER",
    "GROUP_SERVICE_MESSAGE_MARKER",
    "MAX_GROUP_CONTEXT_MESSAGES",
    "GroupContextQuery",
    "GroupConversationScope",
    "GroupMessage",
    "GroupMessageIdentity",
    "GroupMessageKind",
    "GroupMessageObservation",
]
