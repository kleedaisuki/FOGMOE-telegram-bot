"""@brief Admin 公告扩展领域模型 / Domain models for administrative announcement expansion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid5

_ANNOUNCEMENT_ID_NAMESPACE = UUID("6bfaf3fd-aaf4-53f5-b789-0db71fe8b9ef")
"""@brief 幂等键到公告 ID 的 UUIDv5 命名空间 / UUIDv5 namespace mapping idempotency keys to announcement IDs."""


@dataclass(frozen=True, slots=True, order=True)
class AnnouncementId:
    """@brief 持久化公告标识符 / Durable announcement identifier.

    @param value 不透明 UUID / Opaque UUID value.
    """

    value: UUID

    @classmethod
    def for_idempotency_key(cls, idempotency_key: str) -> AnnouncementId:
        """@brief 从来源幂等键推导稳定 ID / Derive a stable ID from a source idempotency key.

        @param idempotency_key 规范来源幂等键 / Canonical source idempotency key.
        @return 确定性 UUIDv5 ID / Deterministic UUIDv5 identifier.
        @raise ValueError 键为空或过长 / The key is blank or too long.
        """

        key = idempotency_key.strip()
        if not key or len(key) > 255:
            raise ValueError(
                "Announcement idempotency key must contain 1-255 characters"
            )
        return cls(uuid5(_ANNOUNCEMENT_ID_NAMESPACE, key))

    @classmethod
    def parse(cls, value: UUID | str) -> AnnouncementId:
        """@brief 解析持久化 ID / Parse a persisted identifier.

        @param value UUID 或规范文本 / UUID or canonical text.
        @return 公告 ID / Announcement identifier.
        """

        return cls(value if isinstance(value, UUID) else UUID(str(value)))

    def __str__(self) -> str:
        """@brief 返回规范 UUID 文本 / Return canonical UUID text.

        @return UUID 文本 / UUID text.
        """

        return str(self.value)


class AnnouncementRecipientKind(StrEnum):
    """@brief 公告出站回执类型 / Kinds of announcement outbound receipts."""

    USER = "user"
    """@brief Telegram 用户公告 / Telegram user announcement."""

    GROUP = "group"
    """@brief Telegram 群组公告 / Telegram group announcement."""

    COMPLETION = "completion"
    """@brief 投递终态后的管理员回执 / Administrator receipt after delivery reaches terminal states."""


@dataclass(frozen=True, slots=True)
class AnnouncementRecipientClaim:
    """@brief 带 fencing token 的公告出站领取 / Announcement outbound claim carrying a fencing token.

    @param announcement_id 公告 ID / Announcement ID.
    @param recipient_kind 出站类型 / Outbound-recipient kind.
    @param chat_id Telegram chat ID / Telegram chat identifier.
    @param message_thread_id 可选 topic ID / Optional topic identifier.
    @param reply_to_message_id 可选回复消息 ID / Optional replied-to message identifier.
    @param body 不可变公告本文 / Immutable announcement body.
    @param recipient_count 受众快照总数 / Total snapshotted audience count.
    @param delivered_count 已投递数 / Delivered audience count.
    @param failed_count 最终失败数 / Finally failed audience count.
    @param claim_token 防陈旧 worker 令牌 / Stale-worker fencing token.
    @param attempt_count 当前尝试序号 / Current attempt number.
    @param announcement_created_at 公告创建时间 / Announcement creation instant.
    @param claimed_at 领取时间 / Claim instant.
    @param lease_expires_at 租约到期时间 / Lease expiry instant.
    """

    announcement_id: AnnouncementId
    recipient_kind: AnnouncementRecipientKind
    chat_id: int
    message_thread_id: int | None
    reply_to_message_id: int | None
    body: str
    recipient_count: int
    delivered_count: int
    failed_count: int
    claim_token: UUID
    attempt_count: int
    announcement_created_at: datetime
    claimed_at: datetime
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验领取不变量并规范 UTC / Validate claim invariants and normalize UTC.

        @return None / None.
        @raise ValueError 地址、计数或时间非法 / Invalid address, counters, or timestamps.
        """

        if self.chat_id == 0:
            raise ValueError("Announcement recipient chat ID cannot be zero")
        if self.message_thread_id is not None and self.message_thread_id < 1:
            raise ValueError("Announcement message thread ID must be positive")
        if self.reply_to_message_id is not None and self.reply_to_message_id < 1:
            raise ValueError("Announcement reply message ID must be positive")
        if not self.body.strip() or len(self.body) > 3500:
            raise ValueError("Announcement body must contain 1-3500 characters")
        counters = (
            self.recipient_count,
            self.delivered_count,
            self.failed_count,
        )
        if any(value < 0 for value in counters):
            raise ValueError("Announcement counters cannot be negative")
        if self.delivered_count + self.failed_count > self.recipient_count:
            raise ValueError(
                "Terminal announcement counts exceed the audience snapshot"
            )
        if self.attempt_count < 1:
            raise ValueError("Announcement claim attempt count must be positive")
        created_at = _utc(self.announcement_created_at)
        claimed_at = _utc(self.claimed_at)
        lease_expires_at = _utc(self.lease_expires_at)
        if claimed_at < created_at or lease_expires_at <= claimed_at:
            raise ValueError("Announcement claim timestamps are out of order")
        object.__setattr__(self, "announcement_created_at", created_at)
        object.__setattr__(self, "claimed_at", claimed_at)
        object.__setattr__(self, "lease_expires_at", lease_expires_at)


def _utc(value: datetime) -> datetime:
    """@brief 将 aware 时间规范为 UTC / Normalize an aware instant to UTC.

    @param value 输入时间 / Input instant.
    @return UTC aware 时间 / UTC-aware instant.
    @raise ValueError 输入为 naive datetime / The input is naive.
    """

    if value.tzinfo is None:
        raise ValueError("Announcement timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "AnnouncementId",
    "AnnouncementRecipientClaim",
    "AnnouncementRecipientKind",
]
