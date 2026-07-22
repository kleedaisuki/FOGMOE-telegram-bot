"""@brief Billing 应用服务测试 / Billing application-service tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fogmoe_bot.application.billing.models import (
    BillingCode,
    BillingResult,
    CancelSubscription,
    FulfillOrder,
    PlaceOrder,
    RecordPaymentEvent,
    RefundReviewDecision,
    RequestRefund,
    ReviewRefund,
    SettleRefund,
)
from fogmoe_bot.application.billing.service import BillingService
from fogmoe_bot.domain.billing.catalog import PaymentAmount
from fogmoe_bot.domain.billing.entitlements import EntitlementGrant
from fogmoe_bot.domain.billing.orders import (
    PaymentEvent,
    PaymentEventKind,
    PaymentProvider,
)
from fogmoe_bot.infrastructure.billing.payment_events import (
    DenyUnconfiguredPaymentEventVerifier,
)


class _Operations:
    """@brief 记录 Billing 服务调用的内存端口 / In-memory port recording Billing-service calls."""

    def __init__(self) -> None:
        """@brief 初始化命令记录 / Initialize command recordings.

        @return None / None.
        """

        self.placed: list[PlaceOrder] = []
        self.payments: list[RecordPaymentEvent] = []
        self.fulfilled: list[FulfillOrder] = []
        self.refunds: list[RequestRefund] = []
        self.reviewed: list[ReviewRefund] = []
        self.settled: list[SettleRefund] = []
        self.cancelled: list[CancelSubscription] = []
        self.entitlement_reads: list[tuple[int, datetime]] = []

    async def place_order(self, command: PlaceOrder) -> BillingResult:
        """@brief 记录下单调用 / Record order-placement call.

        @param command 下单命令 / Order-placement command.
        @return 成功结果 / Successful result.
        """

        self.placed.append(command)
        return BillingResult(BillingCode.SUCCESS)

    async def record_payment_event(self, command: RecordPaymentEvent) -> BillingResult:
        """@brief 记录支付事件调用 / Record payment-event call.

        @param command 支付事件命令 / Payment-event command.
        @return 成功结果 / Successful result.
        """

        self.payments.append(command)
        return BillingResult(BillingCode.SUCCESS)

    async def fulfill_order(self, command: FulfillOrder) -> BillingResult:
        """@brief 记录履约调用 / Record fulfillment call.

        @param command 履约命令 / Fulfillment command.
        @return 成功结果 / Successful result.
        """

        self.fulfilled.append(command)
        return BillingResult(BillingCode.SUCCESS)

    async def request_refund(self, command: RequestRefund) -> BillingResult:
        """@brief 记录退款申请调用 / Record refund-request call.

        @param command 退款申请命令 / Refund-request command.
        @return 成功结果 / Successful result.
        """

        self.refunds.append(command)
        return BillingResult(BillingCode.SUCCESS)

    async def review_refund(self, command: ReviewRefund) -> BillingResult:
        """@brief 记录退款审核调用 / Record refund-review call.

        @param command 退款审核命令 / Refund-review command.
        @return 成功结果 / Successful result.
        """

        self.reviewed.append(command)
        return BillingResult(BillingCode.SUCCESS)

    async def settle_refund(self, command: SettleRefund) -> BillingResult:
        """@brief 记录退款结算调用 / Record refund-settlement call.

        @param command 退款结算命令 / Refund-settlement command.
        @return 成功结果 / Successful result.
        """

        self.settled.append(command)
        return BillingResult(BillingCode.SUCCESS)

    async def cancel_subscription(self, command: CancelSubscription) -> BillingResult:
        """@brief 记录订阅取消调用 / Record subscription-cancellation call.

        @param command 订阅取消命令 / Subscription-cancellation command.
        @return 成功结果 / Successful result.
        """

        self.cancelled.append(command)
        return BillingResult(BillingCode.SUCCESS)

    async def active_user_entitlements(
        self,
        user_id: int,
        *,
        observed_at: datetime,
    ) -> tuple[EntitlementGrant, ...]:
        """@brief 记录权益读取调用 / Record entitlement-read call.

        @param user_id 用户标识 / User identity.
        @param observed_at 观察时刻 / Observation instant.
        @return 空权益元组 / Empty entitlement tuple.
        """

        self.entitlement_reads.append((user_id, observed_at))
        return ()


class _Verifier:
    """@brief 可编排的支付事件验证替身 / Configurable payment-event verifier double."""

    def __init__(self, accepted: bool) -> None:
        """@brief 设置验证结果 / Set verification outcome.

        @param accepted 是否接受事件 / Whether to accept events.
        """

        self.accepted = accepted
        self.events: list[PaymentEvent] = []

    async def verify(self, event: PaymentEvent) -> bool:
        """@brief 记录并返回预设验证结果 / Record and return preset verification result.

        @param event 待验证事件 / Event to verify.
        @return 预设验证结果 / Preset verification outcome.
        """

        self.events.append(event)
        return self.accepted


def test_service_gates_backoffice_writes_and_unverified_provider_events() -> None:
    """@brief 只有管理员可做后台写入，未验证事件不能落库 / Only admin performs back-office writes and unverified events never persist."""

    async def scenario() -> None:
        """@brief 运行服务授权场景 / Run service authorization scenario.

        @return None / None.
        """

        now = datetime.now(UTC)
        order_id = uuid4()
        event = _payment_event(order_id=order_id, at=now)
        operations = _Operations()
        verifier = _Verifier(accepted=False)
        service = BillingService(
            operations=operations,
            payment_events=verifier,
            administrator_id=1,
        )
        unverified = await service.record_payment_event(
            RecordPaymentEvent(event, "test:billing:payment:unverified")
        )
        forbidden_fulfillment = await service.fulfill_order(
            FulfillOrder(order_id, 2, now, "test:billing:fulfill:forbidden")
        )
        forbidden_review = await service.review_refund(
            ReviewRefund(
                refund_id=uuid4(),
                reviewer_id=2,
                decision=RefundReviewDecision.APPROVE,
                reviewed_at=now,
                idempotency_key="test:billing:review:forbidden",
            )
        )

        assert unverified.code is BillingCode.PAYMENT_UNVERIFIED
        assert forbidden_fulfillment.code is BillingCode.FORBIDDEN
        assert forbidden_review.code is BillingCode.FORBIDDEN
        assert operations.payments == []
        assert operations.fulfilled == []
        assert operations.reviewed == []

        verifier.accepted = True
        verified = await service.record_payment_event(
            RecordPaymentEvent(event, "test:billing:payment:verified")
        )
        fulfilled = await service.fulfill_order(
            FulfillOrder(order_id, 1, now, "test:billing:fulfill:allowed")
        )
        reviewed = await service.review_refund(
            ReviewRefund(
                refund_id=uuid4(),
                reviewer_id=1,
                decision=RefundReviewDecision.REJECT,
                reviewed_at=now,
                idempotency_key="test:billing:review:allowed",
            )
        )

        assert verified.code is BillingCode.SUCCESS
        assert fulfilled.code is BillingCode.SUCCESS
        assert reviewed.code is BillingCode.SUCCESS
        assert len(operations.payments) == 1
        assert len(operations.fulfilled) == 1
        assert len(operations.reviewed) == 1

    asyncio.run(scenario())


def test_unconfigured_payment_verifier_fails_closed_before_persistence() -> None:
    """@brief 默认验证器拒绝付款事件且不触及持久化端口 / The default verifier rejects payment events without reaching persistence.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 运行未配置渠道场景 / Run the unconfigured-provider scenario.

        @return None / None.
        """

        now = datetime.now(UTC)
        """@brief 测试事件的发生时刻 / Occurrence instant for the test event."""
        operations = _Operations()
        """@brief 记录写调用的持久化替身 / Persistence double recording write calls."""
        service = BillingService(
            operations=operations,
            payment_events=DenyUnconfiguredPaymentEventVerifier(),
            administrator_id=1,
        )
        """@brief 使用默认拒绝验证器的 Billing 服务 / Billing service with the default denial verifier."""

        result = await service.record_payment_event(
            RecordPaymentEvent(
                _payment_event(order_id=uuid4(), at=now),
                "test:billing:default-denial",
            )
        )

        assert result.code is BillingCode.PAYMENT_UNVERIFIED
        assert operations.payments == []

    asyncio.run(scenario())


def test_service_leaves_user_commands_to_atomic_ownership_checks() -> None:
    """@brief 用户命令交给原子端口做所有权校验 / User commands leave ownership validation to the atomic port."""

    async def scenario() -> None:
        """@brief 运行用户命令场景 / Run user-command scenario.

        @return None / None.
        """

        now = datetime.now(UTC)
        operations = _Operations()
        service = BillingService(
            operations=operations,
            payment_events=_Verifier(accepted=True),
            administrator_id=1,
        )
        placed = await service.place_order(
            PlaceOrder(42, uuid4(), uuid4(), now, "test:billing:place")
        )
        refund = await service.request_refund(
            RequestRefund(
                requester_id=42,
                order_id=uuid4(),
                refund_id=uuid4(),
                reason="希望退款",
                requested_at=now,
                idempotency_key="test:billing:request-refund",
            )
        )
        cancellation = await service.cancel_subscription(
            CancelSubscription(42, uuid4(), now, "test:billing:cancel")
        )
        entitlements = await service.active_user_entitlements(42, observed_at=now)

        assert placed.code is BillingCode.SUCCESS
        assert refund.code is BillingCode.SUCCESS
        assert cancellation.code is BillingCode.SUCCESS
        assert entitlements == ()
        assert len(operations.placed) == 1
        assert len(operations.refunds) == 1
        assert len(operations.cancelled) == 1
        assert operations.entitlement_reads == [(42, now)]

    asyncio.run(scenario())


def _payment_event(*, order_id: UUID, at: datetime) -> PaymentEvent:
    """@brief 构造测试付款事件 / Construct a test payment event.

    @param order_id 所属订单标识 / Owning order identity.
    @param at 发生时刻 / Occurrence instant.
    @return 已验证付款事件模型 / Verified payment-event model.
    """

    return PaymentEvent(
        event_id=uuid4(),
        provider=PaymentProvider.TELEGRAM_STARS,
        provider_event_id="payment-service-1",
        provider_payment_id="charge-service-1",
        order_id=order_id,
        kind=PaymentEventKind.PAYMENT_SUCCEEDED,
        amount=PaymentAmount("XTR", 50),
        occurred_at=at,
    )
