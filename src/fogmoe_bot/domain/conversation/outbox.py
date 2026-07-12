"""Transactional outbox 领域模型 / Transactional-outbox domain models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .identity import (
    ConversationId,
    DeliveryStreamId,
    LeaseToken,
    MessageSequence,
    OutboundMessageId,
    TurnId,
    normalize_idempotency_key,
)
from .payloads import JsonObject
from .temporal import ensure_utc


@dataclass(frozen=True, slots=True)
class OutboundKind:
    """@brief 可扩展出站动作类型 / Extensible outbound-action kind.

    @param value 稳定持久化名称 / Stable persisted name.
    """

    value: str

    def __post_init__(self) -> None:
        """@brief 校验动作类型 / Validate the action kind.

        @return None / None.
        @raise ValueError 类型为空或过长时抛出 / Raised when the kind is empty or too long.
        """

        normalized = self.value.strip().lower()
        if not normalized:
            raise ValueError("Outbound kind cannot be empty")
        if len(normalized) > 100:
            raise ValueError("Outbound kind cannot exceed 100 characters")
        object.__setattr__(self, "value", normalized)


SEND_TELEGRAM_MESSAGE = OutboundKind("telegram.send_message")
"""@brief Telegram 发送消息动作 / Telegram send-message action."""

EDIT_TELEGRAM_MESSAGE = OutboundKind("telegram.edit_message")
"""@brief Telegram 编辑消息动作 / Telegram edit-message action."""

SEND_TELEGRAM_ARTIFACT = OutboundKind("telegram.send_artifact")
"""@brief Telegram durable artifact 投递动作 / Telegram durable-artifact delivery action."""

SEND_TELEGRAM_STICKER = OutboundKind("telegram.send_sticker")
"""@brief Telegram 贴纸投递动作 / Telegram sticker-delivery action."""

SEND_TELEGRAM_PHOTO = OutboundKind("telegram.send_photo")
"""@brief Telegram 远程图片投递动作 / Telegram remote-photo delivery action."""


class OutboundStatus(StrEnum):
    """@brief 出站消息投递状态 / Outbound-message delivery status."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    DELIVERED = "delivered"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class OutboundDraft:
    """@brief 尚未分配投递流序号的出站副作用 / Outbound effect awaiting a delivery-stream sequence.

    @param message_id 出站消息 ID / Outbound-message identifier.
    @param conversation_id 会话键 / Conversation key.
    @param turn_id 可选来源回合；独立副作用为 None / Optional source Turn; None for standalone effects.
    @param delivery_stream_id 外部有序投递流 / External ordered-delivery stream.
    @param kind 动作类型 / Action kind.
    @param payload 动作载荷 / Action payload.
    @param idempotency_key 会话内副作用幂等键 / Conversation-scoped effect idempotency key.
    @param created_at 创建时间 / Creation time.
    """

    message_id: OutboundMessageId
    conversation_id: ConversationId
    turn_id: TurnId | None
    delivery_stream_id: DeliveryStreamId
    kind: OutboundKind
    payload: JsonObject
    idempotency_key: str
    created_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验出站草稿 / Validate the outbound draft.

        @return None / None.
        @raise ValueError 幂等键或时间非法时抛出 / Raised for an invalid idempotency key or timestamp.
        """

        object.__setattr__(
            self, "idempotency_key", normalize_idempotency_key(self.idempotency_key)
        )
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """@brief 已排序的事务发件箱消息 / Sequenced transactional-outbox message.

    @param draft 不可变出站副作用 / Immutable outbound effect.
    @param stream_sequence 投递流内单调序号 / Monotonic sequence within the delivery stream.
    @param status 投递状态 / Delivery status.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param attempt_count 投递领取次数 / Delivery claim count.
    @param next_attempt_at 下次可领取时间 / Next claimable time.
    @param updated_at 最近更新时间 / Most recent update time.
    @param delivered_at 成功投递时间 / Successful delivery time.
    @param external_message_id 外部系统返回的消息 ID / Message ID returned by the external system.
    @param last_error 最近错误 / Most recent error.
    """

    draft: OutboundDraft
    stream_sequence: MessageSequence
    status: OutboundStatus
    version: int
    attempt_count: int
    next_attempt_at: datetime | None
    updated_at: datetime
    delivered_at: datetime | None = None
    external_message_id: str | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        """@brief 校验出站消息不变量 / Validate outbound-message invariants.

        @return None / None.
        @raise ValueError 状态、版本、幂等键或时间非法时抛出 / Raised for invalid state, version, idempotency key, or timestamps.
        """

        if self.version < 0 or self.attempt_count < 0:
            raise ValueError("Outbound version and attempt count cannot be negative")
        updated_at = ensure_utc(self.updated_at)
        if updated_at < self.draft.created_at:
            raise ValueError("Outbound updated_at cannot precede created_at")
        next_attempt_at = (
            ensure_utc(self.next_attempt_at) if self.next_attempt_at else None
        )
        delivered_at = ensure_utc(self.delivered_at) if self.delivered_at else None
        if (
            self.status in {OutboundStatus.PENDING, OutboundStatus.RETRY_WAIT}
            and next_attempt_at is None
        ):
            raise ValueError("Claimable outbound messages require next_attempt_at")
        if self.status is OutboundStatus.DELIVERED and delivered_at is None:
            raise ValueError("Delivered outbound messages require delivered_at")
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "next_attempt_at", next_attempt_at)
        object.__setattr__(self, "delivered_at", delivered_at)

    @property
    def message_id(self) -> OutboundMessageId:
        """@brief 返回出站消息 ID / Return the outbound-message ID.

        @return 出站消息 ID / Outbound-message ID.
        """

        return self.draft.message_id

    @property
    def conversation_id(self) -> ConversationId:
        """@brief 返回来源会话 / Return the source conversation.

        @return 会话 ID / Conversation ID.
        """

        return self.draft.conversation_id

    @property
    def turn_id(self) -> TurnId | None:
        """@brief 返回可选来源回合 / Return the optional source Turn.

        @return 回合 ID，独立副作用为 None / Turn ID, or None for a standalone effect.
        """

        return self.draft.turn_id

    @property
    def delivery_stream_id(self) -> DeliveryStreamId:
        """@brief 返回外部投递流 / Return the external delivery stream.

        @return 投递流 ID / Delivery-stream ID.
        """

        return self.draft.delivery_stream_id

    @property
    def kind(self) -> OutboundKind:
        """@brief 返回动作类型 / Return the action kind.

        @return 动作类型 / Action kind.
        """

        return self.draft.kind

    @property
    def payload(self) -> JsonObject:
        """@brief 返回动作载荷 / Return the action payload.

        @return JSON 载荷 / JSON payload.
        """

        return self.draft.payload

    @property
    def idempotency_key(self) -> str:
        """@brief 返回副作用幂等键 / Return the effect idempotency key.

        @return 幂等键 / Idempotency key.
        """

        return self.draft.idempotency_key

    @property
    def created_at(self) -> datetime:
        """@brief 返回创建时间 / Return the creation time.

        @return UTC 创建时间 / UTC creation time.
        """

        return self.draft.created_at


@dataclass(frozen=True, slots=True)
class OutboundClaim:
    """@brief 带 fencing token 的 outbox 领取凭证 / Outbox claim carrying a fencing token.

    @param message 已进入 processing 的消息 / Message now in processing state.
    @param token 本次领取 token / Token for this claim.
    @param lease_expires_at 租约过期时间 / Lease expiration time.
    """

    message: OutboundMessage
    token: LeaseToken
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验领取凭证 / Validate the claim.

        @return None / None.
        @raise ValueError 消息不处于 processing 或租约无效时抛出 / Raised when the message is not processing or the lease is invalid.
        """

        lease_expires_at = ensure_utc(self.lease_expires_at)
        if self.message.status is not OutboundStatus.PROCESSING:
            raise ValueError("Outbound claims require a processing message")
        if lease_expires_at <= self.message.updated_at:
            raise ValueError("Outbound lease must expire after claim time")
        object.__setattr__(self, "lease_expires_at", lease_expires_at)


@dataclass(frozen=True, slots=True)
class OutboundEnqueueResult:
    """@brief 幂等 outbox 入队结果 / Idempotent outbox-enqueue result.

    @param message 数据库中的规范消息 / Canonical stored message.
    @param inserted 本次是否插入 / Whether this call inserted the row.
    """

    message: OutboundMessage
    inserted: bool
