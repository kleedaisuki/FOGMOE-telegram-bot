"""@brief Admin durable recipient 富聚合与类型化决策 / Rich aggregate and typed decisions for durable Admin recipients."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fogmoe_bot.domain.admin.announcement import (
    AnnouncementDispatchContent,
    AnnouncementId,
)
from fogmoe_bot.domain.admin.recipient_state import (
    AnnouncementClaimCapability,
    AnnouncementClaimToken,
    AnnouncementFailureCategory,
    AnnouncementRecipientKind,
    AnnouncementRecipientState,
    AnnouncementRecipientStatus,
    BlockedAnnouncementRecipient,
    ExpandedAnnouncementRecipient,
    FailedAnnouncementRecipient,
    PendingAnnouncementRecipient,
    ProcessingAnnouncementRecipient,
    RetryWaitingAnnouncementRecipient,
    _utc,
)
from fogmoe_bot.domain.conversation.identity import OutboundMessageId


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementRecipient:
    """@brief durable 公告受众回执聚合 / Durable announcement-recipient aggregate."""

    announcement_id: AnnouncementId
    recipient_kind: AnnouncementRecipientKind
    chat_id: int
    message_thread_id: int | None
    reply_to_message_id: int | None
    attempt_count: int
    state: AnnouncementRecipientState
    created_at: datetime
    updated_at: datetime

    def __init__(self) -> None:
        """@brief 禁止绕过 restore 创建聚合 / Prevent aggregate construction outside restore.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use AnnouncementRecipient.restore")

    @classmethod
    def restore(
        cls,
        *,
        announcement_id: AnnouncementId,
        recipient_kind: AnnouncementRecipientKind,
        chat_id: int,
        message_thread_id: int | None,
        reply_to_message_id: int | None,
        status: AnnouncementRecipientStatus | str,
        attempt_count: int,
        next_attempt_at: datetime | None,
        claim_token: AnnouncementClaimToken | UUID | str | None,
        lease_expires_at: datetime | None,
        outbound_message_id: OutboundMessageId | UUID | str | None,
        last_error: str | None,
        created_at: datetime,
        updated_at: datetime,
        expanded_at: datetime | None,
        terminal_at: datetime | None,
    ) -> AnnouncementRecipient:
        """@brief 从完整持久化矩阵恢复聚合 / Restore an aggregate from the complete persistence matrix.

        @return 已验证聚合 / Validated aggregate.
        """

        if not isinstance(announcement_id, AnnouncementId):
            raise TypeError("Announcement recipient requires an AnnouncementId")
        if not isinstance(recipient_kind, AnnouncementRecipientKind):
            raise TypeError("Announcement recipient kind is invalid")
        _validate_address(
            recipient_kind,
            chat_id,
            message_thread_id,
            reply_to_message_id,
        )
        if isinstance(attempt_count, bool) or not isinstance(attempt_count, int):
            raise TypeError("Announcement recipient attempt count must be an integer")
        if attempt_count < 0:
            raise ValueError("Announcement recipient attempt count cannot be negative")
        created = _utc(created_at)
        updated = _utc(updated_at)
        if updated < created:
            raise ValueError("Announcement recipient updated_at precedes created_at")
        state = _restore_state(
            recipient_kind=recipient_kind,
            status=AnnouncementRecipientStatus(status),
            attempt_count=attempt_count,
            next_attempt_at=next_attempt_at,
            claim_token=claim_token,
            lease_expires_at=lease_expires_at,
            outbound_message_id=outbound_message_id,
            last_error=last_error,
            created_at=created,
            updated_at=updated,
            expanded_at=expanded_at,
            terminal_at=terminal_at,
        )
        recipient = object.__new__(cls)
        for name, value in (
            ("announcement_id", announcement_id),
            ("recipient_kind", recipient_kind),
            ("chat_id", chat_id),
            ("message_thread_id", message_thread_id),
            ("reply_to_message_id", reply_to_message_id),
            ("attempt_count", attempt_count),
            ("state", state),
            ("created_at", created),
            ("updated_at", updated),
        ):
            object.__setattr__(recipient, name, value)
        return recipient

    @property
    def status(self) -> AnnouncementRecipientStatus:
        """@brief 返回当前持久化状态 / Return the current persisted status.

        @return 状态枚举 / Status enum.
        """

        return self.state.status

    def release_completion(
        self,
        *,
        released_at: datetime,
    ) -> AnnouncementCompletionReleased:
        """@brief 将 blocked completion 释放为 pending / Release a blocked completion to pending.

        @return 封闭 release 决策 / Closed release decision.
        """

        if (
            self.recipient_kind is not AnnouncementRecipientKind.COMPLETION
            or not isinstance(self.state, BlockedAnnouncementRecipient)
        ):
            raise ValueError("Only a blocked completion recipient can be released")
        timestamp = _utc(released_at)
        if timestamp < self.updated_at:
            raise ValueError("Announcement completion release precedes its state")
        released = self._transition(
            PendingAnnouncementRecipient(timestamp),
            attempt_count=self.attempt_count,
            updated_at=timestamp,
        )
        return AnnouncementCompletionReleased._from_transition(self, released)

    def recover_expired(
        self,
        *,
        recovered_at: datetime,
    ) -> AnnouncementRecipientLeaseRecovered:
        """@brief 回收已到期 processing lease / Recover an expired processing lease.

        @return 保留 attempt 与旧 capability 的恢复决策 / Recovery decision preserving the attempt and old capability.
        """

        if not isinstance(self.state, ProcessingAnnouncementRecipient):
            raise ValueError("Only a processing announcement recipient can be recovered")
        timestamp = _utc(recovered_at)
        capability = self.state.capability
        if timestamp < capability.lease_expires_at:
            raise ValueError("Announcement recipient lease has not expired")
        recovered = self._transition(
            RetryWaitingAnnouncementRecipient(
                timestamp,
                AnnouncementFailureCategory("lease_expired"),
            ),
            attempt_count=self.attempt_count,
            updated_at=timestamp,
        )
        return AnnouncementRecipientLeaseRecovered._from_transition(
            self,
            capability,
            recovered,
        )

    def claim(
        self,
        *,
        token: AnnouncementClaimToken,
        claimed_at: datetime,
        lease_expires_at: datetime,
        content: AnnouncementDispatchContent,
    ) -> AnnouncementRecipientClaim:
        """@brief 从到期等待态领取 recipient / Claim a recipient from a due waiting state.

        @return recipient/content/capability 本体 / Recipient, content, and capability.
        """

        if not isinstance(token, AnnouncementClaimToken):
            raise TypeError("Announcement claim requires an AnnouncementClaimToken")
        if not isinstance(content, AnnouncementDispatchContent):
            raise TypeError("Announcement claim requires dispatch content")
        if not isinstance(
            self.state,
            PendingAnnouncementRecipient | RetryWaitingAnnouncementRecipient,
        ):
            raise ValueError("Announcement recipient is not claimable")
        timestamp = _utc(claimed_at)
        if timestamp < self.state.next_attempt_at:
            raise ValueError("Announcement recipient is not due for claim")
        if timestamp < self.updated_at:
            raise ValueError("Announcement claim timestamps are out of order")
        if content.announcement_created_at > timestamp:
            raise ValueError("Announcement content was created after its claim")
        capability = AnnouncementClaimCapability(token, timestamp, lease_expires_at)
        claimed = self._transition(
            ProcessingAnnouncementRecipient(capability),
            attempt_count=self.attempt_count + 1,
            updated_at=timestamp,
        )
        return AnnouncementRecipientClaim._from(claimed, content, capability)

    def _transition(
        self,
        state: AnnouncementRecipientState,
        *,
        attempt_count: int,
        updated_at: datetime,
    ) -> AnnouncementRecipient:
        """@brief 经唯一 restore 路径构造后继 / Build a successor through the sole restore path.

        @return 已验证后继 / Validated successor.
        """

        capability = (
            state.capability
            if isinstance(state, ProcessingAnnouncementRecipient)
            else None
        )
        return type(self).restore(
            announcement_id=self.announcement_id,
            recipient_kind=self.recipient_kind,
            chat_id=self.chat_id,
            message_thread_id=self.message_thread_id,
            reply_to_message_id=self.reply_to_message_id,
            status=state.status,
            attempt_count=attempt_count,
            next_attempt_at=(
                state.next_attempt_at
                if isinstance(
                    state,
                    PendingAnnouncementRecipient | RetryWaitingAnnouncementRecipient,
                )
                else None
            ),
            claim_token=None if capability is None else capability.token,
            lease_expires_at=(
                None if capability is None else capability.lease_expires_at
            ),
            outbound_message_id=(
                state.outbound_message_id
                if isinstance(state, ExpandedAnnouncementRecipient)
                else None
            ),
            last_error=(
                state.failure.value
                if isinstance(
                    state,
                    RetryWaitingAnnouncementRecipient | FailedAnnouncementRecipient,
                )
                else None
            ),
            created_at=self.created_at,
            updated_at=updated_at,
            expanded_at=(
                state.expanded_at
                if isinstance(state, ExpandedAnnouncementRecipient)
                else None
            ),
            terminal_at=(
                state.terminal_at
                if isinstance(state, FailedAnnouncementRecipient)
                else None
            ),
        )

    def _settle_processing(
        self,
        capability: AnnouncementClaimCapability,
        state: ExpandedAnnouncementRecipient
        | RetryWaitingAnnouncementRecipient
        | FailedAnnouncementRecipient,
        *,
        updated_at: datetime,
    ) -> AnnouncementRecipient:
        """@brief 以精确 capability 结算 processing / Settle processing with the exact capability.

        @return 已验证后继 / Validated successor.
        """

        if not isinstance(self.state, ProcessingAnnouncementRecipient):
            raise ValueError("Announcement recipient is not processing")
        if self.state.capability != capability:
            raise ValueError("Announcement settlement capability is stale")
        timestamp = _utc(updated_at)
        if timestamp < self.updated_at:
            raise ValueError("Announcement settlement precedes its claim")
        return self._transition(
            state,
            attempt_count=self.attempt_count,
            updated_at=timestamp,
        )


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementRecipientClaim:
    """@brief 聚合领取产生的 recipient/content/capability / Recipient, content, and capability produced by claiming."""

    recipient: AnnouncementRecipient
    content: AnnouncementDispatchContent
    capability: AnnouncementClaimCapability

    def __init__(self) -> None:
        """@brief 禁止绕过 aggregate.claim 构造 / Prevent construction outside aggregate.claim.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use AnnouncementRecipient.claim")

    @classmethod
    def _from(
        cls,
        recipient: AnnouncementRecipient,
        content: AnnouncementDispatchContent,
        capability: AnnouncementClaimCapability,
    ) -> AnnouncementRecipientClaim:
        """@brief 从已验证 processing 后态私有构造 / Privately construct from a validated processing post-state.

        @return claim capability / Claim capability.
        """

        state = recipient.state
        if (
            not isinstance(state, ProcessingAnnouncementRecipient)
            or state.capability != capability
        ):
            raise ValueError("Announcement claim requires a processing recipient")
        claim = object.__new__(cls)
        object.__setattr__(claim, "recipient", recipient)
        object.__setattr__(claim, "content", content)
        object.__setattr__(claim, "capability", capability)
        return claim

    def expand(
        self,
        *,
        outbound_message_id: OutboundMessageId,
        completed_at: datetime,
    ) -> AnnouncementRecipientExpanded:
        """@brief 决定成功扩展到 outbox / Decide successful expansion into the outbox.

        @return sealed expanded 决策 / Sealed expanded decision.
        """

        timestamp = self._settlement_time(completed_at)
        recipient = self.recipient._settle_processing(
            self.capability,
            ExpandedAnnouncementRecipient(outbound_message_id, timestamp),
            updated_at=timestamp,
        )
        return AnnouncementRecipientExpanded._from_claim(self, recipient)

    def retry(
        self,
        *,
        retry_at: datetime,
        failure: AnnouncementFailureCategory,
    ) -> AnnouncementRecipientRetryScheduled:
        """@brief 决定安排下一次领取 / Decide to schedule another claim.

        @return sealed retry 决策 / Sealed retry decision.
        """

        if not isinstance(failure, AnnouncementFailureCategory):
            raise TypeError("Announcement retry requires a failure category")
        timestamp = self._settlement_time(retry_at)
        recipient = self.recipient._settle_processing(
            self.capability,
            RetryWaitingAnnouncementRecipient(timestamp, failure),
            updated_at=timestamp,
        )
        return AnnouncementRecipientRetryScheduled._from_claim(self, recipient)

    def dead_letter(
        self,
        *,
        failed_at: datetime,
        failure: AnnouncementFailureCategory,
    ) -> AnnouncementRecipientDeadLettered:
        """@brief 决定进入最终失败 / Decide to enter final failure.

        @return sealed dead-letter 决策 / Sealed dead-letter decision.
        """

        if not isinstance(failure, AnnouncementFailureCategory):
            raise TypeError("Announcement dead letter requires a failure category")
        timestamp = self._settlement_time(failed_at)
        recipient = self.recipient._settle_processing(
            self.capability,
            FailedAnnouncementRecipient(failure, timestamp),
            updated_at=timestamp,
        )
        return AnnouncementRecipientDeadLettered._from_claim(self, recipient)

    def _settlement_time(self, value: datetime) -> datetime:
        """@brief 验证结算不早于领取 / Require settlement at or after claiming.

        @return 规范 UTC 时间 / Canonical UTC instant.
        """

        timestamp = _utc(value)
        if timestamp < self.capability.claimed_at:
            raise ValueError("Announcement settlement precedes its claim")
        return timestamp


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementCompletionReleased:
    """@brief blocked completion 到 pending 的 sealed 决策 / Sealed decision from blocked completion to pending."""

    blocked_recipient: AnnouncementRecipient
    recipient: AnnouncementRecipient

    def __init__(self) -> None:
        """@brief 禁止公开构造 / Prevent public construction.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use AnnouncementRecipient.release_completion")

    @classmethod
    def _from_transition(
        cls,
        blocked: AnnouncementRecipient,
        recipient: AnnouncementRecipient,
    ) -> AnnouncementCompletionReleased:
        """@brief 从领域转换私有构造 / Privately construct from a domain transition.

        @return sealed release 决策 / Sealed release decision.
        """

        if (
            blocked.recipient_kind is not AnnouncementRecipientKind.COMPLETION
            or not isinstance(blocked.state, BlockedAnnouncementRecipient)
            or not isinstance(recipient.state, PendingAnnouncementRecipient)
            or blocked.attempt_count != recipient.attempt_count
            or not _same_identity(blocked, recipient)
        ):
            raise ValueError("Announcement completion release has invalid states")
        decision = object.__new__(cls)
        object.__setattr__(decision, "blocked_recipient", blocked)
        object.__setattr__(decision, "recipient", recipient)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementRecipientLeaseRecovered:
    """@brief processing 到 retry-wait 的 sealed 恢复决策 / Sealed recovery decision from processing to retry-wait."""

    processing_recipient: AnnouncementRecipient
    capability: AnnouncementClaimCapability
    recipient: AnnouncementRecipient

    def __init__(self) -> None:
        """@brief 禁止公开构造 / Prevent public construction.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use AnnouncementRecipient.recover_expired")

    @classmethod
    def _from_transition(
        cls,
        processing: AnnouncementRecipient,
        capability: AnnouncementClaimCapability,
        recipient: AnnouncementRecipient,
    ) -> AnnouncementRecipientLeaseRecovered:
        """@brief 从领域转换私有构造 / Privately construct from a domain transition.

        @return sealed recovery 决策 / Sealed recovery decision.
        """

        state = processing.state
        recovered = recipient.state
        if (
            not isinstance(state, ProcessingAnnouncementRecipient)
            or state.capability != capability
            or not isinstance(recovered, RetryWaitingAnnouncementRecipient)
            or recovered.failure.value != "lease_expired"
            or processing.attempt_count != recipient.attempt_count
            or not _same_identity(processing, recipient)
        ):
            raise ValueError("Announcement lease recovery has invalid states")
        decision = object.__new__(cls)
        object.__setattr__(decision, "processing_recipient", processing)
        object.__setattr__(decision, "capability", capability)
        object.__setattr__(decision, "recipient", recipient)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementRecipientExpanded:
    """@brief claim 产生的 sealed expanded 决策 / Sealed expanded decision produced by a claim."""

    claim: AnnouncementRecipientClaim
    recipient: AnnouncementRecipient

    def __init__(self) -> None:
        """@brief 禁止公开构造 / Prevent public construction.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use AnnouncementRecipientClaim.expand")

    @classmethod
    def _from_claim(
        cls,
        claim: AnnouncementRecipientClaim,
        recipient: AnnouncementRecipient,
    ) -> AnnouncementRecipientExpanded:
        """@brief 从 claim 私有构造 / Privately construct from a claim.

        @return sealed expanded 决策 / Sealed expanded decision.
        """

        _validate_claim_decision(claim, recipient, ExpandedAnnouncementRecipient)
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "recipient", recipient)
        return decision

    def apply_to(self, pre_state: AnnouncementRecipient) -> AnnouncementRecipient:
        """@brief 在真实 pre-state 上重放 / Replay on a real pre-state.

        @return 领域计算后态 / Domain-calculated post-state.
        """

        state = self.recipient.state
        if not isinstance(state, ExpandedAnnouncementRecipient):
            raise RuntimeError("Expanded decision lost its post-state")
        return pre_state._settle_processing(
            self.claim.capability,
            state,
            updated_at=self.recipient.updated_at,
        )


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementRecipientRetryScheduled:
    """@brief claim 产生的 sealed retry 决策 / Sealed retry decision produced by a claim."""

    claim: AnnouncementRecipientClaim
    recipient: AnnouncementRecipient

    def __init__(self) -> None:
        """@brief 禁止公开构造 / Prevent public construction.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use AnnouncementRecipientClaim.retry")

    @classmethod
    def _from_claim(
        cls,
        claim: AnnouncementRecipientClaim,
        recipient: AnnouncementRecipient,
    ) -> AnnouncementRecipientRetryScheduled:
        """@brief 从 claim 私有构造 / Privately construct from a claim.

        @return sealed retry 决策 / Sealed retry decision.
        """

        _validate_claim_decision(
            claim,
            recipient,
            RetryWaitingAnnouncementRecipient,
        )
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "recipient", recipient)
        return decision

    def apply_to(self, pre_state: AnnouncementRecipient) -> AnnouncementRecipient:
        """@brief 在真实 pre-state 上重放 / Replay on a real pre-state.

        @return 领域计算后态 / Domain-calculated post-state.
        """

        state = self.recipient.state
        if not isinstance(state, RetryWaitingAnnouncementRecipient):
            raise RuntimeError("Retry decision lost its post-state")
        return pre_state._settle_processing(
            self.claim.capability,
            state,
            updated_at=self.recipient.updated_at,
        )


@dataclass(frozen=True, slots=True, init=False)
class AnnouncementRecipientDeadLettered:
    """@brief claim 产生的 sealed dead-letter 决策 / Sealed dead-letter decision produced by a claim."""

    claim: AnnouncementRecipientClaim
    recipient: AnnouncementRecipient

    def __init__(self) -> None:
        """@brief 禁止公开构造 / Prevent public construction.

        @raise TypeError 始终抛出 / Always raised.
        """

        raise TypeError("Use AnnouncementRecipientClaim.dead_letter")

    @classmethod
    def _from_claim(
        cls,
        claim: AnnouncementRecipientClaim,
        recipient: AnnouncementRecipient,
    ) -> AnnouncementRecipientDeadLettered:
        """@brief 从 claim 私有构造 / Privately construct from a claim.

        @return sealed dead-letter 决策 / Sealed dead-letter decision.
        """

        _validate_claim_decision(claim, recipient, FailedAnnouncementRecipient)
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "recipient", recipient)
        return decision

    def apply_to(self, pre_state: AnnouncementRecipient) -> AnnouncementRecipient:
        """@brief 在真实 pre-state 上重放 / Replay on a real pre-state.

        @return 领域计算后态 / Domain-calculated post-state.
        """

        state = self.recipient.state
        if not isinstance(state, FailedAnnouncementRecipient):
            raise RuntimeError("Dead-letter decision lost its post-state")
        return pre_state._settle_processing(
            self.claim.capability,
            state,
            updated_at=self.recipient.updated_at,
        )


def _restore_state(
    *,
    recipient_kind: AnnouncementRecipientKind,
    status: AnnouncementRecipientStatus,
    attempt_count: int,
    next_attempt_at: datetime | None,
    claim_token: AnnouncementClaimToken | UUID | str | None,
    lease_expires_at: datetime | None,
    outbound_message_id: OutboundMessageId | UUID | str | None,
    last_error: str | None,
    created_at: datetime,
    updated_at: datetime,
    expanded_at: datetime | None,
    terminal_at: datetime | None,
) -> AnnouncementRecipientState:
    """@brief 将完整列矩阵恢复为穷尽状态 / Restore the complete column matrix into an exhaustive state.

    @return 已验证状态 / Validated state.
    """

    match status:
        case AnnouncementRecipientStatus.BLOCKED:
            _require_attempt(attempt_count, exact=0)
            if recipient_kind is not AnnouncementRecipientKind.COMPLETION:
                raise ValueError("Only completion recipients may be blocked")
            _require_none(
                next_attempt_at,
                claim_token,
                lease_expires_at,
                outbound_message_id,
                last_error,
                expanded_at,
                terminal_at,
            )
            return BlockedAnnouncementRecipient()
        case AnnouncementRecipientStatus.PENDING:
            _require_attempt(attempt_count, exact=0)
            _require_none(
                claim_token,
                lease_expires_at,
                outbound_message_id,
                last_error,
                expanded_at,
                terminal_at,
            )
            claimable = _required_utc(next_attempt_at, "next_attempt_at")
            if claimable != updated_at or claimable < created_at:
                raise ValueError("Pending announcement recipient time shape is invalid")
            return PendingAnnouncementRecipient(claimable)
        case AnnouncementRecipientStatus.PROCESSING:
            _require_attempt(attempt_count)
            _require_none(
                next_attempt_at,
                outbound_message_id,
                last_error,
                expanded_at,
                terminal_at,
            )
            return ProcessingAnnouncementRecipient(
                AnnouncementClaimCapability(
                    _required_token(claim_token),
                    updated_at,
                    _required_utc(lease_expires_at, "lease_expires_at"),
                )
            )
        case AnnouncementRecipientStatus.RETRY_WAIT:
            _require_attempt(attempt_count)
            _require_none(
                claim_token,
                lease_expires_at,
                outbound_message_id,
                expanded_at,
                terminal_at,
            )
            retry_at = _required_utc(next_attempt_at, "next_attempt_at")
            if retry_at != updated_at or retry_at < created_at:
                raise ValueError("Retry-wait announcement recipient time shape is invalid")
            return RetryWaitingAnnouncementRecipient(
                retry_at,
                _required_failure(last_error),
            )
        case AnnouncementRecipientStatus.EXPANDED:
            _require_attempt(attempt_count)
            _require_none(
                next_attempt_at,
                claim_token,
                lease_expires_at,
                last_error,
                terminal_at,
            )
            completed = _required_utc(expanded_at, "expanded_at")
            if completed != updated_at or completed < created_at:
                raise ValueError("Expanded announcement recipient time shape is invalid")
            return ExpandedAnnouncementRecipient(
                _required_outbound_id(outbound_message_id),
                completed,
            )
        case AnnouncementRecipientStatus.FAILED_FINAL:
            _require_attempt(attempt_count)
            _require_none(
                next_attempt_at,
                claim_token,
                lease_expires_at,
                outbound_message_id,
                expanded_at,
            )
            completed = _required_utc(terminal_at, "terminal_at")
            if completed != updated_at or completed < created_at:
                raise ValueError("Failed announcement recipient time shape is invalid")
            return FailedAnnouncementRecipient(
                _required_failure(last_error),
                completed,
            )


def _validate_address(
    kind: AnnouncementRecipientKind,
    chat_id: int,
    thread_id: int | None,
    reply_id: int | None,
) -> None:
    """@brief 验证 recipient 地址矩阵 / Validate the recipient address matrix.

    @return None / None.
    """

    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        raise TypeError("Announcement recipient chat ID must be an integer")
    if chat_id == 0:
        raise ValueError("Announcement recipient chat ID cannot be zero")
    for name, value in (("message thread", thread_id), ("reply message", reply_id)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise TypeError(f"Announcement {name} ID must be an integer")
        if value is not None and value < 1:
            raise ValueError(f"Announcement {name} ID must be positive")
    if (kind is AnnouncementRecipientKind.COMPLETION) != (reply_id is not None):
        raise ValueError("Only completion recipients require a reply message")


def _validate_claim_decision(
    claim: AnnouncementRecipientClaim,
    recipient: AnnouncementRecipient,
    expected_state: type[AnnouncementRecipientState],
) -> None:
    """@brief 验证 claim 产生的 sealed 决策 / Validate a sealed decision produced by a claim.

    @return None / None.
    """

    if not isinstance(recipient.state, expected_state):
        raise ValueError("Announcement recipient decision has an invalid post-state")
    if (
        recipient.attempt_count != claim.recipient.attempt_count
        or not _same_identity(claim.recipient, recipient)
    ):
        raise ValueError("Announcement recipient decision changed claim identity")


def _same_identity(first: AnnouncementRecipient, second: AnnouncementRecipient) -> bool:
    """@brief 比较跨状态不变身份 / Compare identity invariant across states.

    @return 身份与创建事实相同时为 True / True when identity and creation facts match.
    """

    return (
        first.announcement_id == second.announcement_id
        and first.recipient_kind is second.recipient_kind
        and first.chat_id == second.chat_id
        and first.message_thread_id == second.message_thread_id
        and first.reply_to_message_id == second.reply_to_message_id
        and first.created_at == second.created_at
    )


def _require_attempt(value: int, *, exact: int | None = None) -> None:
    """@brief 验证状态允许的 attempt / Validate the attempt allowed by a state.

    @return None / None.
    """

    if exact is not None:
        if value != exact:
            raise ValueError(f"Announcement recipient attempt count must equal {exact}")
    elif value < 1:
        raise ValueError("Claimed announcement recipient needs a positive attempt count")


def _require_none(*values: object) -> None:
    """@brief 要求状态外列为空 / Require out-of-state columns to be null.

    @return None / None.
    """

    if any(value is not None for value in values):
        raise ValueError("Announcement recipient persistence shape is inconsistent")


def _required_utc(value: datetime | None, name: str) -> datetime:
    """@brief 读取必需 UTC 时间 / Read a required UTC instant.

    @return UTC 时间 / UTC instant.
    """

    if value is None:
        raise ValueError(f"Announcement recipient {name} is required")
    return _utc(value)


def _required_token(
    value: AnnouncementClaimToken | UUID | str | None,
) -> AnnouncementClaimToken:
    """@brief 读取必需 token / Read a required token.

    @return 强类型 token / Strong token.
    """

    if value is None:
        raise ValueError("Processing announcement recipient requires a claim token")
    return value if isinstance(value, AnnouncementClaimToken) else AnnouncementClaimToken.parse(value)


def _required_outbound_id(
    value: OutboundMessageId | UUID | str | None,
) -> OutboundMessageId:
    """@brief 读取必需 outbox ID / Read a required outbox ID.

    @return 强类型 ID / Strong ID.
    """

    if value is None:
        raise ValueError("Expanded announcement recipient requires an outbound ID")
    return value if isinstance(value, OutboundMessageId) else OutboundMessageId.parse(value)


def _required_failure(value: str | None) -> AnnouncementFailureCategory:
    """@brief 读取必需失败类别 / Read a required failure category.

    @return 失败类别 / Failure category.
    """

    if value is None:
        raise ValueError("Failed announcement recipient requires a failure category")
    return AnnouncementFailureCategory(value)


__all__ = [
    "AnnouncementClaimCapability",
    "AnnouncementClaimToken",
    "AnnouncementCompletionReleased",
    "AnnouncementFailureCategory",
    "AnnouncementRecipient",
    "AnnouncementRecipientClaim",
    "AnnouncementRecipientDeadLettered",
    "AnnouncementRecipientExpanded",
    "AnnouncementRecipientKind",
    "AnnouncementRecipientLeaseRecovered",
    "AnnouncementRecipientRetryScheduled",
    "AnnouncementRecipientState",
    "AnnouncementRecipientStatus",
    "BlockedAnnouncementRecipient",
    "ExpandedAnnouncementRecipient",
    "FailedAnnouncementRecipient",
    "PendingAnnouncementRecipient",
    "ProcessingAnnouncementRecipient",
    "RetryWaitingAnnouncementRecipient",
]
