"""@brief Admin 公告身份与不可变投递内容 / Admin announcement identity and immutable dispatch content."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

_ANNOUNCEMENT_ID_NAMESPACE = UUID("6bfaf3fd-aaf4-53f5-b789-0db71fe8b9ef")
"""@brief 幂等键到公告 ID 的 UUIDv5 命名空间 / UUIDv5 namespace mapping idempotency keys to announcement IDs."""


@dataclass(frozen=True, slots=True, order=True)
class AnnouncementId:
    """@brief 持久化公告标识符 / Durable announcement identifier.

    @param value 不透明 UUID / Opaque UUID value.
    """

    value: UUID
    """@brief 不透明 UUID / Opaque UUID."""

    def __post_init__(self) -> None:
        """@brief 验证 UUID 类型 / Validate the UUID type.

        @return None / None.
        @raise TypeError value 不是 UUID 时抛出 / Raised when value is not a UUID.
        """

        if not isinstance(self.value, UUID):
            raise TypeError("Announcement ID must contain a UUID")

    @classmethod
    def for_idempotency_key(cls, idempotency_key: str) -> AnnouncementId:
        """@brief 从来源幂等键推导稳定 ID / Derive a stable ID from a source idempotency key.

        @param idempotency_key 规范来源幂等键 / Canonical source idempotency key.
        @return 确定性 UUIDv5 ID / Deterministic UUIDv5 identifier.
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


@dataclass(frozen=True, slots=True)
class AnnouncementDeliveryCounts:
    """@brief 公告受众与终态投递计数 / Announcement audience and terminal-delivery counts."""

    recipients: int
    """@brief 受众快照数 / Audience-snapshot count."""

    delivered: int
    """@brief 成功投递数 / Delivered count."""

    failed: int
    """@brief 最终失败数 / Finally failed count."""

    def __post_init__(self) -> None:
        """@brief 验证计数矩阵 / Validate the count matrix.

        @return None / None.
        """

        values = (self.recipients, self.delivered, self.failed)
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise TypeError("Announcement delivery counts must be integers")
        if any(value < 0 for value in values):
            raise ValueError("Announcement delivery counts cannot be negative")
        if self.delivered + self.failed > self.recipients:
            raise ValueError(
                "Terminal announcement counts exceed the audience snapshot"
            )


@dataclass(frozen=True, slots=True)
class AnnouncementDispatchContent:
    """@brief claim 时冻结的公告出站内容 / Announcement dispatch content frozen at claim time."""

    body: str
    """@brief 公告正文 / Announcement body."""

    counts: AnnouncementDeliveryCounts
    """@brief 受众与终态计数 / Audience and terminal counts."""

    announcement_created_at: datetime
    """@brief 公告创建时刻 / Announcement creation instant."""

    def __post_init__(self) -> None:
        """@brief 验证并规范不可变内容 / Validate and normalize immutable content.

        @return None / None.
        """

        if not isinstance(self.body, str):
            raise TypeError("Announcement body must be a string")
        if not self.body.strip() or len(self.body) > 3500:
            raise ValueError("Announcement body must contain 1-3500 characters")
        if not isinstance(self.counts, AnnouncementDeliveryCounts):
            raise TypeError("Announcement dispatch requires delivery counts")
        object.__setattr__(
            self,
            "announcement_created_at",
            _utc(self.announcement_created_at),
        )


def _utc(value: datetime) -> datetime:
    """@brief 规范 aware UTC 时间 / Normalize an aware UTC instant.

    @param value 输入时间 / Input instant.
    @return UTC aware 时间 / UTC-aware instant.
    """

    if type(value) is not datetime:
        raise TypeError("Announcement timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Announcement timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "AnnouncementDeliveryCounts",
    "AnnouncementDispatchContent",
    "AnnouncementId",
]
