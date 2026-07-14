"""@brief Billing 订单与支付事件状态机 / Billing order and payment-event state machine."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from ._validation import (
    normalize_instant,
    normalize_reference,
    require_positive_identity,
)
from .catalog import PaymentAmount, ProductKind

_PAID_ORDER_STATUSES: Final = frozenset(
    {
        "paid",
        "fulfilled",
        "refund_pending",
        "refunded",
        "chargeback",
    }
)
"""@brief 必须带有成功支付证据的订单状态 / Order states that require successful-payment evidence."""


class PaymentProvider(StrEnum):
    """@brief 支付事件来源渠道 / Source provider of a payment event."""

    TELEGRAM_STARS = "telegram_stars"
    """@brief Telegram Stars 支付 / Telegram Stars payment."""

    EXTERNAL = "external"
    """@brief 已验证的外部支付渠道 / Verified external payment provider."""

    BACKOFFICE = "backoffice"
    """@brief 受控后台结算渠道 / Controlled back-office settlement provider."""


class PaymentEventKind(StrEnum):
    """@brief 支付渠道通知的业务类型 / Business type emitted by a payment provider."""

    PAYMENT_SUCCEEDED = "payment_succeeded"
    """@brief 已捕获完整付款 / Full payment was captured."""

    PAYMENT_FAILED = "payment_failed"
    """@brief 一次付款尝试失败 / One payment attempt failed."""

    REFUND_SUCCEEDED = "refund_succeeded"
    """@brief 已完成退款 / A refund was completed."""

    REFUND_FAILED = "refund_failed"
    """@brief 一次退款尝试失败 / One refund attempt failed."""

    CHARGEBACK_OPENED = "chargeback_opened"
    """@brief 付款方发起争议或拒付 / Payer opened a dispute or chargeback."""


class OrderStatus(StrEnum):
    """@brief 订单履约与资金状态 / Order fulfillment and money state."""

    AWAITING_PAYMENT = "awaiting_payment"
    """@brief 已创建、尚未收到付款 / Created but payment has not been received."""

    PAID = "paid"
    """@brief 已收到完整付款、尚未履约 / Full payment received but not yet fulfilled."""

    FULFILLED = "fulfilled"
    """@brief 权益已原子交付 / Entitlements were fulfilled atomically."""

    CANCELLED = "cancelled"
    """@brief 未付款订单已取消 / Unpaid order was cancelled."""

    REFUND_PENDING = "refund_pending"
    """@brief 已申请退款、等待支付渠道结算 / Refund was requested and awaits provider settlement."""

    REFUNDED = "refunded"
    """@brief 退款已经确认完成 / Refund was confirmed completed."""

    CHARGEBACK = "chargeback"
    """@brief 已收到争议或拒付通知 / A dispute or chargeback notification was received."""


@dataclass(frozen=True, slots=True)
class PaymentEvent:
    """@brief 已验真的支付渠道事件 / Verified event emitted by a payment provider.

    @param event_id 本地稳定事件标识 / Local stable event identity.
    @param provider 支付渠道 / Payment provider.
    @param provider_event_id 渠道事件唯一参考号 / Provider-unique event reference.
    @param provider_payment_id 原始付款参考号 / Original provider payment reference.
    @param order_id 所属订单标识 / Owning order identity.
    @param kind 支付事件类型 / Payment event type.
    @param amount 渠道原生金额 / Provider-native amount.
    @param occurred_at 渠道发生时刻 / Provider occurrence instant.
    @note 外层适配器必须在构造此对象之前验证签名、来源和重放保护。/
        An outer adapter must validate signatures, provenance, and replay protection before
        constructing this object.
    """

    event_id: UUID
    """@brief 本地稳定事件标识 / Local stable event identity."""

    provider: PaymentProvider
    """@brief 支付渠道 / Payment provider."""

    provider_event_id: str
    """@brief 渠道事件唯一参考号 / Provider-unique event reference."""

    provider_payment_id: str
    """@brief 原始付款参考号 / Original provider payment reference."""

    order_id: UUID
    """@brief 所属订单标识 / Owning order identity."""

    kind: PaymentEventKind
    """@brief 事件类型 / Event type."""

    amount: PaymentAmount
    """@brief 原生支付金额 / Native payment amount."""

    occurred_at: datetime
    """@brief 渠道发生时刻 / Provider occurrence instant."""

    def __post_init__(self) -> None:
        """@brief 规范化外部事件字段 / Normalize external event fields.

        @return None / None.
        @raise TypeError 渠道、类型或金额类型非法时抛出 /
            Raised when provider, kind, or amount has an invalid type.
        @raise ValueError 外部引用或时刻非法时抛出 /
            Raised when external references or instant are invalid.
        """

        if not isinstance(self.provider, PaymentProvider):
            raise TypeError("Payment provider must be a PaymentProvider")
        if not isinstance(self.kind, PaymentEventKind):
            raise TypeError("Payment event kind must be a PaymentEventKind")
        if not isinstance(self.amount, PaymentAmount):
            raise TypeError("Payment event amount must be a PaymentAmount")
        object.__setattr__(
            self,
            "provider_event_id",
            normalize_reference(self.provider_event_id, field="Provider event ID"),
        )
        object.__setattr__(
            self,
            "provider_payment_id",
            normalize_reference(self.provider_payment_id, field="Provider payment ID"),
        )
        object.__setattr__(
            self,
            "occurred_at",
            normalize_instant(self.occurred_at, field="Payment event time"),
        )

    @property
    def receipt_key(self) -> str:
        """@brief 返回渠道范围内的去重键 / Return a provider-scoped deduplication key.

        @return 渠道加事件参考号 / Provider plus event reference.
        """

        return f"{self.provider.value}:{self.provider_event_id}"


@dataclass(frozen=True, slots=True)
class Order:
    """@brief 付款与履约分离的 Billing 订单 / Billing order separating payment from fulfillment.

    @param order_id 订单稳定标识 / Stable order identity.
    @param buyer_id 购买用户 / Purchasing user.
    @param product_id 产品标识 / Product identity.
    @param offer_id 报价标识 / Offer identity.
    @param product_kind 下单时冻结的产品形态 / Product kind frozen at order placement.
    @param price 下单时冻结的原生价格 / Native price frozen at order placement.
    @param status 订单状态 / Order status.
    @param created_at 创建时刻 / Creation instant.
    @param renewal_subscription_id 可选待续费订阅标识 / Optional subscription identity being renewed.
    @param payment_provider 可选成功付款渠道 / Optional successful payment provider.
    @param provider_payment_id 可选成功付款参考号 / Optional successful payment reference.
    @param paid_at 可选付款成功时刻 / Optional payment-success instant.
    @param fulfilled_at 可选权益履约时刻 / Optional entitlement-fulfillment instant.
    @param refund_requested_at 可选退款申请时刻 / Optional refund-request instant.
    @param refunded_at 可选退款完成时刻 / Optional refund-completion instant.
    @param cancelled_at 可选取消时刻 / Optional cancellation instant.
    @param chargeback_at 可选争议时刻 / Optional chargeback instant.
    @param processed_event_keys 已处理渠道事件键 / Processed provider-event keys.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    """

    order_id: UUID
    """@brief 订单稳定标识 / Stable order identity."""

    buyer_id: int
    """@brief 购买用户 / Purchasing user."""

    product_id: UUID
    """@brief 产品标识 / Product identity."""

    offer_id: UUID
    """@brief 报价标识 / Offer identity."""

    product_kind: ProductKind
    """@brief 下单时冻结的产品形态 / Product kind frozen at order placement."""

    price: PaymentAmount
    """@brief 下单时冻结的原生价格 / Native price frozen at order placement."""

    status: OrderStatus
    """@brief 当前订单状态 / Current order status."""

    created_at: datetime
    """@brief 创建时刻 / Creation instant."""

    renewal_subscription_id: UUID | None = None
    """@brief 可选待续费订阅标识 / Optional subscription identity being renewed."""

    payment_provider: PaymentProvider | None = None
    """@brief 成功付款渠道 / Successful payment provider."""

    provider_payment_id: str | None = None
    """@brief 成功付款参考号 / Successful payment reference."""

    paid_at: datetime | None = None
    """@brief 付款成功时刻 / Payment-success instant."""

    fulfilled_at: datetime | None = None
    """@brief 权益履约时刻 / Entitlement-fulfillment instant."""

    refund_requested_at: datetime | None = None
    """@brief 退款申请时刻 / Refund-request instant."""

    refunded_at: datetime | None = None
    """@brief 退款完成时刻 / Refund-completion instant."""

    cancelled_at: datetime | None = None
    """@brief 取消时刻 / Cancellation instant."""

    chargeback_at: datetime | None = None
    """@brief 争议或拒付时刻 / Chargeback instant."""

    processed_event_keys: frozenset[str] = frozenset()
    """@brief 已处理渠道事件去重键 / Processed provider-event deduplication keys."""

    version: int = 0
    """@brief 乐观并发版本 / Optimistic-concurrency version."""

    def __post_init__(self) -> None:
        """@brief 验证订单状态形状 / Validate order-state shape.

        @return None / None.
        @raise TypeError 产品、价格、状态或版本类型非法时抛出 /
            Raised for invalid product, price, status, or version types.
        @raise ValueError 订单生命周期字段不一致时抛出 /
            Raised when order lifecycle fields are inconsistent.
        """

        require_positive_identity(self.buyer_id, field="Order buyer")
        if not isinstance(self.product_kind, ProductKind):
            raise TypeError("Order product kind must be a ProductKind")
        if not isinstance(self.price, PaymentAmount):
            raise TypeError("Order price must be a PaymentAmount")
        if not isinstance(self.status, OrderStatus):
            raise TypeError("Order status must be an OrderStatus")
        if (
            self.renewal_subscription_id is not None
            and self.product_kind is not ProductKind.SUBSCRIPTION
        ):
            raise ValueError("Only subscription orders can renew a subscription")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Order version must be an integer")
        if self.version < 0:
            raise ValueError("Order version cannot be negative")
        created_at = normalize_instant(self.created_at, field="Order creation time")
        paid_at = _optional_instant(self.paid_at, field="Order payment time")
        fulfilled_at = _optional_instant(
            self.fulfilled_at,
            field="Order fulfillment time",
        )
        refund_requested_at = _optional_instant(
            self.refund_requested_at,
            field="Order refund-request time",
        )
        refunded_at = _optional_instant(self.refunded_at, field="Order refund time")
        cancelled_at = _optional_instant(
            self.cancelled_at,
            field="Order cancellation time",
        )
        chargeback_at = _optional_instant(
            self.chargeback_at,
            field="Order chargeback time",
        )
        if (self.payment_provider is None) != (self.provider_payment_id is None):
            raise ValueError(
                "Order payment provider and reference must appear together"
            )
        if self.provider_payment_id is not None:
            object.__setattr__(
                self,
                "provider_payment_id",
                normalize_reference(
                    self.provider_payment_id,
                    field="Order provider payment ID",
                ),
            )
        payment_evidence = self.status.value in _PAID_ORDER_STATUSES
        if payment_evidence != (
            self.payment_provider is not None and paid_at is not None
        ):
            raise ValueError("Paid order states require exactly one payment evidence")
        if paid_at is not None and paid_at < created_at:
            raise ValueError("Order payment cannot precede creation")
        _validate_order_timeline(
            status=self.status,
            created_at=created_at,
            paid_at=paid_at,
            fulfilled_at=fulfilled_at,
            refund_requested_at=refund_requested_at,
            refunded_at=refunded_at,
            cancelled_at=cancelled_at,
            chargeback_at=chargeback_at,
        )
        event_keys = frozenset(
            normalize_reference(key, field="Processed payment event key")
            for key in self.processed_event_keys
        )
        if len(event_keys) != len(self.processed_event_keys):
            raise ValueError("Processed payment event keys must be unique")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "paid_at", paid_at)
        object.__setattr__(self, "fulfilled_at", fulfilled_at)
        object.__setattr__(self, "refund_requested_at", refund_requested_at)
        object.__setattr__(self, "refunded_at", refunded_at)
        object.__setattr__(self, "cancelled_at", cancelled_at)
        object.__setattr__(self, "chargeback_at", chargeback_at)
        object.__setattr__(self, "processed_event_keys", event_keys)

    @classmethod
    def create(
        cls,
        *,
        order_id: UUID,
        buyer_id: int,
        product_id: UUID,
        offer_id: UUID,
        product_kind: ProductKind,
        price: PaymentAmount,
        created_at: datetime,
        renewal_subscription_id: UUID | None = None,
    ) -> Order:
        """@brief 创建待付款订单 / Create an awaiting-payment order.

        @param order_id 订单稳定标识 / Stable order identity.
        @param buyer_id 购买用户 / Purchasing user.
        @param product_id 产品标识 / Product identity.
        @param offer_id 报价标识 / Offer identity.
        @param product_kind 冻结产品形态 / Frozen product kind.
        @param price 冻结原生价格 / Frozen native price.
        @param created_at 创建时刻 / Creation instant.
        @param renewal_subscription_id 可选待续费订阅标识 / Optional subscription identity being renewed.
        @return 待付款订单 / Awaiting-payment order.
        """

        return cls(
            order_id=order_id,
            buyer_id=buyer_id,
            product_id=product_id,
            offer_id=offer_id,
            product_kind=product_kind,
            price=price,
            status=OrderStatus.AWAITING_PAYMENT,
            created_at=created_at,
            renewal_subscription_id=renewal_subscription_id,
        )

    def apply_payment_event(self, event: PaymentEvent) -> Order:
        """@brief 应用付款或争议通知 / Apply a payment or chargeback notification.

        @param event 已验真的渠道事件 / Verified provider event.
        @return 变迁后的订单；同一事件重放时返回原对象 /
            Transitioned order; returns this object when the event is replayed.
        @raise ValueError 事件不属于订单、金额不匹配或状态不允许时抛出 /
            Raised when event ownership, amount, or state is invalid.
        @note 退款事件必须通过 Refund 聚合和 ``resolve_refund`` 一起原子处理。/
            Refund events must be atomically processed with a Refund aggregate via
            ``resolve_refund``.
        """

        self._validate_event(event)
        if event.receipt_key in self.processed_event_keys:
            return self
        if event.kind is PaymentEventKind.PAYMENT_SUCCEEDED:
            if self.status is not OrderStatus.AWAITING_PAYMENT:
                raise ValueError("Only awaiting-payment orders can receive a payment")
            if event.occurred_at < self.created_at:
                raise ValueError("Payment event cannot precede order creation")
            return replace(
                self,
                status=OrderStatus.PAID,
                payment_provider=event.provider,
                provider_payment_id=event.provider_payment_id,
                paid_at=event.occurred_at,
                processed_event_keys=self._with_event(event),
                version=self.version + 1,
            )
        if event.kind is PaymentEventKind.PAYMENT_FAILED:
            if self.status is not OrderStatus.AWAITING_PAYMENT:
                raise ValueError(
                    "Only awaiting-payment orders can record a payment failure"
                )
            return replace(
                self,
                processed_event_keys=self._with_event(event),
                version=self.version + 1,
            )
        if event.kind is PaymentEventKind.CHARGEBACK_OPENED:
            if self.status not in {
                OrderStatus.PAID,
                OrderStatus.FULFILLED,
                OrderStatus.REFUND_PENDING,
                OrderStatus.REFUNDED,
            }:
                raise ValueError("Chargebacks require a previously paid order")
            self._validate_original_payment_reference(event)
            assert self.paid_at is not None
            if event.occurred_at < self.paid_at:
                raise ValueError("Chargeback cannot precede successful payment")
            return replace(
                self,
                status=OrderStatus.CHARGEBACK,
                chargeback_at=event.occurred_at,
                processed_event_keys=self._with_event(event),
                version=self.version + 1,
            )
        raise ValueError("Refund events must be resolved through a Refund aggregate")

    def mark_fulfilled(self, *, fulfilled_at: datetime) -> Order:
        """@brief 标记已付款订单完成履约 / Mark a paid order as fulfilled.

        @param fulfilled_at 权益原子交付完成时刻 / Entitlement atomic-fulfillment instant.
        @return 已履约订单 / Fulfilled order.
        @raise ValueError 订单尚未付款或履约时刻非法时抛出 /
            Raised when the order is unpaid or fulfillment time is invalid.
        """

        if self.status is not OrderStatus.PAID:
            raise ValueError("Only paid orders can be fulfilled")
        normalized = normalize_instant(fulfilled_at, field="Order fulfillment time")
        assert self.paid_at is not None
        if normalized < self.paid_at:
            raise ValueError("Fulfillment cannot precede successful payment")
        return replace(
            self,
            status=OrderStatus.FULFILLED,
            fulfilled_at=normalized,
            version=self.version + 1,
        )

    def cancel(self, *, cancelled_at: datetime) -> Order:
        """@brief 取消尚未付款的订单 / Cancel an unpaid order.

        @param cancelled_at 取消时刻 / Cancellation instant.
        @return 已取消订单 / Cancelled order.
        @raise ValueError 订单已收到付款或时刻非法时抛出 /
            Raised when payment was received or the instant is invalid.
        """

        if self.status is not OrderStatus.AWAITING_PAYMENT:
            raise ValueError("Only awaiting-payment orders can be cancelled")
        normalized = normalize_instant(cancelled_at, field="Order cancellation time")
        if normalized < self.created_at:
            raise ValueError("Cancellation cannot precede order creation")
        return replace(
            self,
            status=OrderStatus.CANCELLED,
            cancelled_at=normalized,
            version=self.version + 1,
        )

    def request_refund(self, *, requested_at: datetime) -> Order:
        """@brief 进入等待退款结算状态 / Enter the refund-pending state.

        @param requested_at 退款申请时刻 / Refund-request instant.
        @return 等待退款结算的订单 / Refund-pending order.
        @raise ValueError 订单尚未履约或时刻非法时抛出 /
            Raised when the order is not fulfilled or the instant is invalid.
        """

        if self.status is not OrderStatus.FULFILLED:
            raise ValueError("Only fulfilled orders can request refunds")
        normalized = normalize_instant(requested_at, field="Order refund-request time")
        assert self.fulfilled_at is not None
        if normalized < self.fulfilled_at:
            raise ValueError("Refund request cannot precede fulfillment")
        return replace(
            self,
            status=OrderStatus.REFUND_PENDING,
            refund_requested_at=normalized,
            version=self.version + 1,
        )

    def resolve_refund(self, event: PaymentEvent) -> Order:
        """@brief 以已验真的退款事件结算订单 / Settle an order with a verified refund event.

        @param event 已验真的退款渠道事件 / Verified refund-provider event.
        @return 已退款订单或恢复履约的订单 / Refunded order or restored fulfilled order.
        @raise ValueError 退款事件不匹配或订单不等待退款时抛出 /
            Raised for mismatched refund events or non-pending orders.
        """

        self._validate_event(event)
        if event.receipt_key in self.processed_event_keys:
            return self
        if event.kind not in {
            PaymentEventKind.REFUND_SUCCEEDED,
            PaymentEventKind.REFUND_FAILED,
        }:
            raise ValueError("Only refund events can resolve a refund")
        if self.status is not OrderStatus.REFUND_PENDING:
            raise ValueError("Only refund-pending orders can resolve refunds")
        self._validate_original_payment_reference(event)
        assert self.refund_requested_at is not None
        if event.occurred_at < self.refund_requested_at:
            raise ValueError("Refund settlement cannot precede the refund request")
        if event.kind is PaymentEventKind.REFUND_SUCCEEDED:
            return replace(
                self,
                status=OrderStatus.REFUNDED,
                refunded_at=event.occurred_at,
                processed_event_keys=self._with_event(event),
                version=self.version + 1,
            )
        return replace(
            self,
            status=OrderStatus.FULFILLED,
            processed_event_keys=self._with_event(event),
            version=self.version + 1,
        )

    def reject_refund(self, *, reviewed_at: datetime) -> Order:
        """@brief 在退款审核拒绝后恢复订单履约状态 / Restore fulfilled order state after a refund review is rejected.

        @param reviewed_at 退款审核拒绝时刻 / Refund-rejection review instant.
        @return 恢复为已履约的订单 / Order restored to fulfilled.
        @raise ValueError 订单不等待退款或审核时刻非法时抛出 /
            Raised when order is not refund pending or review time is invalid.
        @note 调用方必须与 ``Refund.reject`` 在同一事务中执行。/
            The caller must execute this with ``Refund.reject`` in one transaction.
        """

        if self.status is not OrderStatus.REFUND_PENDING:
            raise ValueError("Only refund-pending orders can reject refunds")
        normalized = normalize_instant(reviewed_at, field="Refund rejection time")
        assert self.refund_requested_at is not None
        if normalized < self.refund_requested_at:
            raise ValueError("Refund rejection cannot precede the refund request")
        return replace(
            self,
            status=OrderStatus.FULFILLED,
            version=self.version + 1,
        )

    def _validate_event(self, event: PaymentEvent) -> None:
        """@brief 校验事件与订单的归属和金额 / Validate event ownership and amount against the order.

        @param event 待应用的支付事件 / Payment event to apply.
        @return None / None.
        @raise ValueError 事件不属于本订单或金额不匹配时抛出 /
            Raised when event does not own this order or its amount differs.
        """

        if event.order_id != self.order_id:
            raise ValueError("Payment event does not belong to this order")
        if event.amount != self.price:
            raise ValueError("Payment event amount must exactly match the order price")

    def _with_event(self, event: PaymentEvent) -> frozenset[str]:
        """@brief 为状态快照追加去重键 / Add a deduplication key to the state snapshot.

        @param event 已处理支付事件 / Processed payment event.
        @return 新的不可变事件键集合 / New immutable event-key set.
        """

        return self.processed_event_keys | {event.receipt_key}

    def _validate_original_payment_reference(self, event: PaymentEvent) -> None:
        """@brief 校验事件引用的是订单的原始付款 / Validate that an event references the order's original payment.

        @param event 争议或退款渠道事件 / Chargeback or refund provider event.
        @return None / None.
        @raise ValueError 渠道或付款参考号不匹配时抛出 /
            Raised when provider or payment reference does not match.
        """

        if (
            event.provider is not self.payment_provider
            or event.provider_payment_id != self.provider_payment_id
        ):
            raise ValueError(
                "Payment settlement must reference the original successful payment"
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


def _validate_order_timeline(
    *,
    status: OrderStatus,
    created_at: datetime,
    paid_at: datetime | None,
    fulfilled_at: datetime | None,
    refund_requested_at: datetime | None,
    refunded_at: datetime | None,
    cancelled_at: datetime | None,
    chargeback_at: datetime | None,
) -> None:
    """@brief 验证订单状态与时间线的一致性 / Validate consistency of order status and timeline.

    @param status 当前订单状态 / Current order status.
    @param created_at 创建时刻 / Creation instant.
    @param paid_at 成功付款时刻 / Successful payment instant.
    @param fulfilled_at 履约时刻 / Fulfillment instant.
    @param refund_requested_at 退款申请时刻 / Refund-request instant.
    @param refunded_at 退款完成时刻 / Refund-completion instant.
    @param cancelled_at 取消时刻 / Cancellation instant.
    @param chargeback_at 争议时刻 / Chargeback instant.
    @return None / None.
    @raise ValueError 状态所需字段缺失或时间顺序非法时抛出 /
        Raised when state-required fields are absent or time ordering is invalid.
    """

    if status is OrderStatus.AWAITING_PAYMENT:
        if any(
            instant is not None
            for instant in (
                paid_at,
                fulfilled_at,
                refund_requested_at,
                refunded_at,
                cancelled_at,
                chargeback_at,
            )
        ):
            raise ValueError(
                "Awaiting-payment orders cannot contain lifecycle timestamps"
            )
        return
    if status is OrderStatus.CANCELLED:
        if cancelled_at is None or cancelled_at < created_at:
            raise ValueError("Cancelled orders require a valid cancellation time")
        if any(
            instant is not None
            for instant in (
                paid_at,
                fulfilled_at,
                refund_requested_at,
                refunded_at,
                chargeback_at,
            )
        ):
            raise ValueError(
                "Cancelled orders cannot contain payment lifecycle timestamps"
            )
        return
    assert paid_at is not None
    if status is OrderStatus.PAID:
        if any(
            instant is not None
            for instant in (
                fulfilled_at,
                refund_requested_at,
                refunded_at,
                cancelled_at,
                chargeback_at,
            )
        ):
            raise ValueError("Paid orders cannot contain later lifecycle timestamps")
        return
    if status is OrderStatus.FULFILLED:
        if fulfilled_at is None or fulfilled_at < paid_at:
            raise ValueError("Fulfilled orders require a valid fulfillment time")
        if (
            refunded_at is not None
            or cancelled_at is not None
            or chargeback_at is not None
        ):
            raise ValueError(
                "Fulfilled orders cannot contain terminal settlement timestamps"
            )
        if refund_requested_at is not None and refund_requested_at < fulfilled_at:
            raise ValueError("Refund request cannot precede fulfillment")
        return
    if status is OrderStatus.REFUND_PENDING:
        if fulfilled_at is None or refund_requested_at is None:
            raise ValueError(
                "Refund-pending orders require fulfillment and request times"
            )
        if fulfilled_at < paid_at or refund_requested_at < fulfilled_at:
            raise ValueError("Refund-pending order timeline is invalid")
        if (
            refunded_at is not None
            or cancelled_at is not None
            or chargeback_at is not None
        ):
            raise ValueError("Refund-pending orders cannot contain terminal timestamps")
        return
    if status is OrderStatus.REFUNDED:
        if fulfilled_at is None or refund_requested_at is None or refunded_at is None:
            raise ValueError(
                "Refunded orders require fulfillment and refund timestamps"
            )
        if not paid_at <= fulfilled_at <= refund_requested_at <= refunded_at:
            raise ValueError("Refunded order timeline is invalid")
        if cancelled_at is not None or chargeback_at is not None:
            raise ValueError(
                "Refunded orders cannot contain cancellation or chargeback times"
            )
        return
    if status is OrderStatus.CHARGEBACK:
        if chargeback_at is None or chargeback_at < paid_at:
            raise ValueError("Chargeback orders require a valid chargeback time")
        if cancelled_at is not None:
            raise ValueError("Chargeback orders cannot be cancelled")
        if fulfilled_at is not None and fulfilled_at < paid_at:
            raise ValueError("Chargeback fulfillment cannot precede payment")
        if refund_requested_at is not None:
            if fulfilled_at is None or refund_requested_at < fulfilled_at:
                raise ValueError("Chargeback refund request timeline is invalid")
        if refunded_at is not None:
            if refund_requested_at is None or refunded_at < refund_requested_at:
                raise ValueError("Chargeback refund timeline is invalid")
        return
    raise ValueError("Unsupported order status")
