"""@brief Admin durable recipient 状态与能力值 / State and capability values for durable Admin recipients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID, uuid4

from fogmoe_bot.domain.conversation.identity import OutboundMessageId


class AnnouncementRecipientKind(StrEnum):
    """@brief 公告出站回执类型 / Kinds of announcement outbound receipts."""

    USER = "user"
    GROUP = "group"
    COMPLETION = "completion"


class AnnouncementRecipientStatus(StrEnum):
    """@brief durable recipient 的穷尽持久化状态 / Exhaustive persisted recipient states."""

    BLOCKED = "blocked"
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    EXPANDED = "expanded"
    FAILED_FINAL = "failed_final"


@dataclass(frozen=True, slots=True, order=True)
class AnnouncementClaimToken:
    """@brief 防陈旧 worker 的公告领取令牌 / Announcement-claim token fencing stale workers."""

    value: UUID
    """@brief 非 nil fencing UUID / Non-nil fencing UUID."""

    def __post_init__(self) -> None:
        """@brief 拒绝非 UUID 与 nil UUID / Reject non-UUID and nil UUID values.

        @return None / None.
        """

        if not isinstance(self.value, UUID):
            raise TypeError("Announcement claim token must contain a UUID")
        if self.value.int == 0:
            raise ValueError("Announcement claim token cannot be the nil UUID")

    @classmethod
    def new(cls) -> AnnouncementClaimToken:
        """@brief 生成随机 claim token / Generate a random claim token.

        @return UUIDv4 token / UUIDv4 token.
        """

        return cls(uuid4())

    @classmethod
    def parse(cls, value: UUID | str) -> AnnouncementClaimToken:
        """@brief 解析持久化 token / Parse a persisted token.

        @param value UUID 或文本 / UUID or text.
        @return 强类型 token / Strongly typed token.
        """

        return cls(value if isinstance(value, UUID) else UUID(str(value)))

    def __str__(self) -> str:
        """@brief 返回规范 UUID 文本 / Return canonical UUID text.

        @return UUID 文本 / UUID text.
        """

        return str(self.value)


@dataclass(frozen=True, slots=True, order=True)
class AnnouncementFailureCategory:
    """@brief 有界且非空的失败类别 / Bounded non-empty failure category."""

    value: str
    """@brief 1 至 100 字符规范类别 / Canonical category containing 1-100 characters."""

    def __post_init__(self) -> None:
        """@brief 规范失败类别 / Normalize the failure category.

        @return None / None.
        """

        if not isinstance(self.value, str):
            raise TypeError("Announcement failure category must be a string")
        object.__setattr__(self, "value", self.value.strip()[:100] or "unknown")

    @classmethod
    def from_exception(cls, error: BaseException) -> AnnouncementFailureCategory:
        """@brief 从异常类型推导稳定类别 / Derive a stable category from an exception type.

        @param error 原异常 / Original exception.
        @return 失败类别 / Failure category.
        """

        return cls(type(error).__name__)

    def __str__(self) -> str:
        """@brief 返回类别文本 / Return category text.

        @return 规范类别 / Canonical category.
        """

        return self.value


@dataclass(frozen=True, slots=True)
class AnnouncementClaimCapability:
    """@brief token 与 lease 构成的领取能力 / Claim capability composed of a token and lease."""

    token: AnnouncementClaimToken
    """@brief fencing token / Fencing token."""

    claimed_at: datetime
    """@brief 领取时刻 / Claim instant."""

    lease_expires_at: datetime
    """@brief 恢复授权边界 / Recovery-authorization boundary."""

    def __post_init__(self) -> None:
        """@brief 验证 capability 时间与 token / Validate capability chronology and token.

        @return None / None.
        """

        if not isinstance(self.token, AnnouncementClaimToken):
            raise TypeError("Announcement capability requires a claim token")
        claimed = _utc(self.claimed_at)
        expires = _utc(self.lease_expires_at)
        if expires <= claimed:
            raise ValueError("Announcement claim lease must follow its claim")
        object.__setattr__(self, "claimed_at", claimed)
        object.__setattr__(self, "lease_expires_at", expires)


@dataclass(frozen=True, slots=True)
class BlockedAnnouncementRecipient:
    """@brief completion 回执尚未可领取 / Completion receipt not yet claimable."""

    status: ClassVar[AnnouncementRecipientStatus] = AnnouncementRecipientStatus.BLOCKED


@dataclass(frozen=True, slots=True)
class PendingAnnouncementRecipient:
    """@brief 首次领取等待态 / First-claim waiting state."""

    next_attempt_at: datetime
    """@brief 可领取时刻 / Claimable instant."""

    status: ClassVar[AnnouncementRecipientStatus] = AnnouncementRecipientStatus.PENDING

    def __post_init__(self) -> None:
        """@brief 规范可领取时刻 / Normalize the claimable instant.

        @return None / None.
        """

        object.__setattr__(self, "next_attempt_at", _utc(self.next_attempt_at))


@dataclass(frozen=True, slots=True)
class ProcessingAnnouncementRecipient:
    """@brief token-fenced 处理中状态 / Token-fenced processing state."""

    capability: AnnouncementClaimCapability
    """@brief 当前 token/lease capability / Current token-and-lease capability."""

    status: ClassVar[AnnouncementRecipientStatus] = AnnouncementRecipientStatus.PROCESSING

    def __post_init__(self) -> None:
        """@brief 验证 capability 类型 / Validate the capability type.

        @return None / None.
        """

        if not isinstance(self.capability, AnnouncementClaimCapability):
            raise TypeError("Processing recipient requires a claim capability")


@dataclass(frozen=True, slots=True)
class RetryWaitingAnnouncementRecipient:
    """@brief 携带失败原因的重试等待态 / Retry-wait state carrying a failure category."""

    next_attempt_at: datetime
    """@brief 下次可领取时刻 / Next claimable instant."""

    failure: AnnouncementFailureCategory
    """@brief 上次失败类别 / Previous failure category."""

    status: ClassVar[AnnouncementRecipientStatus] = AnnouncementRecipientStatus.RETRY_WAIT

    def __post_init__(self) -> None:
        """@brief 验证失败并规范时间 / Validate failure and normalize time.

        @return None / None.
        """

        if not isinstance(self.failure, AnnouncementFailureCategory):
            raise TypeError("Retry-wait recipient requires a failure")
        object.__setattr__(self, "next_attempt_at", _utc(self.next_attempt_at))


@dataclass(frozen=True, slots=True)
class ExpandedAnnouncementRecipient:
    """@brief 已扩展到 outbox 的终态 / Terminal state expanded into the outbox."""

    outbound_message_id: OutboundMessageId
    """@brief 确定性 outbox ID / Deterministic outbox ID."""

    expanded_at: datetime
    """@brief 扩展完成时刻 / Expansion-completion instant."""

    status: ClassVar[AnnouncementRecipientStatus] = AnnouncementRecipientStatus.EXPANDED

    def __post_init__(self) -> None:
        """@brief 验证 outbox ID 并规范时间 / Validate the outbox ID and normalize time.

        @return None / None.
        """

        if not isinstance(self.outbound_message_id, OutboundMessageId):
            raise TypeError("Expanded recipient requires an outbound ID")
        object.__setattr__(self, "expanded_at", _utc(self.expanded_at))


@dataclass(frozen=True, slots=True)
class FailedAnnouncementRecipient:
    """@brief 最终失败终态 / Finally failed terminal state."""

    failure: AnnouncementFailureCategory
    """@brief 最终失败类别 / Final failure category."""

    terminal_at: datetime
    """@brief 最终失败时刻 / Final-failure instant."""

    status: ClassVar[AnnouncementRecipientStatus] = AnnouncementRecipientStatus.FAILED_FINAL

    def __post_init__(self) -> None:
        """@brief 验证失败并规范时间 / Validate failure and normalize time.

        @return None / None.
        """

        if not isinstance(self.failure, AnnouncementFailureCategory):
            raise TypeError("Failed recipient requires a failure")
        object.__setattr__(self, "terminal_at", _utc(self.terminal_at))


type AnnouncementRecipientState = (
    BlockedAnnouncementRecipient
    | PendingAnnouncementRecipient
    | ProcessingAnnouncementRecipient
    | RetryWaitingAnnouncementRecipient
    | ExpandedAnnouncementRecipient
    | FailedAnnouncementRecipient
)
"""@brief durable recipient 的穷尽状态联合 / Exhaustive durable-recipient state union."""


def _utc(value: datetime) -> datetime:
    """@brief 规范 aware UTC 时间 / Normalize an aware UTC instant.

    @param value 待规范化时间 / Instant to normalize.
    @return UTC 时间 / UTC instant.
    """

    if type(value) is not datetime:
        raise TypeError("Announcement timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Announcement timestamps must be timezone-aware")
    return value.astimezone(UTC)
