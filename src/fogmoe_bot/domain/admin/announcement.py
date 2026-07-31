"""@brief Admin 公告意图、主聚合与生命周期决策 / Admin announcement intent, aggregate, and lifecycle decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID, uuid5

if TYPE_CHECKING:
    from fogmoe_bot.domain.admin.recipient import (
        AnnouncementCompletionReleased,
        AnnouncementRecipient,
    )

_ANNOUNCEMENT_ID_NAMESPACE = UUID("6bfaf3fd-aaf4-53f5-b789-0db71fe8b9ef")
"""@brief 幂等键到公告 ID 的 UUIDv5 命名空间 / UUIDv5 namespace mapping idempotency keys to announcement IDs."""


@dataclass(frozen=True, slots=True, order=True)
class AnnouncementId:
    """@brief 持久化公告标识符 / Durable announcement identifier."""

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
class AnnouncementCompletionAddress:
    """@brief 公告最终报告的不可变回复地址 / Immutable reply address for the final announcement report."""

    chat_id: int
    """@brief 报告会话 ID / Report chat ID."""

    message_thread_id: int | None
    """@brief 可选话题 ID / Optional message-thread ID."""

    reply_to_message_id: int
    """@brief 原管理命令消息 ID / Original administrative-command message ID."""

    def __post_init__(self) -> None:
        """@brief 验证回复地址 / Validate the reply address.

        @return None / None.
        """

        _require_integer(self.chat_id, "completion chat ID")
        if self.chat_id == 0:
            raise ValueError("Announcement completion chat ID cannot be zero")
        if self.message_thread_id is not None:
            _require_integer(self.message_thread_id, "completion thread ID")
            if self.message_thread_id < 1:
                raise ValueError("Announcement completion thread ID must be positive")
        _require_integer(self.reply_to_message_id, "completion reply message ID")
        if self.reply_to_message_id < 1:
            raise ValueError(
                "Announcement completion reply message ID must be positive"
            )


@dataclass(frozen=True, slots=True)
class AnnouncementIntent:
    """@brief 幂等公告请求的完整领域语义 / Complete domain semantics of an idempotent announcement request."""

    idempotency_key: str
    """@brief 来源命令的稳定幂等键 / Stable idempotency key of the source command."""

    requested_by: int
    """@brief 请求管理员主体 ID / Requesting administrator principal ID."""

    source_update_id: int
    """@brief 来源 Update ID / Source Update ID."""

    body: str
    """@brief 公告正文 / Announcement body."""

    completion_address: AnnouncementCompletionAddress
    """@brief 最终报告回复地址 / Final-report reply address."""

    requested_at: datetime
    """@brief 公告意图创建时刻 / Announcement-intent creation instant."""

    def __post_init__(self) -> None:
        """@brief 规范并验证完整意图 / Normalize and validate the complete intent.

        @return None / None.
        """

        if not isinstance(self.idempotency_key, str):
            raise TypeError("Announcement idempotency key must be a string")
        key = self.idempotency_key.strip()
        if not key or len(key) > 255:
            raise ValueError(
                "Announcement idempotency key must contain 1-255 characters"
            )
        _require_integer(self.requested_by, "requesting principal ID")
        if self.requested_by < 1:
            raise ValueError("Announcement requesting principal ID must be positive")
        _require_integer(self.source_update_id, "source update ID")
        if self.source_update_id < 0:
            raise ValueError("Announcement source update ID cannot be negative")
        if not isinstance(self.body, str):
            raise TypeError("Announcement body must be a string")
        if not self.body.strip() or len(self.body) > 3500:
            raise ValueError("Announcement body must contain 1-3500 characters")
        if not isinstance(self.completion_address, AnnouncementCompletionAddress):
            raise TypeError("Announcement intent requires a completion address")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "requested_at", _utc(self.requested_at))


@dataclass(frozen=True, slots=True)
class AnnouncementAudienceSnapshot:
    """@brief 在接收事务内冻结的受众规模 / Audience size frozen in the acceptance transaction."""

    recipient_count: int
    """@brief 用户与群组受众总数 / Total user and group audience count."""

    def __post_init__(self) -> None:
        """@brief 验证快照规模 / Validate the snapshot size.

        @return None / None.
        """

        _require_non_negative_count(self.recipient_count, "audience snapshot")


@dataclass(frozen=True, slots=True)
class AnnouncementAudienceProgress:
    """@brief 受众扩展阶段的一致进度证据 / Consistent progress evidence for audience expansion."""

    recipient_count: int
    """@brief 实际快照受众总数 / Actual snapshotted audience count."""

    terminal_count: int
    """@brief 已 expanded 或 failed-final 的受众数 / Audience count already expanded or finally failed."""

    def __post_init__(self) -> None:
        """@brief 验证进度计数矩阵 / Validate the progress-count matrix.

        @return None / None.
        """

        _require_non_negative_count(self.recipient_count, "audience progress")
        _require_non_negative_count(self.terminal_count, "terminal audience")
        if self.terminal_count > self.recipient_count:
            raise ValueError("Terminal audience count exceeds the audience snapshot")


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


class AnnouncementStatus(StrEnum):
    """@brief 主公告聚合的穷尽持久化状态 / Exhaustive persisted states of the main announcement aggregate."""

    EXPANDING = "expanding"
    DELIVERING = "delivering"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ExpandingAnnouncement:
    """@brief 正在将受众扩展到 outbox / Audience is being expanded into the outbox."""

    status: ClassVar[AnnouncementStatus] = AnnouncementStatus.EXPANDING


@dataclass(frozen=True, slots=True)
class DeliveringAnnouncement:
    """@brief 等待所有受众 outbox 进入终态 / Waiting for all audience outbox messages to become terminal."""

    status: ClassVar[AnnouncementStatus] = AnnouncementStatus.DELIVERING


@dataclass(frozen=True, slots=True)
class CompletedAnnouncement:
    """@brief 全部受众投递已终结 / All audience delivery has terminated."""

    completed_at: datetime
    """@brief 投递完成时刻 / Delivery-completion instant."""

    status: ClassVar[AnnouncementStatus] = AnnouncementStatus.COMPLETED

    def __post_init__(self) -> None:
        """@brief 规范完成时间 / Normalize the completion instant.

        @return None / None.
        """

        object.__setattr__(self, "completed_at", _utc(self.completed_at))


type AnnouncementState = (
    ExpandingAnnouncement | DeliveringAnnouncement | CompletedAnnouncement
)
"""@brief 主公告聚合的穷尽状态联合 / Exhaustive state union of the main announcement aggregate."""


class AnnouncementIntentMismatch(ValueError):
    """@brief 同一幂等键被重放为不同领域意图 / The same idempotency key was replayed with different domain intent."""


@dataclass(frozen=True, slots=True, init=False)
class Announcement:
    """@brief admin.announcements 主聚合 / Main aggregate represented by admin.announcements."""

    announcement_id: AnnouncementId
    """@brief 稳定公告 ID / Stable announcement ID."""

    intent: AnnouncementIntent
    """@brief 不可变请求语义 / Immutable request semantics."""

    recipient_count: int
    """@brief 冻结的受众规模 / Frozen audience size."""

    state: AnnouncementState
    """@brief 当前穷尽状态 / Current exhaustive state."""

    updated_at: datetime
    """@brief 最近领域转换时刻 / Most recent domain-transition instant."""

    def __init__(self) -> None:
        """@brief 禁止绕过 start/restore 创建聚合 / Prevent construction outside start or restore.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use Announcement.start or Announcement.restore")

    @classmethod
    def start(cls, intent: AnnouncementIntent) -> Announcement:
        """@brief 创建尚未记录受众快照的公告 / Start an announcement before its audience snapshot is recorded.

        @param intent 完整不可变意图 / Complete immutable intent.
        @return 初始 expanding 聚合 / Initial expanding aggregate.
        """

        if not isinstance(intent, AnnouncementIntent):
            raise TypeError("Announcement start requires an AnnouncementIntent")
        return cls._from_validated(
            announcement_id=AnnouncementId.for_idempotency_key(intent.idempotency_key),
            intent=intent,
            recipient_count=0,
            state=ExpandingAnnouncement(),
            updated_at=intent.requested_at,
        )

    @classmethod
    def restore(
        cls,
        *,
        announcement_id: AnnouncementId,
        idempotency_key: str,
        requested_by: int,
        source_update_id: int,
        body: str,
        completion_chat_id: int,
        completion_message_thread_id: int | None,
        completion_reply_to_message_id: int,
        recipient_count: int,
        status: AnnouncementStatus | str,
        created_at: datetime,
        updated_at: datetime,
        completed_at: datetime | None,
    ) -> Announcement:
        """@brief 从完整持久化矩阵恢复主聚合 / Restore the main aggregate from its complete persistence matrix.

        @return 已验证主聚合 / Validated main aggregate.
        """

        if not isinstance(announcement_id, AnnouncementId):
            raise TypeError("Announcement restore requires an AnnouncementId")
        intent = AnnouncementIntent(
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            source_update_id=source_update_id,
            body=body,
            completion_address=AnnouncementCompletionAddress(
                chat_id=completion_chat_id,
                message_thread_id=completion_message_thread_id,
                reply_to_message_id=completion_reply_to_message_id,
            ),
            requested_at=created_at,
        )
        expected_id = AnnouncementId.for_idempotency_key(intent.idempotency_key)
        if announcement_id != expected_id:
            raise ValueError("Announcement ID disagrees with its idempotency key")
        _require_non_negative_count(recipient_count, "recipient")
        updated = _utc(updated_at)
        if updated < intent.requested_at:
            raise ValueError("Announcement updated_at precedes its creation")
        state = _restore_state(
            status=AnnouncementStatus(status),
            completed_at=completed_at,
            updated_at=updated,
        )
        return cls._from_validated(
            announcement_id=announcement_id,
            intent=intent,
            recipient_count=recipient_count,
            state=state,
            updated_at=updated,
        )

    @classmethod
    def _from_validated(
        cls,
        *,
        announcement_id: AnnouncementId,
        intent: AnnouncementIntent,
        recipient_count: int,
        state: AnnouncementState,
        updated_at: datetime,
    ) -> Announcement:
        """@brief 从已验证值私有创建聚合 / Privately create an aggregate from validated values.

        @return 主公告聚合 / Main announcement aggregate.
        """

        announcement = object.__new__(cls)
        object.__setattr__(announcement, "announcement_id", announcement_id)
        object.__setattr__(announcement, "intent", intent)
        object.__setattr__(announcement, "recipient_count", recipient_count)
        object.__setattr__(announcement, "state", state)
        object.__setattr__(announcement, "updated_at", updated_at)
        return announcement

    @property
    def status(self) -> AnnouncementStatus:
        """@brief 返回当前持久化状态 / Return the current persisted status.

        @return 状态枚举 / Status enum.
        """

        return self.state.status

    def require_same_intent(self, replayed: AnnouncementIntent) -> None:
        """@brief 要求幂等重放表达相同领域意图 / Require an idempotent replay to express the same domain intent.

        @param replayed 重放意图 / Replayed intent.
        @return None / None.
        @raise AnnouncementIntentMismatch 重放语义不同 / Replay semantics differ.
        """

        if not isinstance(replayed, AnnouncementIntent):
            raise TypeError("Announcement replay requires an AnnouncementIntent")
        if self.intent != replayed:
            raise AnnouncementIntentMismatch(
                "Announcement idempotency key already denotes another intent"
            )

    def record_audience_snapshot(
        self,
        snapshot: AnnouncementAudienceSnapshot,
        *,
        recorded_at: datetime,
    ) -> AnnouncementAudienceSnapshotted:
        """@brief 记录固定受众并处理零受众直达 delivering / Record the fixed audience, sending zero audience directly to delivering.

        @return sealed 受众快照决策 / Sealed audience-snapshot decision.
        """

        if not isinstance(snapshot, AnnouncementAudienceSnapshot):
            raise TypeError("Announcement snapshot transition requires a snapshot")
        if (
            not isinstance(self.state, ExpandingAnnouncement)
            or self.recipient_count != 0
        ):
            raise ValueError("Announcement audience snapshot was already recorded")
        timestamp = self._transition_time(recorded_at)
        state: AnnouncementState = (
            DeliveringAnnouncement()
            if snapshot.recipient_count == 0
            else ExpandingAnnouncement()
        )
        announcement = self._transition(
            recipient_count=snapshot.recipient_count,
            state=state,
            updated_at=timestamp,
        )
        return AnnouncementAudienceSnapshotted._from_transition(
            self,
            snapshot,
            announcement,
        )

    def finish_audience_expansion(
        self,
        progress: AnnouncementAudienceProgress,
        *,
        finished_at: datetime,
    ) -> AnnouncementDeliveryStarted:
        """@brief 最后一个受众终结后进入投递等待 / Enter delivery waiting after the final audience expansion terminates.

        @return sealed delivering 决策 / Sealed delivering decision.
        """

        if not isinstance(progress, AnnouncementAudienceProgress):
            raise TypeError("Announcement expansion transition requires progress")
        if not isinstance(self.state, ExpandingAnnouncement):
            raise ValueError("Only an expanding announcement can begin delivery")
        if self.recipient_count < 1:
            raise ValueError(
                "Zero-audience announcements begin delivery at snapshot time"
            )
        if (
            progress.recipient_count != self.recipient_count
            or progress.terminal_count != self.recipient_count
        ):
            raise ValueError("Announcement audience expansion is not complete")
        timestamp = self._transition_time(finished_at)
        announcement = self._transition(
            recipient_count=self.recipient_count,
            state=DeliveringAnnouncement(),
            updated_at=timestamp,
        )
        return AnnouncementDeliveryStarted._from_transition(
            self,
            progress,
            announcement,
        )

    def complete_delivery(
        self,
        counts: AnnouncementDeliveryCounts,
        *,
        completion_recipient: AnnouncementRecipient,
        completed_at: datetime,
    ) -> AnnouncementDeliveryCompleted:
        """@brief 完成投递并组合 blocked completion 释放 / Complete delivery and compose the blocked-completion release.

        @return 同时包含公告与 completion 后态的 sealed 决策 / Sealed decision containing both announcement and completion post-states.
        """

        from fogmoe_bot.domain.admin.recipient import AnnouncementRecipient

        if not isinstance(counts, AnnouncementDeliveryCounts):
            raise TypeError("Announcement completion requires delivery counts")
        if not isinstance(completion_recipient, AnnouncementRecipient):
            raise TypeError("Announcement completion requires a completion recipient")
        if not isinstance(self.state, DeliveringAnnouncement):
            raise ValueError("Only a delivering announcement can be completed")
        if (
            counts.recipients != self.recipient_count
            or counts.delivered + counts.failed != self.recipient_count
        ):
            raise ValueError(
                "Announcement delivery is not terminal for every recipient"
            )
        address = self.intent.completion_address
        if (
            completion_recipient.announcement_id != self.announcement_id
            or completion_recipient.chat_id != address.chat_id
            or completion_recipient.message_thread_id != address.message_thread_id
            or completion_recipient.reply_to_message_id != address.reply_to_message_id
        ):
            raise ValueError(
                "Announcement completion recipient disagrees with its intent"
            )
        timestamp = self._transition_time(completed_at)
        completion_release = completion_recipient.release_completion(
            released_at=timestamp
        )
        announcement = self._transition(
            recipient_count=self.recipient_count,
            state=CompletedAnnouncement(timestamp),
            updated_at=timestamp,
        )
        return AnnouncementDeliveryCompleted._from_transition(
            self,
            counts,
            completion_release,
            announcement,
        )

    def _transition(
        self,
        *,
        recipient_count: int,
        state: AnnouncementState,
        updated_at: datetime,
    ) -> Announcement:
        """@brief 从不变身份创建后继聚合 / Build a successor aggregate from invariant identity.

        @return 后继聚合 / Successor aggregate.
        """

        return type(self)._from_validated(
            announcement_id=self.announcement_id,
            intent=self.intent,
            recipient_count=recipient_count,
            state=state,
            updated_at=updated_at,
        )

    def _transition_time(self, value: datetime) -> datetime:
        """@brief 验证领域时间单调性 / Validate monotonic domain time.

        @return 规范 UTC 时间 / Canonical UTC instant.
        """

        timestamp = _utc(value)
        if timestamp < self.updated_at:
            raise ValueError("Announcement transition precedes its current state")
        return timestamp


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementAudienceSnapshotted:
    """@brief 受众快照记录的 sealed 决策 / Sealed decision recording an audience snapshot."""

    expanding_announcement: Announcement
    """@brief 快照前 expanding 聚合 / Expanding aggregate before the snapshot."""

    snapshot: AnnouncementAudienceSnapshot
    """@brief 冻结的受众规模 / Frozen audience size."""

    announcement: Announcement
    """@brief 快照后聚合 / Aggregate after the snapshot."""

    def __init__(self) -> None:
        """@brief 禁止公开构造 / Prevent public construction.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use Announcement.record_audience_snapshot")

    @classmethod
    def _from_transition(
        cls,
        expanding: Announcement,
        snapshot: AnnouncementAudienceSnapshot,
        announcement: Announcement,
    ) -> AnnouncementAudienceSnapshotted:
        """@brief 从已验证转换私有构造 / Privately construct from a validated transition.

        @return sealed 快照决策 / Sealed snapshot decision.
        """

        expected_state = (
            DeliveringAnnouncement
            if snapshot.recipient_count == 0
            else ExpandingAnnouncement
        )
        if (
            not isinstance(expanding.state, ExpandingAnnouncement)
            or expanding.recipient_count != 0
            or announcement.recipient_count != snapshot.recipient_count
            or not isinstance(announcement.state, expected_state)
            or not _same_identity(expanding, announcement)
        ):
            raise ValueError("Announcement audience snapshot has invalid states")
        decision = object.__new__(cls)
        object.__setattr__(decision, "expanding_announcement", expanding)
        object.__setattr__(decision, "snapshot", snapshot)
        object.__setattr__(decision, "announcement", announcement)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementDeliveryStarted:
    """@brief expanding 到 delivering 的 sealed 决策 / Sealed decision from expanding to delivering."""

    expanding_announcement: Announcement
    """@brief 转换前聚合 / Aggregate before the transition."""

    progress: AnnouncementAudienceProgress
    """@brief 全部受众终结的证据 / Evidence that every audience expansion terminated."""

    announcement: Announcement
    """@brief delivering 后态聚合 / Delivering post-state aggregate."""

    def __init__(self) -> None:
        """@brief 禁止公开构造 / Prevent public construction.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use Announcement.finish_audience_expansion")

    @classmethod
    def _from_transition(
        cls,
        expanding: Announcement,
        progress: AnnouncementAudienceProgress,
        announcement: Announcement,
    ) -> AnnouncementDeliveryStarted:
        """@brief 从已验证转换私有构造 / Privately construct from a validated transition.

        @return sealed delivering 决策 / Sealed delivering decision.
        """

        if (
            not isinstance(expanding.state, ExpandingAnnouncement)
            or not isinstance(announcement.state, DeliveringAnnouncement)
            or progress.recipient_count != expanding.recipient_count
            or progress.terminal_count != expanding.recipient_count
            or announcement.recipient_count != expanding.recipient_count
            or not _same_identity(expanding, announcement)
        ):
            raise ValueError("Announcement delivery start has invalid states")
        decision = object.__new__(cls)
        object.__setattr__(decision, "expanding_announcement", expanding)
        object.__setattr__(decision, "progress", progress)
        object.__setattr__(decision, "announcement", announcement)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementDeliveryCompleted:
    """@brief 公告完成与 completion 释放的复合 sealed 决策 / Compound sealed decision completing an announcement and releasing its completion receipt."""

    delivering_announcement: Announcement
    """@brief 转换前 delivering 聚合 / Delivering aggregate before completion."""

    counts: AnnouncementDeliveryCounts
    """@brief 完整终态投递计数 / Complete terminal-delivery counts."""

    completion_release: AnnouncementCompletionReleased
    """@brief 同事务持久化的 completion 释放 / Completion release persisted in the same transaction."""

    announcement: Announcement
    """@brief completed 后态聚合 / Completed post-state aggregate."""

    def __init__(self) -> None:
        """@brief 禁止公开构造 / Prevent public construction.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use Announcement.complete_delivery")

    @classmethod
    def _from_transition(
        cls,
        delivering: Announcement,
        counts: AnnouncementDeliveryCounts,
        completion_release: AnnouncementCompletionReleased,
        announcement: Announcement,
    ) -> AnnouncementDeliveryCompleted:
        """@brief 从公告与 completion 后态私有构造 / Privately construct from announcement and completion post-states.

        @return 复合完成决策 / Compound completion decision.
        """

        from fogmoe_bot.domain.admin.recipient import AnnouncementCompletionReleased

        if not isinstance(completion_release, AnnouncementCompletionReleased):
            raise TypeError(
                "Announcement completion requires a sealed completion release"
            )
        released = completion_release.recipient
        address = announcement.intent.completion_address
        if (
            not isinstance(delivering.state, DeliveringAnnouncement)
            or not isinstance(announcement.state, CompletedAnnouncement)
            or released.announcement_id != announcement.announcement_id
            or released.chat_id != address.chat_id
            or released.message_thread_id != address.message_thread_id
            or released.reply_to_message_id != address.reply_to_message_id
            or released.updated_at != announcement.updated_at
            or delivering.recipient_count != announcement.recipient_count
            or counts.recipients != announcement.recipient_count
            or counts.delivered + counts.failed != announcement.recipient_count
            or not _same_identity(delivering, announcement)
        ):
            raise ValueError("Announcement delivery completion has invalid states")
        decision = object.__new__(cls)
        object.__setattr__(decision, "delivering_announcement", delivering)
        object.__setattr__(decision, "counts", counts)
        object.__setattr__(decision, "completion_release", completion_release)
        object.__setattr__(decision, "announcement", announcement)
        return decision


def _restore_state(
    *,
    status: AnnouncementStatus,
    completed_at: datetime | None,
    updated_at: datetime,
) -> AnnouncementState:
    """@brief 从持久化状态矩阵恢复穷尽状态 / Restore an exhaustive state from the persistence matrix.

    @return 已验证状态 / Validated state.
    """

    match status:
        case AnnouncementStatus.EXPANDING:
            if completed_at is not None:
                raise ValueError("Expanding announcement cannot have completed_at")
            return ExpandingAnnouncement()
        case AnnouncementStatus.DELIVERING:
            if completed_at is not None:
                raise ValueError("Delivering announcement cannot have completed_at")
            return DeliveringAnnouncement()
        case AnnouncementStatus.COMPLETED:
            if completed_at is None:
                raise ValueError("Completed announcement requires completed_at")
            completed = _utc(completed_at)
            if completed != updated_at:
                raise ValueError(
                    "Completed announcement timestamp must equal updated_at"
                )
            return CompletedAnnouncement(completed)


def _same_identity(first: Announcement, second: Announcement) -> bool:
    """@brief 比较跨状态不可变公告事实 / Compare immutable announcement facts across states.

    @return 不可变事实一致时为 True / True when immutable facts are equal.
    """

    return (
        first.announcement_id == second.announcement_id
        and first.intent == second.intent
    )


def _require_integer(value: int, name: str) -> None:
    """@brief 验证真正整数而非 bool / Validate a real integer rather than bool.

    @return None / None.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Announcement {name} must be an integer")


def _require_non_negative_count(value: int, name: str) -> None:
    """@brief 验证非负计数 / Validate a non-negative count.

    @return None / None.
    """

    _require_integer(value, f"{name} count")
    if value < 0:
        raise ValueError(f"Announcement {name} count cannot be negative")


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
    "Announcement",
    "AnnouncementAudienceProgress",
    "AnnouncementAudienceSnapshot",
    "AnnouncementAudienceSnapshotted",
    "AnnouncementCompletionAddress",
    "AnnouncementDeliveryCompleted",
    "AnnouncementDeliveryCounts",
    "AnnouncementDeliveryStarted",
    "AnnouncementDispatchContent",
    "AnnouncementId",
    "AnnouncementIntent",
    "AnnouncementIntentMismatch",
    "AnnouncementState",
    "AnnouncementStatus",
    "CompletedAnnouncement",
    "DeliveringAnnouncement",
    "ExpandingAnnouncement",
]
