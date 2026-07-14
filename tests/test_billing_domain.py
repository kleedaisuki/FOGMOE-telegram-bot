"""@brief Billing 领域状态机测试 / Billing domain state-machine tests."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from fogmoe_bot.domain.billing.catalog import (
    BillingOffer,
    BillingProduct,
    OfferStatus,
    PaymentAmount,
    ProductKind,
)
from fogmoe_bot.domain.billing.entitlements import (
    EntitlementGrant,
    EntitlementScope,
    EntitlementStatus,
    Subscription,
    SubscriptionStatus,
)
from fogmoe_bot.domain.billing.orders import (
    Order,
    OrderStatus,
    PaymentEvent,
    PaymentEventKind,
    PaymentProvider,
)
from fogmoe_bot.domain.billing.refunds import Refund, RefundStatus


def test_catalog_freezes_native_price_without_any_token_conversion() -> None:
    """@brief 报价只保存渠道原生价格，不含金币换算 / An offer stores only native provider price, with no token conversion."""

    now = datetime.now(UTC)
    product = BillingProduct(
        product_id=uuid4(),
        code="premium.monthly",
        display_name="高级月卡",
        kind=ProductKind.SUBSCRIPTION,
    )
    offer = BillingOffer(
        offer_id=uuid4(),
        product_id=product.product_id,
        product_kind=product.kind,
        price=PaymentAmount("xtr", 120),
        entitlement_codes=("assistant.priority", "town.banner"),
        created_at=now,
        subscription_period=timedelta(days=30),
    )

    assert offer.price == PaymentAmount("XTR", 120)
    assert offer.is_available_at(now)
    assert product.is_active
    assert offer.retire().status is OfferStatus.RETIRED
    with pytest.raises(ValueError, match="Subscription offers"):
        BillingOffer(
            offer_id=uuid4(),
            product_id=product.product_id,
            product_kind=product.kind,
            price=PaymentAmount("XTR", 120),
            entitlement_codes=("assistant.priority",),
            created_at=now,
        )


def test_order_payment_fulfillment_and_refund_follow_one_way_transitions() -> None:
    """@brief 订单遵守付款、履约、退款的单向状态机 / Order follows one-way payment, fulfillment, and refund transitions."""

    now = datetime.now(UTC)
    order = Order.create(
        order_id=uuid4(),
        buyer_id=42,
        product_id=uuid4(),
        offer_id=uuid4(),
        product_kind=ProductKind.ONE_TIME,
        price=PaymentAmount("XTR", 50),
        created_at=now,
    )
    paid_event = _event(
        order_id=order.order_id,
        kind=PaymentEventKind.PAYMENT_SUCCEEDED,
        provider_event_id="payment-1",
        provider_payment_id="charge-1",
        occurred_at=now + timedelta(seconds=1),
    )
    paid = order.apply_payment_event(paid_event)
    fulfilled = paid.mark_fulfilled(fulfilled_at=now + timedelta(seconds=2))
    pending = fulfilled.request_refund(requested_at=now + timedelta(seconds=3))
    rejected = pending.reject_refund(reviewed_at=now + timedelta(seconds=4))
    refund_event = _event(
        order_id=order.order_id,
        kind=PaymentEventKind.REFUND_SUCCEEDED,
        provider_event_id="refund-1",
        provider_payment_id="charge-1",
        occurred_at=now + timedelta(seconds=5),
    )
    refunded = pending.resolve_refund(refund_event)

    assert paid.status is OrderStatus.PAID
    assert fulfilled.status is OrderStatus.FULFILLED
    assert pending.status is OrderStatus.REFUND_PENDING
    assert rejected.status is OrderStatus.FULFILLED
    assert refunded.status is OrderStatus.REFUNDED
    assert refunded.resolve_refund(refund_event) is refunded
    with pytest.raises(ValueError, match="Refund events"):
        fulfilled.apply_payment_event(refund_event)
    with pytest.raises(ValueError, match="Only fulfilled"):
        refunded.request_refund(requested_at=now + timedelta(seconds=5))


def test_refund_review_and_provider_settlement_are_separate_and_auditable() -> None:
    """@brief 退款审核与渠道结算分开，并留下可审计状态 / Refund review is separate from provider settlement with auditable states."""

    now = datetime.now(UTC)
    order_id = uuid4()
    refund = Refund.request(
        refund_id=uuid4(),
        order_id=order_id,
        requester_id=42,
        amount=PaymentAmount("XTR", 50),
        reason="服务未达到约定效果",
        requested_at=now,
    )
    approved = refund.approve(
        reviewer_id=1,
        reviewed_at=now + timedelta(seconds=1),
        note="核验通过",
    )
    settled = approved.settle_from_payment_event(
        _event(
            order_id=order_id,
            kind=PaymentEventKind.REFUND_SUCCEEDED,
            provider_event_id="refund-2",
            provider_payment_id="charge-2",
            occurred_at=now + timedelta(seconds=2),
        )
    )

    assert refund.status is RefundStatus.REQUESTED
    assert approved.status is RefundStatus.APPROVED
    assert settled.status is RefundStatus.SUCCEEDED
    assert settled.provider_settlement_id == "refund-2"
    with pytest.raises(ValueError, match="cannot review their own"):
        refund.approve(reviewer_id=42, reviewed_at=now + timedelta(seconds=1))
    with pytest.raises(ValueError, match="Only approved"):
        refund.settle_from_payment_event(
            _event(
                order_id=order_id,
                kind=PaymentEventKind.REFUND_FAILED,
                provider_event_id="refund-3",
                provider_payment_id="charge-2",
                occurred_at=now + timedelta(seconds=2),
            )
        )


def test_entitlements_and_subscriptions_expire_or_revoke_without_reactivation() -> None:
    """@brief 权益和订阅仅能到期或撤销，不能自动复活 / Entitlements and subscriptions only expire or revoke; they do not auto-reactivate."""

    now = datetime.now(UTC)
    order_id = uuid4()
    expiring = EntitlementGrant.grant(
        grant_id=uuid4(),
        code="assistant.priority",
        scope=EntitlementScope.USER,
        subject_id=42,
        source_order_id=order_id,
        starts_at=now,
        expires_at=now + timedelta(days=30),
    )
    expired = expiring.expire(observed_at=now + timedelta(days=31))
    revocable = EntitlementGrant.grant(
        grant_id=uuid4(),
        code="town.banner",
        scope=EntitlementScope.GROUP,
        subject_id=-100_42,
        source_order_id=order_id,
        starts_at=now,
    )
    revoked = revocable.revoke(
        revoked_at=now + timedelta(seconds=1),
        reason="退款成功",
    )
    subscription = Subscription.activate(
        subscription_id=uuid4(),
        owner_id=42,
        product_id=uuid4(),
        offer_id=uuid4(),
        source_order_id=order_id,
        entitlement_grant_ids=(expiring.grant_id,),
        period_starts_at=now,
        period_ends_at=now + timedelta(days=30),
    )
    cancelling = subscription.request_cancellation(
        requested_at=now + timedelta(days=1),
    )
    cancelled = cancelling.expire(observed_at=now + timedelta(days=30))

    assert not expired.is_active_at(now + timedelta(days=31))
    assert expired.status is EntitlementStatus.EXPIRED
    assert revoked.status is EntitlementStatus.REVOKED
    assert subscription.is_active_at(now + timedelta(days=10))
    assert cancelled.status is SubscriptionStatus.CANCELLED
    assert not cancelled.is_active_at(now + timedelta(days=30))
    with pytest.raises(ValueError, match="Only active"):
        cancelled.renew(
            renewed_at=now + timedelta(days=30),
            next_period_ends_at=now + timedelta(days=60),
        )


def test_subscription_renewal_replaces_current_grant_snapshot_and_one_time_orders_cannot_renew() -> (
    None
):
    """@brief 续费替换当前权益快照，非订阅订单不能伪装续费 / Renewal replaces the current grant snapshot and one-time orders cannot masquerade as renewals."""

    now = datetime.now(UTC)
    initial_grant_id = uuid4()
    next_grant_id = uuid4()
    subscription = Subscription.activate(
        subscription_id=uuid4(),
        owner_id=42,
        product_id=uuid4(),
        offer_id=uuid4(),
        source_order_id=uuid4(),
        entitlement_grant_ids=(initial_grant_id,),
        period_starts_at=now,
        period_ends_at=now + timedelta(days=30),
    )

    renewed = subscription.renew(
        renewed_at=now + timedelta(days=1),
        next_period_ends_at=now + timedelta(days=60),
        entitlement_grant_ids=(next_grant_id,),
    )

    assert renewed.period_starts_at == now + timedelta(days=30)
    assert renewed.entitlement_grant_ids == (next_grant_id,)
    with pytest.raises(ValueError, match="Only subscription"):
        Order.create(
            order_id=uuid4(),
            buyer_id=42,
            product_id=uuid4(),
            offer_id=uuid4(),
            product_kind=ProductKind.ONE_TIME,
            price=PaymentAmount("XTR", 1),
            created_at=now,
            renewal_subscription_id=subscription.subscription_id,
        )


def test_billing_context_has_no_dependency_on_banking_or_coin_issuance() -> None:
    """@brief Billing 核心不依赖银行或金币发行 / Billing core depends on neither banking nor token issuance."""

    source_root = Path(__file__).parents[1] / "src" / "fogmoe_bot"
    billing_roots = (
        source_root / "domain" / "billing",
        source_root / "application" / "billing",
    )
    imported_modules: list[str] = []
    for root in billing_roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    imported_modules.append(node.module)

    assert not [
        module
        for module in imported_modules
        if module.startswith("fogmoe_bot.domain.banking")
        or module.startswith("fogmoe_bot.application.banking")
    ]


def _event(
    *,
    order_id,
    kind: PaymentEventKind,
    provider_event_id: str,
    provider_payment_id: str,
    occurred_at: datetime,
) -> PaymentEvent:
    """@brief 构造测试用已验证支付事件 / Construct a verified payment event for tests.

    @param order_id 所属订单标识 / Owning order identity.
    @param kind 支付事件类型 / Payment event kind.
    @param provider_event_id 渠道事件参考号 / Provider event reference.
    @param provider_payment_id 渠道付款参考号 / Provider payment reference.
    @param occurred_at 发生时刻 / Occurrence instant.
    @return 测试支付事件 / Test payment event.
    """

    return PaymentEvent(
        event_id=uuid4(),
        provider=PaymentProvider.TELEGRAM_STARS,
        provider_event_id=provider_event_id,
        provider_payment_id=provider_payment_id,
        order_id=order_id,
        kind=kind,
        amount=PaymentAmount("XTR", 50),
        occurred_at=occurred_at,
    )
