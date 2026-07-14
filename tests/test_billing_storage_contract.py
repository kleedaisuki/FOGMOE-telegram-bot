"""@brief Billing PostgreSQL 存储契约测试 / Billing PostgreSQL storage-contract tests."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from uuid import uuid4

import pytest

from fogmoe_bot.application.billing.models import BillingCode, BillingResult
from fogmoe_bot.domain.billing.catalog import PaymentAmount, ProductKind
from fogmoe_bot.domain.billing.entitlements import (
    EntitlementGrant,
    EntitlementScope,
    Subscription,
)
from fogmoe_bot.domain.billing.orders import (
    Order,
    PaymentEvent,
    PaymentEventKind,
    PaymentProvider,
)
from fogmoe_bot.domain.billing.refunds import Refund
from fogmoe_bot.infrastructure.database.billing import (
    _result_from_mapping,
    _result_mapping,
    _subscription_period_seconds,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root directory."""


def test_billing_migration_and_snapshot_cover_native_payment_and_entitlement_records() -> (
    None
):
    """@brief 迁移与快照覆盖原生金额、权益和不可变事实 / Migration and snapshot cover native amounts, entitlements, and immutable facts."""

    migration = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0049_billing_entitlements.sql"
    ).read_text(encoding="utf-8")
    snapshot = (PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )

    required_statements = (
        "CREATE TABLE billing.products",
        "CREATE TABLE billing.offers",
        "CREATE TABLE billing.orders",
        "CREATE TABLE billing.payment_events",
        "CREATE TABLE billing.fulfillments",
        "CREATE TABLE billing.entitlement_grants",
        "CREATE TABLE billing.subscriptions",
        "CREATE TABLE billing.subscription_periods",
        "CREATE TABLE billing.subscription_entitlement_grants",
        "CREATE TABLE billing.refunds",
        "CREATE TABLE billing.operation_receipts",
        "CREATE FUNCTION billing.forbid_append_only_mutation",
        "CONSTRAINT billing_orders_offer_snapshot_fk",
        "CONSTRAINT billing_orders_renewal_kind_ck",
        "CREATE TRIGGER billing_payment_events_append_only_tr",
        "CREATE TRIGGER billing_fulfillments_append_only_tr",
        "CREATE TRIGGER billing_subscription_periods_append_only_tr",
        "CREATE TRIGGER billing_subscription_grants_append_only_tr",
        "CREATE TRIGGER billing_operation_receipts_append_only_tr",
    )
    for statement in required_statements:
        assert statement in migration
        assert statement in snapshot

    assert "price_units BIGINT NOT NULL CHECK (price_units > 0)" in migration
    assert "amount_units BIGINT NOT NULL CHECK (amount_units > 0)" in migration
    assert (
        re.search(
            r"\\b(?:FROM|JOIN|INTO|UPDATE|REFERENCES)\\s+bank\\.",
            migration,
            flags=re.IGNORECASE,
        )
        is None
    )
    assert re.search(r"^-- Alembic head: \S+$", snapshot, flags=re.MULTILINE)


def test_billing_adapter_receipts_round_trip_all_public_snapshots() -> None:
    """@brief Billing 回执可无损重放订单、退款、权益和订阅 / Billing receipts replay orders, refunds, entitlements, and subscriptions losslessly."""

    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    order = Order.create(
        order_id=uuid4(),
        buyer_id=42,
        product_id=uuid4(),
        offer_id=uuid4(),
        product_kind=ProductKind.SUBSCRIPTION,
        price=PaymentAmount("XTR", 120),
        created_at=now,
        renewal_subscription_id=uuid4(),
    )
    paid = order.apply_payment_event(
        _event(
            order_id=order.order_id,
            kind=PaymentEventKind.PAYMENT_SUCCEEDED,
            provider_event_id="payment-receipt-1",
            provider_payment_id="charge-receipt-1",
            occurred_at=now + timedelta(seconds=1),
        )
    )
    fulfilled = paid.mark_fulfilled(fulfilled_at=now + timedelta(seconds=2))
    grant = EntitlementGrant.grant(
        grant_id=uuid4(),
        code="assistant.priority",
        scope=EntitlementScope.USER,
        subject_id=42,
        source_order_id=fulfilled.order_id,
        starts_at=now + timedelta(seconds=2),
        expires_at=now + timedelta(days=30),
    )
    assert grant.expires_at is not None
    subscription = Subscription.activate(
        subscription_id=uuid4(),
        owner_id=42,
        product_id=fulfilled.product_id,
        offer_id=fulfilled.offer_id,
        source_order_id=fulfilled.order_id,
        entitlement_grant_ids=(grant.grant_id,),
        period_starts_at=grant.starts_at,
        period_ends_at=grant.expires_at,
    )
    refund = Refund.request(
        refund_id=uuid4(),
        order_id=fulfilled.order_id,
        requester_id=42,
        amount=fulfilled.price,
        reason="测试回执",
        requested_at=now + timedelta(seconds=3),
    ).approve(
        reviewer_id=1,
        reviewed_at=now + timedelta(seconds=4),
        note="批准",
    )
    result = BillingResult(
        BillingCode.SUCCESS,
        order=fulfilled,
        refund=refund,
        entitlements=(grant,),
        subscription=subscription,
    )

    payload = json.loads(json.dumps(_result_mapping(result)))
    replay = _result_from_mapping(payload, replayed=True)

    assert replay.code is BillingCode.SUCCESS
    assert replay.replayed
    assert replay.order == fulfilled
    assert replay.refund == refund
    assert replay.entitlements == (grant,)
    assert replay.subscription == subscription


def test_billing_adapter_does_not_import_banking_and_rejects_lossy_periods() -> None:
    """@brief 适配器不导入银行，且不会静默截断订阅周期 / Adapter imports no banking and never silently truncates subscription periods."""

    adapter_path = PROJECT_ROOT / "src/fogmoe_bot/infrastructure/database/billing.py"
    tree = ast.parse(
        adapter_path.read_text(encoding="utf-8"), filename=str(adapter_path)
    )
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.append(node.module)

    assert not [module for module in imported_modules if ".banking" in module]
    assert _subscription_period_seconds(timedelta(days=30)) == 30 * 86_400
    with pytest.raises(ValueError, match="whole-second"):
        _subscription_period_seconds(timedelta(microseconds=1))


def _event(
    *,
    order_id,
    kind: PaymentEventKind,
    provider_event_id: str,
    provider_payment_id: str,
    occurred_at: datetime,
) -> PaymentEvent:
    """@brief 构造已验证的测试支付事件 / Construct a verified test payment event.

    @param order_id 所属订单标识 / Owning order identity.
    @param kind 支付事件类型 / Payment-event kind.
    @param provider_event_id 渠道事件标识 / Provider-event identity.
    @param provider_payment_id 渠道付款标识 / Provider-payment identity.
    @param occurred_at 事件发生时刻 / Event occurrence instant.
    @return 已验证支付事件 / Verified payment event.
    """

    return PaymentEvent(
        event_id=uuid4(),
        provider=PaymentProvider.TELEGRAM_STARS,
        provider_event_id=provider_event_id,
        provider_payment_id=provider_payment_id,
        order_id=order_id,
        kind=kind,
        amount=PaymentAmount("XTR", 120),
        occurred_at=occurred_at,
    )
