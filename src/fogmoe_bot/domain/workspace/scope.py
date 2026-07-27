"""@brief 隔离 Workspace 的强类型归属范围 / Strongly typed ownership scopes for isolated Workspaces.

一个 Workspace 只归属于一个私聊用户或整个 Telegram 群聊；它刻意不接受
``ConversationId``、消息 ID 或 topic ID。这样 topic 的会话路由变化不能悄悄派生出第二个
宿主运行时。/ A Workspace belongs only to one private user or one whole Telegram group;
it deliberately does not accept a ``ConversationId``, message ID, or topic ID. Consequently,
topic-level conversation routing cannot silently derive a second host runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RuntimeScopeKind(StrEnum):
    """@brief Workspace 运行时归属类别 / Workspace runtime ownership kind."""

    PERSONAL = "personal"
    """@brief 私聊用户专属运行时 / Runtime private to one user."""

    GROUP = "group"
    """@brief 整个群聊共享的运行时 / Runtime shared by one whole group."""


@dataclass(frozen=True, slots=True, order=True)
class PersonalRuntimeScope:
    """@brief 个人 Workspace 的稳定归属 / Stable ownership of a personal Workspace.

    @param user_id 正的、已认证 Telegram 用户标识 / Positive authenticated Telegram user identifier.
    @note 该类型不是通用会话键，不能以 topic、私聊 chat ID 或 ``ConversationId`` 代替。
        This type is not a generic conversation key and cannot be substituted by a topic,
        private-chat ID, or ``ConversationId``.
    """

    user_id: int
    """@brief Workspace 所属用户标识 / Workspace-owning user identifier."""

    def __post_init__(self) -> None:
        """@brief 验证个人运行时范围 / Validate the personal runtime scope.

        @return None / None.
        @raise TypeError 用户标识不是严格整数时抛出 / Raised when the user identifier is not a strict integer.
        @raise ValueError 用户标识不为正时抛出 / Raised when the user identifier is not positive.
        """

        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int):
            raise TypeError("Personal runtime user ID must be an integer")
        if self.user_id <= 0:
            raise ValueError("Personal runtime user ID must be positive")

    @property
    def kind(self) -> RuntimeScopeKind:
        """@brief 返回固定的个人范围类别 / Return the fixed personal scope kind.

        @return ``PERSONAL`` / ``PERSONAL``.
        """

        return RuntimeScopeKind.PERSONAL

    @property
    def stable_id(self) -> int:
        """@brief 返回持久化的范围数字标识 / Return the persisted numeric scope identifier.

        @return 正的用户标识 / Positive user identifier.
        """

        return self.user_id


@dataclass(frozen=True, slots=True, order=True)
class GroupRuntimeScope:
    """@brief 群聊 Workspace 的稳定归属 / Stable ownership of a group Workspace.

    @param chat_id 非零 Telegram 群聊标识 / Non-zero Telegram group-chat identifier.
    @note 不包含 ``message_thread_id``；一个群的全部 topics 永远共用同一 Workspace。
        This type contains no ``message_thread_id``; all topics in one group always share one
        Workspace.
    """

    chat_id: int
    """@brief Workspace 所属群聊标识 / Workspace-owning group-chat identifier."""

    def __post_init__(self) -> None:
        """@brief 验证群聊运行时范围 / Validate the group runtime scope.

        @return None / None.
        @raise TypeError 群聊标识不是严格整数时抛出 / Raised when the group-chat identifier is not a strict integer.
        @raise ValueError 群聊标识为零时抛出 / Raised when the group-chat identifier is zero.
        @note Telegram 群组标识通常为负值，因而不能套用个人 ID 的正数规则。/
            Telegram group identifiers are commonly negative, so the personal-ID positivity rule
            does not apply.
        """

        if isinstance(self.chat_id, bool) or not isinstance(self.chat_id, int):
            raise TypeError("Group runtime chat ID must be an integer")
        if self.chat_id == 0:
            raise ValueError("Group runtime chat ID cannot be zero")

    @property
    def kind(self) -> RuntimeScopeKind:
        """@brief 返回固定的群聊范围类别 / Return the fixed group scope kind.

        @return ``GROUP`` / ``GROUP``.
        """

        return RuntimeScopeKind.GROUP

    @property
    def stable_id(self) -> int:
        """@brief 返回持久化的范围数字标识 / Return the persisted numeric scope identifier.

        @return 非零群聊标识 / Non-zero group-chat identifier.
        """

        return self.chat_id


type RuntimeScope = PersonalRuntimeScope | GroupRuntimeScope
"""@brief 全穷尽的 Workspace 归属范围 / Exhaustive Workspace ownership scope."""


def runtime_scope_parts(scope: RuntimeScope) -> tuple[RuntimeScopeKind, int]:
    """@brief 展开范围为数据库唯一键组成部分 / Expand a scope into database-unique-key parts.

    @param scope 强类型个人或群聊范围 / Strongly typed personal or group scope.
    @return ``(kind, stable_id)`` 持久化键 / Persistable ``(kind, stable_id)`` key.
    @raise TypeError 输入不是已授权的 Workspace scope 时抛出 /
        Raised when the input is not an authorized Workspace scope.
    @note 显式运行时检查防止 Python 的动态调用者把 ``ConversationId`` 或 topic 对象传入。
        The explicit runtime check prevents dynamic Python callers from passing a
        ``ConversationId`` or topic object.
    """

    if not isinstance(scope, PersonalRuntimeScope | GroupRuntimeScope):
        raise TypeError("Workspace runtime scope must be personal or group")
    return scope.kind, scope.stable_id


__all__ = [
    "GroupRuntimeScope",
    "PersonalRuntimeScope",
    "RuntimeScope",
    "RuntimeScopeKind",
    "runtime_scope_parts",
]
