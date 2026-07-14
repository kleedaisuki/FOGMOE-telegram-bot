"""@brief Billing 退款聚合状态机 / Billing refund aggregate state machine."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from ._validation import (
    normalize_instant,
    normalize_reference,
    normalize_text,
    require_positive_identity,
)
from .catalog import PaymentAmount
from .orders import PaymentEvent, PaymentEventKind, PaymentProvider


class RefundStatus(StrEnum):
    """@brief 用户退款申请的状态 / Status of a user refund request."""

    REQUESTED = "requested"
    """@brief 用户已经申请、等待审核 / Requested by user and awaits review."""

    APPROVED = "approved"
    """@brief 已审核通过、等待渠道退款 / Reviewed positively and awaits provider refund."""

    REJECTED = "rejected"
    """@brief 已审核拒绝 / Reviewed negatively."""

    SUCCEEDED = "succeeded"
    """@brief 支付渠道已确认退款 / Payment provider confirmed the refund."""

    FAILED = "failed"
    """@brief 支付渠道未能完成本次退款 / Payment provider did not complete this refund attempt."""

    CANCELLED = "cancelled"
    """@brief 申请者在审核前撤销 / Requester withdrew before review."""


@dataclass(frozen=True, slots=True)
class Refund:
    """@brief 与订单并列持久化的退款申请 / Refund request persisted alongside an order.

    @param refund_id 退款稳定标识 / Stable refund identity.
    @param order_id 所属订单标识 / Owning order identity.
    @param requester_id 申请退款的用户 / User requesting the refund.
    @param amount 请求退款的原生金额 / Native amount requested for refund.
    @param reason 申请原因 / Request reason.
    @param status 退款状态 / Refund status.
    @param requested_at 申请时刻 / Request instant.
    @param reviewer_id 可选审核人员 / Optional reviewer identity.
    @param reviewed_at 可选审核时刻 / Optional review instant.
    @param review_note 可选审核说明 / Optional review note.
    @param settlement_provider 可选结算渠道 / Optional settlement provider.
    @param provider_settlement_id 可选渠道结算参考号 / Optional provider settlement reference.
    @param settled_at 可选结算时刻 / Optional settlement instant.
    @param cancelled_at 可选撤销时刻 / Optional cancellation instant.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @note 退款状态与对应 Order 的 ``refund_pending``/``refunded`` 状态必须在同一事务内推进。/
        Refund state and its Order's ``refund_pending``/``refunded`` state must advance in
        one transaction.
    """

    refund_id: UUID
    """@brief 退款稳定标识 / Stable refund identity."""

    order_id: UUID
    """@brief 所属订单标识 / Owning order identity."""

    requester_id: int
    """@brief 退款申请用户 / Refund-requesting user."""

    amount: PaymentAmount
    """@brief 原生退款金额 / Native refund amount."""

    reason: str
    """@brief 用户说明的退款原因 / User-provided refund reason."""

    status: RefundStatus
    """@brief 当前退款状态 / Current refund status."""

    requested_at: datetime
    """@brief 退款申请时刻 / Refund-request instant."""

    reviewer_id: int | None = None
    """@brief 可选审核人员 / Optional reviewer identity."""

    reviewed_at: datetime | None = None
    """@brief 可选审核时刻 / Optional review instant."""

    review_note: str | None = None
    """@brief 可选审核说明 / Optional review note."""

    settlement_provider: PaymentProvider | None = None
    """@brief 可选结算渠道 / Optional settlement provider."""

    provider_settlement_id: str | None = None
    """@brief 可选渠道结算参考号 / Optional provider settlement reference."""

    settled_at: datetime | None = None
    """@brief 可选结算时刻 / Optional settlement instant."""

    cancelled_at: datetime | None = None
    """@brief 可选撤销时刻 / Optional cancellation instant."""

    version: int = 0
    """@brief 乐观并发版本 / Optimistic-concurrency version."""

    def __post_init__(self) -> None:
        """@brief 验证退款状态形状 / Validate refund-state shape.

        @return None / None.
        @raise TypeError 金额、状态、渠道或版本类型非法时抛出 /
            Raised for invalid amount, status, provider, or version types.
        @raise ValueError 退款状态字段或时间线不一致时抛出 /
            Raised for inconsistent refund state fields or timeline.
        """

        require_positive_identity(self.requester_id, field="Refund requester")
        if not isinstance(self.amount, PaymentAmount):
            raise TypeError("Refund amount must be a PaymentAmount")
        if not isinstance(self.status, RefundStatus):
            raise TypeError("Refund status must be a RefundStatus")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Refund version must be an integer")
        if self.version < 0:
            raise ValueError("Refund version cannot be negative")
        requested_at = normalize_instant(self.requested_at, field="Refund request time")
        reviewed_at = _optional_instant(self.reviewed_at, field="Refund review time")
        settled_at = _optional_instant(self.settled_at, field="Refund settlement time")
        cancelled_at = _optional_instant(
            self.cancelled_at,
            field="Refund cancellation time",
        )
        if reviewed_at is not None and reviewed_at < requested_at:
            raise ValueError("Refund review cannot precede the request")
        if settled_at is not None:
            if reviewed_at is None or settled_at < reviewed_at:
                raise ValueError("Refund settlement cannot precede review")
        if cancelled_at is not None and cancelled_at < requested_at:
            raise ValueError("Refund cancellation cannot precede the request")
        if self.reviewer_id is not None:
            require_positive_identity(self.reviewer_id, field="Refund reviewer")
            if self.reviewer_id == self.requester_id:
                raise ValueError("Refund requester cannot review their own refund")
        review_note = _optional_note(self.review_note, field="Refund review note")
        if (self.settlement_provider is None) != (self.provider_settlement_id is None):
            raise ValueError(
                "Refund settlement provider and reference must appear together"
            )
        if self.provider_settlement_id is not None:
            if not isinstance(self.settlement_provider, PaymentProvider):
                raise TypeError("Refund settlement provider must be a PaymentProvider")
            object.__setattr__(
                self,
                "provider_settlement_id",
                normalize_reference(
                    self.provider_settlement_id,
                    field="Refund provider settlement ID",
                ),
            )
        _validate_refund_state(
            status=self.status,
            reviewer_id=self.reviewer_id,
            reviewed_at=reviewed_at,
            settled_at=settled_at,
            cancelled_at=cancelled_at,
            settlement_provider=self.settlement_provider,
            provider_settlement_id=self.provider_settlement_id,
        )
        object.__setattr__(
            self,
            "reason",
            normalize_text(
                self.reason,
                field="Refund reason",
                minimum_length=1,
                maximum_length=1_000,
            ),
        )
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "reviewed_at", reviewed_at)
        object.__setattr__(self, "review_note", review_note)
        object.__setattr__(self, "settled_at", settled_at)
        object.__setattr__(self, "cancelled_at", cancelled_at)

    @classmethod
    def request(
        cls,
        *,
        refund_id: UUID,
        order_id: UUID,
        requester_id: int,
        amount: PaymentAmount,
        reason: str,
        requested_at: datetime,
    ) -> Refund:
        """@brief 创建等待审核的退款申请 / Create a refund request awaiting review.

        @param refund_id 退款稳定标识 / Stable refund identity.
        @param order_id 所属订单标识 / Owning order identity.
        @param requester_id 申请用户 / Requesting user.
        @param amount 原生退款金额 / Native refund amount.
        @param reason 用户退款原因 / User refund reason.
        @param requested_at 申请时刻 / Request instant.
        @return 已请求退款聚合 / Requested refund aggregate.
        """

        return cls(
            refund_id=refund_id,
            order_id=order_id,
            requester_id=requester_id,
            amount=amount,
            reason=reason,
            status=RefundStatus.REQUESTED,
            requested_at=requested_at,
        )

    def approve(
        self,
        *,
        reviewer_id: int,
        reviewed_at: datetime,
        note: str | None = None,
    ) -> Refund:
        """@brief 批准待审核退款 / Approve a requested refund.

        @param reviewer_id 审核人员 / Reviewer identity.
        @param reviewed_at 审核时刻 / Review instant.
        @param note 可选审核说明 / Optional review note.
        @return 已批准退款 / Approved refund.
        @raise ValueError 退款不再等待审核时抛出 / Raised when refund is no longer awaiting review.
        """

        return self._review(
            status=RefundStatus.APPROVED,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            note=note,
        )

    def reject(
        self,
        *,
        reviewer_id: int,
        reviewed_at: datetime,
        note: str | None = None,
    ) -> Refund:
        """@brief 拒绝待审核退款 / Reject a requested refund.

        @param reviewer_id 审核人员 / Reviewer identity.
        @param reviewed_at 审核时刻 / Review instant.
        @param note 可选审核说明 / Optional review note.
        @return 已拒绝退款 / Rejected refund.
        @raise ValueError 退款不再等待审核时抛出 / Raised when refund is no longer awaiting review.
        """

        return self._review(
            status=RefundStatus.REJECTED,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            note=note,
        )

    def cancel(self, *, requester_id: int, cancelled_at: datetime) -> Refund:
        """@brief 由申请者撤销待审核退款 / Cancel a requested refund as its requester.

        @param requester_id 执行撤销的用户 / User performing the cancellation.
        @param cancelled_at 撤销时刻 / Cancellation instant.
        @return 已撤销退款 / Cancelled refund.
        @raise ValueError 非申请者撤销或退款不待审核时抛出 /
            Raised when caller is not requester or refund is no longer requested.
        """

        if requester_id != self.requester_id:
            raise ValueError("Only the refund requester can cancel the refund")
        if self.status is not RefundStatus.REQUESTED:
            raise ValueError("Only requested refunds can be cancelled")
        normalized = normalize_instant(cancelled_at, field="Refund cancellation time")
        if normalized < self.requested_at:
            raise ValueError("Refund cancellation cannot precede the request")
        return replace(
            self,
            status=RefundStatus.CANCELLED,
            cancelled_at=normalized,
            version=self.version + 1,
        )

    def settle_from_payment_event(self, event: PaymentEvent) -> Refund:
        """@brief 根据渠道退款事件结束申请 / Finish the request from a provider refund event.

        @param event 已验真的退款事件 / Verified refund event.
        @return 已成功或失败的退款 / Succeeded or failed refund.
        @raise ValueError 退款未经批准、事件不匹配或状态不合法时抛出 /
            Raised when refund is unapproved, event mismatches, or state is invalid.
        """

        if self.status is not RefundStatus.APPROVED:
            raise ValueError("Only approved refunds can be settled")
        if event.order_id != self.order_id:
            raise ValueError("Refund event does not belong to this order")
        if event.amount != self.amount:
            raise ValueError(
                "Refund event amount must exactly match the requested amount"
            )
        if event.kind not in {
            PaymentEventKind.REFUND_SUCCEEDED,
            PaymentEventKind.REFUND_FAILED,
        }:
            raise ValueError("Only refund events can settle a refund")
        assert self.reviewed_at is not None
        if event.occurred_at < self.reviewed_at:
            raise ValueError("Refund settlement cannot precede review")
        terminal_status = (
            RefundStatus.SUCCEEDED
            if event.kind is PaymentEventKind.REFUND_SUCCEEDED
            else RefundStatus.FAILED
        )
        return replace(
            self,
            status=terminal_status,
            settlement_provider=event.provider,
            provider_settlement_id=event.provider_event_id,
            settled_at=event.occurred_at,
            version=self.version + 1,
        )

    def _review(
        self,
        *,
        status: RefundStatus,
        reviewer_id: int,
        reviewed_at: datetime,
        note: str | None,
    ) -> Refund:
        """@brief 执行审核终态变迁 / Perform a review state transition.

        @param status 批准或拒绝目标状态 / Approved or rejected target status.
        @param reviewer_id 审核人员 / Reviewer identity.
        @param reviewed_at 审核时刻 / Review instant.
        @param note 可选审核说明 / Optional review note.
        @return 已审核退款 / Reviewed refund.
        @raise ValueError 状态不是审核终态或退款不待审核时抛出 /
            Raised when target is not a review terminal or refund is not requested.
        """

        if self.status is not RefundStatus.REQUESTED:
            raise ValueError("Only requested refunds can be reviewed")
        if status not in {RefundStatus.APPROVED, RefundStatus.REJECTED}:
            raise ValueError("Refund review must approve or reject")
        return replace(
            self,
            status=status,
            reviewer_id=reviewer_id,
            reviewed_at=reviewed_at,
            review_note=note,
            version=self.version + 1,
        )


def _optional_instant(value: datetime | None, *, field: str) -> datetime | None:
    """@brief 规范化可选时刻 / Normalize an optional instant.

    @param value 可选原始时刻 / Optional raw instant.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @return UTC 时刻或 None / UTC instant or None.
    """

    if value is None:
        return None
    return normalize_instant(value, field=field)


def _optional_note(value: str | None, *, field: str) -> str | None:
    """@brief 规范化可选审核说明 / Normalize an optional review note.

    @param value 可选原始说明 / Optional raw note.
    @param field 出错信息中的字段名称 / Field name for error messages.
    @return 规范说明或 None / Normalized note or None.
    """

    if value is None:
        return None
    normalized = normalize_text(
        value, field=field, minimum_length=1, maximum_length=1_000
    )
    return normalized


def _validate_refund_state(
    *,
    status: RefundStatus,
    reviewer_id: int | None,
    reviewed_at: datetime | None,
    settled_at: datetime | None,
    cancelled_at: datetime | None,
    settlement_provider: PaymentProvider | None,
    provider_settlement_id: str | None,
) -> None:
    """@brief 验证退款状态所需的字段 / Validate fields required by a refund status.

    @param status 当前退款状态 / Current refund status.
    @param reviewer_id 可选审核人员 / Optional reviewer identity.
    @param reviewed_at 可选审核时刻 / Optional review instant.
    @param settled_at 可选结算时刻 / Optional settlement instant.
    @param cancelled_at 可选撤销时刻 / Optional cancellation instant.
    @param settlement_provider 可选结算渠道 / Optional settlement provider.
    @param provider_settlement_id 可选渠道结算参考号 / Optional provider settlement reference.
    @return None / None.
    @raise ValueError 状态需要的字段缺失或不应存在时抛出 /
        Raised when state-required fields are absent or forbidden fields are present.
    """

    reviewed = reviewer_id is not None and reviewed_at is not None
    settled = (
        settlement_provider is not None
        and provider_settlement_id is not None
        and settled_at is not None
    )
    if status is RefundStatus.REQUESTED:
        if reviewed or settled or cancelled_at is not None:
            raise ValueError(
                "Requested refunds cannot contain review or settlement fields"
            )
        return
    if status is RefundStatus.APPROVED:
        if not reviewed or settled or cancelled_at is not None:
            raise ValueError("Approved refunds require review and no settlement")
        return
    if status is RefundStatus.REJECTED:
        if not reviewed or settled or cancelled_at is not None:
            raise ValueError("Rejected refunds require review and no settlement")
        return
    if status in {RefundStatus.SUCCEEDED, RefundStatus.FAILED}:
        if not reviewed or not settled or cancelled_at is not None:
            raise ValueError("Settled refunds require review and settlement fields")
        return
    if status is RefundStatus.CANCELLED:
        if reviewed or settled or cancelled_at is None:
            raise ValueError("Cancelled refunds require only a cancellation time")
        return
    raise ValueError("Unsupported refund status")
