"""@brief 银行代币请求聚合 / Bank token-request aggregate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from .money import TokenAmount, TokenBucket


class TokenRequestStatus(StrEnum):
    """@brief 代币请求状态 / Token-request status."""

    PENDING = "pending"
    """@brief 等待银行审核 / Awaiting bank review."""

    APPROVED = "approved"
    """@brief 已批准并已发行 / Approved and issued."""

    REJECTED = "rejected"
    """@brief 已拒绝 / Rejected."""

    CANCELLED = "cancelled"
    """@brief 由申请者取消 / Cancelled by requester."""


@dataclass(frozen=True, slots=True)
class TokenRequest:
    """@brief 用户向银行申请代币的聚合 / Aggregate for a user requesting tokens from the bank.

    @param request_id 请求标识 / Request identity.
    @param requester_id 申请用户 / Requesting user.
    @param requested_amount 申请数量 / Requested amount.
    @param requested_bucket 希望授予的钱包类别 / Desired wallet bucket.
    @param purpose 用户说明 / User-provided purpose.
    @param status 当前状态 / Current status.
    @param requested_at 请求时刻 / Request instant.
    @param reviewed_at 审核时刻 / Review instant.
    @param reviewer_id 审核管理员 / Reviewing administrator.
    @param review_note 审核说明 / Review note.
    @param ledger_entry_id 批准发行的账本分录 / Ledger entry for an approved issuance.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    """

    request_id: UUID
    """@brief 请求标识 / Request identity."""

    requester_id: int
    """@brief 申请用户 / Requesting user."""

    requested_amount: TokenAmount
    """@brief 申请的正数金额 / Requested positive amount."""

    requested_bucket: TokenBucket
    """@brief 所请求钱包 / Requested wallet bucket."""

    purpose: str
    """@brief 用户填写的用途 / User-provided purpose."""

    status: TokenRequestStatus
    """@brief 当前审核状态 / Current review status."""

    requested_at: datetime
    """@brief 请求时间 / Request time."""

    reviewed_at: datetime | None = None
    """@brief 可选审核时间 / Optional review time."""

    reviewer_id: int | None = None
    """@brief 可选审核管理员 / Optional reviewing administrator."""

    review_note: str | None = None
    """@brief 可选审核说明 / Optional review note."""

    ledger_entry_id: UUID | None = None
    """@brief 批准发行对应的账本分录 / Ledger entry backing an approved issuance."""

    version: int = 0
    """@brief 乐观并发版本 / Optimistic-concurrency version."""

    def __post_init__(self) -> None:
        """@brief 验证请求状态形状 / Validate request-state shape.

        @return None / None.
        @raise ValueError 请求状态、时间或审核字段不一致时抛出 /
            Raised when status, times, or review fields are inconsistent.
        """

        if self.requester_id <= 0:
            raise ValueError("Token requester must be positive")
        purpose = self.purpose.strip()
        if not 1 <= len(purpose) <= 500:
            raise ValueError("Token request purpose must contain 1-500 characters")
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("Token request time must be timezone-aware")
        if self.version < 0:
            raise ValueError("Token request version cannot be negative")
        terminal = self.status is not TokenRequestStatus.PENDING
        if terminal != (self.reviewed_at is not None):
            raise ValueError("Terminal token requests need exactly one review time")
        if terminal != (self.reviewer_id is not None):
            raise ValueError("Terminal token requests need exactly one reviewer")
        if self.reviewer_id is not None and self.reviewer_id <= 0:
            raise ValueError("Token reviewer must be positive")
        if (
            self.status in {TokenRequestStatus.APPROVED, TokenRequestStatus.REJECTED}
            and self.reviewer_id == self.requester_id
        ):
            raise ValueError("A requester cannot review their own token request")
        if self.reviewed_at is not None:
            if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
                raise ValueError("Token review time must be timezone-aware")
            if self.reviewed_at < self.requested_at:
                raise ValueError("Token review cannot precede the request")
        if self.review_note is not None and len(self.review_note.strip()) > 500:
            raise ValueError("Token review note cannot exceed 500 characters")
        if self.status is TokenRequestStatus.APPROVED:
            if self.ledger_entry_id is None:
                raise ValueError("An approved token request needs a ledger entry")
        elif self.ledger_entry_id is not None:
            raise ValueError(
                "Only an approved token request can reference a ledger entry"
            )
        object.__setattr__(self, "purpose", purpose)
        if self.review_note is not None:
            object.__setattr__(self, "review_note", self.review_note.strip() or None)

    def approve(
        self,
        *,
        reviewer_id: int,
        reviewed_at: datetime,
        ledger_entry_id: UUID,
        note: str | None = None,
    ) -> TokenRequest:
        """@brief 批准待处理请求 / Approve a pending request.

        @param reviewer_id 银行管理员 / Bank administrator.
        @param reviewed_at 审核时刻 / Review instant.
        @param ledger_entry_id 批准发行分录 / Issuance ledger entry.
        @param note 可选审核说明 / Optional review note.
        @return 已批准的新聚合 / New approved aggregate.
        @raise ValueError 请求不再待处理时抛出 / Raised when the request is no longer pending.
        """

        return self._review(
            status=TokenRequestStatus.APPROVED,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            ledger_entry_id=ledger_entry_id,
            note=note,
        )

    def reject(
        self,
        *,
        reviewer_id: int,
        reviewed_at: datetime,
        note: str | None = None,
    ) -> TokenRequest:
        """@brief 拒绝待处理请求 / Reject a pending request.

        @param reviewer_id 银行管理员 / Bank administrator.
        @param reviewed_at 审核时刻 / Review instant.
        @param note 可选审核说明 / Optional review note.
        @return 已拒绝的新聚合 / New rejected aggregate.
        @raise ValueError 请求不再待处理时抛出 / Raised when the request is no longer pending.
        """

        return self._review(
            status=TokenRequestStatus.REJECTED,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            ledger_entry_id=None,
            note=note,
        )

    def cancel(self, *, requester_id: int, cancelled_at: datetime) -> TokenRequest:
        """@brief 由申请者取消待处理请求 / Cancel a pending request as its requester.

        @param requester_id 取消操作用户 / Cancelling user.
        @param cancelled_at 取消时刻 / Cancellation instant.
        @return 已取消的新聚合 / New cancelled aggregate.
        @raise ValueError 非申请者或请求不待处理时抛出 /
            Raised when caller is not requester or request is not pending.
        """

        if requester_id != self.requester_id:
            raise ValueError("Only the token requester can cancel the request")
        return self._review(
            status=TokenRequestStatus.CANCELLED,
            reviewer_id=requester_id,
            reviewed_at=cancelled_at,
            ledger_entry_id=None,
            note="Cancelled by requester",
        )

    def _review(
        self,
        *,
        status: TokenRequestStatus,
        reviewer_id: int,
        reviewed_at: datetime,
        ledger_entry_id: UUID | None,
        note: str | None,
    ) -> TokenRequest:
        """@brief 执行终态变迁 / Perform a terminal-state transition.

        @param status 目标终态 / Target terminal status.
        @param reviewer_id 审核用户 / Reviewing user.
        @param reviewed_at 审核时刻 / Review instant.
        @param ledger_entry_id 批准发行分录 / Issuance ledger entry.
        @param note 审核说明 / Review note.
        @return 终态请求 / Terminal request.
        @raise ValueError 目标不是终态或请求不待处理时抛出 /
            Raised when target is not terminal or request is not pending.
        """

        if self.status is not TokenRequestStatus.PENDING:
            raise ValueError("Only pending token requests can be reviewed")
        if status is TokenRequestStatus.PENDING:
            raise ValueError("Token review must be terminal")
        return TokenRequest(
            request_id=self.request_id,
            requester_id=self.requester_id,
            requested_amount=self.requested_amount,
            requested_bucket=self.requested_bucket,
            purpose=self.purpose,
            status=status,
            requested_at=self.requested_at,
            reviewed_at=reviewed_at,
            reviewer_id=reviewer_id,
            review_note=note,
            ledger_entry_id=ledger_entry_id,
            version=self.version + 1,
        )
