"""@brief Billing 与权益应用服务 / Billing and entitlement application service."""

from __future__ import annotations

from datetime import datetime

from fogmoe_bot.application.billing.models import (
    BillingCode,
    BillingResult,
    CancelSubscription,
    FulfillOrder,
    PlaceOrder,
    RecordPaymentEvent,
    RequestRefund,
    ReviewRefund,
    SettleRefund,
)
from fogmoe_bot.application.billing.ports import BillingOperations, PaymentEventVerifier
from fogmoe_bot.domain.billing.entitlements import EntitlementGrant

BILLING_SERVICE_DATA_KEY = "billing.service"
"""@brief runtime capability 中 Billing 服务的稳定键 / Stable Billing-service key in runtime capabilities."""


class BillingService:
    """@brief 编排支付验证、后台授权和原子 Billing 端口 / Orchestrate payment verification, back-office authorization, and atomic Billing ports.

    @note 此服务不发行金币、不访问银行，也不提供法币或支付金额到金币的映射。/
        This service does not issue tokens, access banking, or map fiat or payment amounts to tokens.
    """

    def __init__(
        self,
        *,
        operations: BillingOperations,
        payment_events: PaymentEventVerifier,
        administrator_id: int,
    ) -> None:
        """@brief 注入 Billing 原子操作、事件验证和管理员身份 / Inject atomic Billing operations, event verification, and administrator identity.

        @param operations 跨聚合原子 Billing 能力 / Cross-aggregate atomic Billing capability.
        @param payment_events 支付渠道事件验证能力 / Payment-provider event verification capability.
        @param administrator_id 受控后台管理员 Telegram ID / Controlled back-office administrator Telegram ID.
        @raise ValueError 管理员标识不为正时抛出 / Raised when administrator identity is not positive.
        """

        if (
            isinstance(administrator_id, bool)
            or not isinstance(administrator_id, int)
            or administrator_id <= 0
        ):
            raise ValueError("Billing administrator must be positive")
        self._operations = operations
        self._payment_events = payment_events
        self._administrator_id = administrator_id

    async def place_order(self, command: PlaceOrder) -> BillingResult:
        """@brief 创建待付款订单 / Create an awaiting-payment order.

        @param command 下单命令 / Order-placement command.
        @return 下单结果 / Order-placement result.
        """

        return await self._operations.place_order(command)

    async def record_payment_event(self, command: RecordPaymentEvent) -> BillingResult:
        """@brief 验证后记录付款或争议通知 / Verify then record a payment or chargeback notification.

        @param command 支付事件命令 / Payment-event command.
        @return 支付状态结果；验证失败不会触及持久化端口 /
            Payment-state result; verification failure does not reach persistence.
        """

        if not await self._payment_events.verify(command.event):
            return BillingResult(BillingCode.PAYMENT_UNVERIFIED)
        return await self._operations.record_payment_event(command)

    async def fulfill_order(self, command: FulfillOrder) -> BillingResult:
        """@brief 由管理员原子履约订单 / Atomically fulfill an order as administrator.

        @param command 履约命令 / Fulfillment command.
        @return 履约结果 / Fulfillment result.
        """

        if command.operator_id != self._administrator_id:
            return BillingResult(BillingCode.FORBIDDEN)
        return await self._operations.fulfill_order(command)

    async def request_refund(self, command: RequestRefund) -> BillingResult:
        """@brief 发起用户退款申请 / Start a user refund request.

        @param command 退款申请命令 / Refund-request command.
        @return 退款申请结果 / Refund-request result.
        @note 端口必须在同一事务中验证订单所有权。/
            The port must validate order ownership in the same transaction.
        """

        return await self._operations.request_refund(command)

    async def review_refund(self, command: ReviewRefund) -> BillingResult:
        """@brief 由管理员审核退款 / Review a refund as administrator.

        @param command 退款审核命令 / Refund-review command.
        @return 退款审核结果 / Refund-review result.
        """

        if command.reviewer_id != self._administrator_id:
            return BillingResult(BillingCode.FORBIDDEN)
        return await self._operations.review_refund(command)

    async def settle_refund(self, command: SettleRefund) -> BillingResult:
        """@brief 验证后结算退款 / Verify then settle a refund.

        @param command 退款结算命令 / Refund-settlement command.
        @return 退款结算结果；验证失败不会触及持久化端口 /
            Refund-settlement result; verification failure does not reach persistence.
        """

        if not await self._payment_events.verify(command.event):
            return BillingResult(BillingCode.PAYMENT_UNVERIFIED)
        return await self._operations.settle_refund(command)

    async def cancel_subscription(self, command: CancelSubscription) -> BillingResult:
        """@brief 请求订阅在期末取消 / Request subscription cancellation at period end.

        @param command 订阅取消命令 / Subscription-cancellation command.
        @return 订阅取消结果 / Subscription-cancellation result.
        @note 端口必须在同一事务中验证订阅所有权。/
            The port must validate subscription ownership in the same transaction.
        """

        return await self._operations.cancel_subscription(command)

    async def active_user_entitlements(
        self,
        user_id: int,
        *,
        observed_at: datetime,
    ) -> tuple[EntitlementGrant, ...]:
        """@brief 查询用户在给定时刻有效的权益 / Query user entitlements active at an instant.

        @param user_id 用户标识 / User identity.
        @param observed_at 观察时刻 / Observation instant.
        @return 有效权益快照 / Active entitlement snapshots.
        @raise ValueError 用户标识不为正时抛出 / Raised when user identity is not positive.
        """

        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("Billing entitlement user must be positive")
        return await self._operations.active_user_entitlements(
            user_id,
            observed_at=observed_at,
        )
