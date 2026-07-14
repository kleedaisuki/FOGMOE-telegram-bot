"""@brief Billing 应用层外部能力端口 / External capability ports for the Billing application layer."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fogmoe_bot.application.billing.models import (
    BillingResult,
    CancelSubscription,
    FulfillOrder,
    PlaceOrder,
    RecordPaymentEvent,
    RequestRefund,
    ReviewRefund,
    SettleRefund,
)
from fogmoe_bot.domain.billing.entitlements import EntitlementGrant
from fogmoe_bot.domain.billing.orders import PaymentEvent


class PaymentEventVerifier(Protocol):
    """@brief 验证外部支付通知的能力 / Capability that verifies external payment notifications.

    @note 实现必须验证渠道签名、来源和渠道事件去重；不能相信 Telegram 普通消息或
        用户提供的字段。/ Implementations must validate provider signatures, origin, and
        provider-event deduplication; they must not trust ordinary Telegram messages or user fields.
    """

    async def verify(self, event: PaymentEvent) -> bool:
        """@brief 验证一个支付渠道事件 / Verify one payment-provider event.

        @param event 待验证事件 / Event to verify.
        @return 已验证为 True / True when verified.
        """

        ...


class BillingOperations(Protocol):
    """@brief 保持跨聚合原子性的 Billing 持久化能力 / Billing persistence capability preserving cross-aggregate atomicity.

    @note 付款成功、权益授予、订阅创建；退款成功、权益撤销、订阅撤销都必须是单一事务。
        / Payment success, entitlement granting, and subscription creation; as well as refund
        success, entitlement revocation, and subscription revocation must each be one transaction.
    """

    async def place_order(self, command: PlaceOrder) -> BillingResult:
        """@brief 创建或重放订单 / Create or replay an order.

        @param command 下单命令 / Order-placement command.
        @return 稳定订单结果 / Stable order result.
        """

        ...

    async def record_payment_event(self, command: RecordPaymentEvent) -> BillingResult:
        """@brief 记录已验证的付款或争议事件 / Record a verified payment or chargeback event.

        @param command 支付事件命令 / Payment-event command.
        @return 稳定支付状态结果 / Stable payment-state result.
        """

        ...

    async def fulfill_order(self, command: FulfillOrder) -> BillingResult:
        """@brief 原子履约订单并授予权益 / Atomically fulfill an order and grant entitlements.

        @param command 履约命令 / Fulfillment command.
        @return 订单、权益与可选订阅结果 / Order, entitlement, and optional subscription result.
        """

        ...

    async def request_refund(self, command: RequestRefund) -> BillingResult:
        """@brief 原子创建退款并标记订单等待退款 / Atomically create refund and mark order refund pending.

        @param command 退款申请命令 / Refund-request command.
        @return 订单和退款结果 / Order and refund result.
        """

        ...

    async def review_refund(self, command: ReviewRefund) -> BillingResult:
        """@brief 审核退款并在拒绝时恢复订单 / Review refund and restore order on rejection.

        @param command 退款审核命令 / Refund-review command.
        @return 订单和退款结果 / Order and refund result.
        """

        ...

    async def settle_refund(self, command: SettleRefund) -> BillingResult:
        """@brief 原子结算退款与关联权益 / Atomically settle refund and associated entitlements.

        @param command 退款结算命令 / Refund-settlement command.
        @return 订单、退款、权益与可选订阅结果 / Order, refund, entitlement, and optional subscription result.
        """

        ...

    async def cancel_subscription(self, command: CancelSubscription) -> BillingResult:
        """@brief 请求订阅按期结束 / Request subscription end of period.

        @param command 订阅取消命令 / Subscription-cancellation command.
        @return 订阅结果 / Subscription result.
        """

        ...

    async def active_user_entitlements(
        self,
        user_id: int,
        *,
        observed_at: datetime,
    ) -> tuple[EntitlementGrant, ...]:
        """@brief 读取指定时刻有效的用户权益 / Read user entitlements active at an instant.

        @param user_id 用户标识 / User identity.
        @param observed_at 观察时刻 / Observation instant.
        @return 有效权益快照 / Active entitlement snapshots.
        """

        ...
