"""会话工作流身份值对象 / Conversation workflow identity value objects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self
from uuid import UUID, uuid4, uuid5

_TURN_ID_NAMESPACE = UUID("f4f93866-a3c2-54d4-995f-56a7bc73bf57")
"""@brief 单逻辑 Bot 的 Update→Turn UUIDv5 命名空间 / UUIDv5 namespace for Update-to-Turn mapping in one logical bot."""

_CONVERSATION_MESSAGE_ID_NAMESPACE = UUID("0e7ec52b-0b35-53ab-9dda-c71a6140fe1f")
"""@brief Turn 语义消息 UUIDv5 命名空间 / UUIDv5 namespace for semantic messages within a Turn."""

_OUTBOUND_MESSAGE_ID_NAMESPACE = UUID("1b753d09-b330-55c9-8354-3d47cf7c9999")
"""@brief Turn 语义出站 UUIDv5 命名空间 / UUIDv5 namespace for semantic outbound effects within a Turn."""

_INFERENCE_ACTIVITY_ID_NAMESPACE = UUID("0aa627d4-6ea0-5a5d-92bd-91aca8c09175")
"""@brief Turn→Inference Activity UUIDv5 命名空间 / UUIDv5 namespace for Turn-to-Inference-Activity mapping."""


@dataclass(frozen=True, slots=True, order=True)
class _UuidIdentifier:
    """@brief UUID 值对象基类 / Base UUID value object.

    @param value 不透明 UUID 值 / Opaque UUID value.
    @note 具体 ID 子类不可互换，从而由类型检查器阻止错接标识符 /
    Concrete ID subclasses are intentionally non-interchangeable so type checkers reject mixed identifiers.
    """

    value: UUID

    @classmethod
    def new(cls) -> Self:
        """@brief 创建随机标识符 / Create a random identifier.

        @return 当前具体类型的新标识符 / A new identifier of the concrete type.
        """

        return cls(uuid4())

    @classmethod
    def parse(cls, value: UUID | str) -> Self:
        """@brief 解析数据库或线上的 UUID / Parse a stored or wire UUID.

        @param value UUID 对象或规范文本 / UUID object or canonical text.
        @return 当前具体类型的标识符 / Identifier of the concrete type.
        @raise ValueError 文本不是合法 UUID 时抛出 / Raised when text is not a valid UUID.
        """

        return cls(value if isinstance(value, UUID) else UUID(str(value)))

    def __str__(self) -> str:
        """@brief 返回规范 UUID 文本 / Return canonical UUID text.

        @return UUID 文本 / UUID text.
        """

        return str(self.value)


class TurnId(_UuidIdentifier):
    """@brief 会话回合 ID / Conversation-turn identifier."""

    __slots__ = ()

    @classmethod
    def for_source(cls, source: TurnSource) -> Self:
        """@brief 为来源事件生成确定性回合 ID / Derive a deterministic turn ID from its source event.

        @param source 规范来源 identity / Canonical source identity.
        @return 稳定 UUIDv5 回合 ID / Stable UUIDv5 turn ID.
        @note 同一来源的崩溃重放必须得到同一 ID；来源 kind 防止 Telegram 与调度键碰撞。/
        Crash replay of the same source must produce the same ID; the source kind prevents
        collisions between Telegram and scheduling keys.
        """

        identity = (
            source.key
            if source.kind == TELEGRAM_UPDATE_SOURCE_KIND
            else f"{source.kind}:{source.key}"
        )
        return cls(uuid5(_TURN_ID_NAMESPACE, identity))


class ConversationMessageId(_UuidIdentifier):
    """@brief 会话消息 ID / Conversation-message identifier."""

    __slots__ = ()

    @classmethod
    def for_turn(cls, turn_id: TurnId, semantic_key: str) -> Self:
        """@brief 为 Turn 内语义消息生成确定性 ID / Derive a deterministic ID for a semantic message within a Turn.

        @param turn_id 所属回合 / Owning turn.
        @param semantic_key 稳定消息语义键 / Stable message semantic key.
        @return 稳定 UUIDv5 消息 ID / Stable UUIDv5 message ID.
        @raise ValueError 语义键为空或过长时抛出 / Raised when the semantic key is empty or too long.
        """

        key = normalize_idempotency_key(semantic_key)
        return cls(
            uuid5(
                _CONVERSATION_MESSAGE_ID_NAMESPACE,
                f"{turn_id}:{key}",
            )
        )


class OutboundMessageId(_UuidIdentifier):
    """@brief 出站消息 ID / Outbound-message identifier."""

    __slots__ = ()

    @classmethod
    def for_turn(cls, turn_id: TurnId, semantic_key: str) -> Self:
        """@brief 为 Turn 内语义副作用生成确定性 ID / Derive a deterministic ID for a semantic effect within a Turn.

        @param turn_id 所属回合 / Owning turn.
        @param semantic_key 稳定副作用语义键 / Stable effect semantic key.
        @return 稳定 UUIDv5 出站 ID / Stable UUIDv5 outbound ID.
        @raise ValueError 语义键为空或过长时抛出 / Raised when the semantic key is empty or too long.
        """

        key = normalize_idempotency_key(semantic_key)
        return cls(
            uuid5(
                _OUTBOUND_MESSAGE_ID_NAMESPACE,
                f"{turn_id}:{key}",
            )
        )

    @classmethod
    def for_conversation(
        cls,
        conversation_id: ConversationId,
        idempotency_key: str,
    ) -> Self:
        """@brief 为无 Turn 副作用生成确定性 ID / Derive a deterministic ID for a standalone effect.

        @param conversation_id 副作用幂等作用域 / Effect idempotency scope.
        @param idempotency_key 会话内稳定幂等键 / Stable conversation-scoped idempotency key.
        @return 稳定 UUIDv5 出站 ID / Stable UUIDv5 outbound ID.
        @note ID 使用与数据库唯一键相同的 ``conversation + idempotency_key`` 语义边界 / The ID uses the same ``conversation + idempotency_key`` semantic boundary as the database uniqueness constraint.
        """

        key = normalize_idempotency_key(idempotency_key)
        return cls(
            uuid5(
                _OUTBOUND_MESSAGE_ID_NAMESPACE,
                f"standalone:{conversation_id}:{key}",
            )
        )


class InferenceActivityId(_UuidIdentifier):
    """@brief 可恢复推理活动 ID / Recoverable inference-activity identifier."""

    __slots__ = ()

    @classmethod
    def for_turn(cls, turn_id: TurnId) -> Self:
        """@brief 为 primary Turn 推导稳定活动 ID / Derive a stable activity ID for a primary Turn.

        @param turn_id 所属回合 / Owning turn.
        @return 稳定 UUIDv5 活动 ID / Stable UUIDv5 activity ID.
        @note 每个 Turn 只允许一个 primary inference activity；确定性 ID 使 acceptance
        的崩溃重放收敛到同一意图。/ Each Turn has exactly one primary inference activity;
        the deterministic ID makes acceptance crash replay converge on one intent.
        """

        return cls(uuid5(_INFERENCE_ACTIVITY_ID_NAMESPACE, str(turn_id)))


class LeaseToken(_UuidIdentifier):
    """@brief 防陈旧 worker 的租约 fencing token / Lease fencing token for stale workers."""

    __slots__ = ()


@dataclass(frozen=True, slots=True, order=True)
class ConversationId:
    """@brief 显式会话聚合键 / Explicit conversation aggregate key.

    @param value 由入口适配器规范化的稳定键 / Stable key normalized by the ingress adapter.
    """

    value: str

    def __post_init__(self) -> None:
        """@brief 校验会话键 / Validate the conversation key.

        @return None / None.
        @raise ValueError 键为空或过长时抛出 / Raised when the key is empty or too long.
        """

        normalized = self.value.strip()
        if not normalized:
            raise ValueError("Conversation ID cannot be empty")
        if len(normalized) > 512:
            raise ValueError("Conversation ID cannot exceed 512 characters")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        """@brief 返回持久化键 / Return the persistable key.

        @return 会话键文本 / Conversation-key text.
        """

        return self.value


@dataclass(frozen=True, slots=True, order=True)
class UpdateId:
    """@brief Telegram Update 幂等 ID / Telegram Update idempotency identifier.

    @param value Telegram 单调递增 update_id / Telegram monotonically increasing update_id.
    @note 当前部署不变量是一套数据库服务一个逻辑 Bot；同 token 的多进程共享该 ID 空间 /
    The deployment invariant is one logical bot per database; processes consuming the same token share this ID space.
    """

    value: int

    def __post_init__(self) -> None:
        """@brief 校验 Update ID / Validate the Update ID.

        @return None / None.
        @raise ValueError ID 为负数时抛出 / Raised for a negative identifier.
        """

        if self.value < 0:
            raise ValueError("Update ID cannot be negative")

    def __int__(self) -> int:
        """@brief 返回数据库整数 / Return the database integer.

        @return Update ID 整数 / Update-ID integer.
        """

        return self.value


TELEGRAM_UPDATE_SOURCE_KIND = "telegram.update"
"""@brief Telegram Update 的 Turn 来源 kind / Turn-source kind for Telegram updates."""


@dataclass(frozen=True, slots=True, order=True)
class TurnSource:
    """@brief Conversation Turn 的稳定来源 identity / Stable source identity for a Conversation Turn.

    @param kind 来源命名空间，如 ``telegram.update`` 或 ``schedule.prompt`` /
        Source namespace, such as ``telegram.update`` or ``schedule.prompt``.
    @param key kind 内唯一的幂等键 / Idempotency key unique within the kind.
    @param update_id 仅 Telegram 来源携带的外键 / Foreign key carried only by Telegram sources.
    """

    kind: str
    key: str
    update_id: UpdateId | None = None

    def __post_init__(self) -> None:
        """@brief 校验来源命名空间、键与 Telegram 外键一致 / Validate namespace, key, and Telegram-FK consistency.

        @return None / None.
        @raise ValueError kind/key 非法或 Telegram identity 不一致 / Invalid kind/key or inconsistent Telegram identity.
        """

        kind = self.kind.strip().lower()
        key = self.key.strip()
        if re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", kind) is None:
            raise ValueError("Turn source kind must be a stable lowercase namespace")
        if not key or len(key) > 255:
            raise ValueError("Turn source key must contain 1-255 characters")
        if kind == TELEGRAM_UPDATE_SOURCE_KIND:
            if self.update_id is None or key != str(int(self.update_id)):
                raise ValueError(
                    "Telegram Turn source key must equal its update identifier"
                )
        elif self.update_id is not None:
            raise ValueError("Only a Telegram Turn source may carry update_id")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "key", key)

    @classmethod
    def telegram(cls, update_id: UpdateId) -> Self:
        """@brief 构造 Telegram Update 来源 / Build a Telegram Update source.

        @param update_id 单逻辑 Bot 内 Update ID / Update ID within one logical Bot.
        @return 规范 Telegram source / Canonical Telegram source.
        """

        return cls(
            kind=TELEGRAM_UPDATE_SOURCE_KIND,
            key=str(int(update_id)),
            update_id=update_id,
        )

    @classmethod
    def external(cls, kind: str, key: str) -> Self:
        """@brief 构造非 Telegram durable 来源 / Build a non-Telegram durable source.

        @param kind 来源命名空间 / Source namespace.
        @param key kind 内幂等键 / Idempotency key within the namespace.
        @return 无 Telegram FK 的规范 source / Canonical source without a Telegram FK.
        """

        return cls(kind=kind, key=key)


@dataclass(frozen=True, slots=True, order=True)
class MessageSequence:
    """@brief 会话内单调消息序号 / Monotonic sequence within one conversation.

    @param value 从 1 开始的消息序号 / One-based message sequence.
    """

    value: int

    def __post_init__(self) -> None:
        """@brief 校验消息序号 / Validate the message sequence.

        @return None / None.
        @raise ValueError 序号小于 1 时抛出 / Raised when the sequence is below one.
        """

        if self.value < 1:
            raise ValueError("Message sequence must be at least one")

    def __int__(self) -> int:
        """@brief 返回数据库整数 / Return the database integer.

        @return 消息序号整数 / Message-sequence integer.
        """

        return self.value


@dataclass(frozen=True, slots=True, order=True)
class DeliveryStreamId:
    """@brief 外部投递顺序流 ID / External-delivery ordering-stream identifier.

    @param value 编码 connector、bot、chat 与 thread 的稳定键 /
    Stable key encoding connector, bot, chat, and thread.
    """

    value: str

    def __post_init__(self) -> None:
        """@brief 校验投递流 ID / Validate the delivery-stream ID.

        @return None / None.
        @raise ValueError ID 为空或过长时抛出 / Raised when the ID is empty or too long.
        """

        normalized = self.value.strip()
        if not normalized:
            raise ValueError("Delivery stream ID cannot be empty")
        if len(normalized) > 512:
            raise ValueError("Delivery stream ID cannot exceed 512 characters")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        """@brief 返回持久化键 / Return the persistable key.

        @return 投递流文本 / Delivery-stream text.
        """

        return self.value


def normalize_idempotency_key(value: str) -> str:
    """@brief 校验幂等键 / Validate an idempotency key.

    @param value 幂等键文本 / Idempotency-key text.
    @return 规范化键 / Normalized key.
    @raise ValueError 键为空或过长时抛出 / Raised when the key is empty or too long.
    """

    normalized = value.strip()
    if not normalized:
        raise ValueError("Idempotency key cannot be empty")
    if len(normalized) > 255:
        raise ValueError("Idempotency key cannot exceed 255 characters")
    return normalized
