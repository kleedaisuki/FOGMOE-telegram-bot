"""@brief Billing PostgreSQL 幂等与付款归属硬化测试 / Tests for Billing PostgreSQL idempotency and payment-ownership hardening."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

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
from fogmoe_bot.domain.billing.catalog import BillingOffer, PaymentAmount, ProductKind
from fogmoe_bot.domain.billing.entitlements import (
    EntitlementGrant,
    EntitlementScope,
    Subscription,
    SubscriptionStatus,
)
from fogmoe_bot.domain.billing.orders import (
    Order,
    PaymentEvent,
    PaymentEventKind,
    PaymentProvider,
)
from fogmoe_bot.infrastructure.database import billing as postgres_module


NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 测试使用的稳定 UTC 时刻 / Stable UTC instant used by tests."""

ORDER_ID = UUID("00000000-0000-0000-0000-000000000101")
"""@brief 主测试订单 UUID / Primary test-order UUID."""

OTHER_ORDER_ID = UUID("00000000-0000-0000-0000-000000000102")
"""@brief 冲突测试订单 UUID / Conflicting test-order UUID."""

OFFER_ID = UUID("00000000-0000-0000-0000-000000000103")
"""@brief 测试报价 UUID / Test-offer UUID."""

REFUND_ID = UUID("00000000-0000-0000-0000-000000000104")
"""@brief 测试退款 UUID / Test-refund UUID."""

SUBSCRIPTION_ID = UUID("00000000-0000-0000-0000-000000000105")
"""@brief 测试订阅 UUID / Test-subscription UUID."""

PRODUCT_ID = UUID("00000000-0000-0000-0000-000000000112")
"""@brief 订阅报价和订阅行共用的产品 UUID / Product UUID shared by the subscription offer and row."""


def test_public_billing_request_fingerprints_bind_every_command_semantic() -> None:
    """@brief 每个公开 Billing 写操作都绑定其完整业务语义 / Every public Billing write binds its full business semantics.

    @return None / None.
    """

    payment = _payment_event(order_id=ORDER_ID)
    """@brief 基线成功付款事件 / Baseline successful-payment event."""
    refund_event = _payment_event(
        order_id=ORDER_ID,
        event_id=UUID("00000000-0000-0000-0000-000000000106"),
        provider_event_id="refund-event-1",
        provider_payment_id="refund-payment-1",
        kind=PaymentEventKind.REFUND_SUCCEEDED,
    )
    """@brief 基线退款结算事件 / Baseline refund-settlement event."""

    place = PlaceOrder(42, OFFER_ID, ORDER_ID, NOW, "billing:place:one")
    """@brief 基线下单命令 / Baseline order-placement command."""
    changed_place = PlaceOrder(
        42,
        OFFER_ID,
        OTHER_ORDER_ID,
        NOW,
        "billing:place:one",
    )
    """@brief 改变订单标识的下单命令 / Order-placement command with a changed order ID."""
    record = RecordPaymentEvent(payment, "billing:payment:one")
    """@brief 基线付款记录命令 / Baseline payment-recording command."""
    changed_record = RecordPaymentEvent(
        _payment_event(order_id=OTHER_ORDER_ID),
        "billing:payment:one",
    )
    """@brief 改变归属订单的付款记录命令 / Payment-recording command with a changed owning order."""
    fulfill = FulfillOrder(ORDER_ID, 1, NOW, "billing:fulfill:one")
    """@brief 基线履约命令 / Baseline fulfillment command."""
    changed_fulfill = FulfillOrder(
        ORDER_ID,
        1,
        NOW + timedelta(seconds=1),
        "billing:fulfill:one",
    )
    """@brief 改变履约时刻的履约命令 / Fulfillment command with a changed fulfillment instant."""
    request_refund = RequestRefund(
        42,
        ORDER_ID,
        REFUND_ID,
        "重复商品",
        NOW,
        "billing:refund-request:one",
    )
    """@brief 基线退款申请命令 / Baseline refund-request command."""
    changed_request_refund = RequestRefund(
        42,
        ORDER_ID,
        REFUND_ID,
        "服务未达预期",
        NOW,
        "billing:refund-request:one",
    )
    """@brief 改变原因的退款申请命令 / Refund-request command with a changed reason."""
    review = ReviewRefund(
        REFUND_ID,
        1,
        RefundReviewDecision.APPROVE,
        NOW,
        "billing:refund-review:one",
        note="证据充分",
    )
    """@brief 基线退款审核命令 / Baseline refund-review command."""
    changed_review = ReviewRefund(
        REFUND_ID,
        1,
        RefundReviewDecision.REJECT,
        NOW,
        "billing:refund-review:one",
        note="证据充分",
    )
    """@brief 改变决定的退款审核命令 / Refund-review command with a changed decision."""
    settle = SettleRefund(REFUND_ID, refund_event, "billing:refund-settle:one")
    """@brief 基线退款结算命令 / Baseline refund-settlement command."""
    changed_settle = SettleRefund(
        REFUND_ID,
        _payment_event(
            order_id=ORDER_ID,
            event_id=UUID("00000000-0000-0000-0000-000000000107"),
            provider_event_id="refund-event-1",
            provider_payment_id="refund-payment-1",
            kind=PaymentEventKind.REFUND_SUCCEEDED,
            amount_units=121,
        ),
        "billing:refund-settle:one",
    )
    """@brief 改变金额的退款结算命令 / Refund-settlement command with a changed amount."""
    cancel = CancelSubscription(42, SUBSCRIPTION_ID, NOW, "billing:cancel:one")
    """@brief 基线订阅取消命令 / Baseline subscription-cancellation command."""
    changed_cancel = CancelSubscription(
        42,
        SUBSCRIPTION_ID,
        NOW + timedelta(seconds=1),
        "billing:cancel:one",
    )
    """@brief 改变请求时刻的订阅取消命令 / Subscription-cancellation command with a changed instant."""

    fingerprints = (
        (
            postgres_module._place_order_request_fingerprint(place),
            postgres_module._place_order_request_fingerprint(changed_place),
        ),
        (
            postgres_module._record_payment_event_request_fingerprint(record),
            postgres_module._record_payment_event_request_fingerprint(changed_record),
        ),
        (
            postgres_module._fulfill_order_request_fingerprint(fulfill),
            postgres_module._fulfill_order_request_fingerprint(changed_fulfill),
        ),
        (
            postgres_module._request_refund_request_fingerprint(request_refund),
            postgres_module._request_refund_request_fingerprint(changed_request_refund),
        ),
        (
            postgres_module._review_refund_request_fingerprint(review),
            postgres_module._review_refund_request_fingerprint(changed_review),
        ),
        (
            postgres_module._settle_refund_request_fingerprint(settle),
            postgres_module._settle_refund_request_fingerprint(changed_settle),
        ),
        (
            postgres_module._cancel_subscription_request_fingerprint(cancel),
            postgres_module._cancel_subscription_request_fingerprint(changed_cancel),
        ),
    )
    """@brief 每个公开操作的基线和变更指纹对 / Baseline and changed fingerprint pairs for every public operation."""

    assert all(len(first) == 64 and len(second) == 64 for first, second in fingerprints)
    assert all(first != second for first, second in fingerprints)
    assert postgres_module._place_order_request_fingerprint(place) == (
        postgres_module._place_order_request_fingerprint(
            PlaceOrder(42, OFFER_ID, ORDER_ID, NOW, "billing:place:another-key")
        )
    )


def test_receipt_loader_binds_sha256_and_fails_closed_for_legacy_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 回执加载器验证 SHA-256 语义且拒绝历史全零占位 / Receipt loader validates SHA-256 semantics and rejects legacy all-zero placeholders.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    command = PlaceOrder(42, OFFER_ID, ORDER_ID, NOW, "billing:receipt:one")
    """@brief 用于计算预期指纹的下单命令 / Order command used to calculate the expected fingerprint."""
    fingerprint = postgres_module._place_order_request_fingerprint(command)
    """@brief 预期请求语义摘要 / Expected request-semantics digest."""
    stored = {
        "operation_kind": "order.place",
        "actor_id": 42,
        "request_fingerprint": fingerprint,
        "result": {"code": "not_found"},
    }
    """@brief 模拟持久化的回执行 / Simulated persisted receipt row."""
    queries: list[str] = []
    """@brief 发出的查询文本 / Emitted query texts."""

    async def fetch_one(
        sql: str,
        params: object = None,
        *,
        mapping: bool = False,
        connection: AsyncConnection | None = None,
    ) -> dict[str, object]:
        """@brief 返回可变的模拟回执行 / Return the mutable simulated receipt row.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param mapping 是否要求映射行 / Whether a mapping row was requested.
        @param connection 当前连接 / Current connection.
        @return 模拟回执行 / Simulated receipt row.
        """

        del params, mapping, connection
        queries.append(sql)
        return stored

    monkeypatch.setattr(postgres_module.db_connection, "fetch_one", fetch_one)

    replay = asyncio.run(
        postgres_module._load_receipt(
            command.idempotency_key,
            "order.place",
            42,
            fingerprint,
            cast(AsyncConnection, object()),
        )
    )
    assert replay == {"code": "not_found"}
    assert "request_fingerprint" in queries[0]
    assert "FROM billing.operation_receipts" in queries[0]

    with pytest.raises(ValueError, match="request semantics"):
        asyncio.run(
            postgres_module._load_receipt(
                command.idempotency_key,
                "order.place",
                42,
                "f" * 64,
                cast(AsyncConnection, object()),
            )
        )

    stored["request_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="no request fingerprint"):
        asyncio.run(
            postgres_module._load_receipt(
                command.idempotency_key,
                "order.place",
                42,
                fingerprint,
                cast(AsyncConnection, object()),
            )
        )


def test_receipt_writer_persists_the_canonical_request_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 回执写入器与结果一起保存规范请求摘要 / Receipt writer saves the canonical request digest with the result.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    command = PlaceOrder(42, OFFER_ID, ORDER_ID, NOW, "billing:receipt-write:one")
    """@brief 用于生成待写摘要的下单命令 / Order command used to generate the digest being written."""
    fingerprint = postgres_module._place_order_request_fingerprint(command)
    """@brief 待写入的规范请求摘要 / Canonical request digest to persist."""
    writes: list[tuple[str, object]] = []
    """@brief 回执写入 SQL 与参数记录 / Recorded receipt-write SQL and parameters."""

    async def execute(
        sql: str,
        params: object = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> int:
        """@brief 记录一次回执写入 / Record one receipt write.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 模拟影响行数 / Simulated affected-row count.
        """

        del connection
        writes.append((sql, params))
        return 1

    monkeypatch.setattr(postgres_module.db_connection, "execute", execute)
    asyncio.run(
        postgres_module._save_receipt(
            command.idempotency_key,
            "order.place",
            command.buyer_id,
            fingerprint,
            {"code": "not_found"},
            cast(AsyncConnection, object()),
        )
    )

    assert "request_fingerprint" in writes[0][0]
    assert isinstance(writes[0][1], tuple)
    assert writes[0][1][3] == fingerprint


def test_successful_payment_ownership_lock_query_conflict_and_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 成功付款以稳定锁归属，冲突返回代码且同一事实重放 / Successful payments use a stable ownership lock, return conflicts, and replay the same fact.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    event = _payment_event(order_id=ORDER_ID)
    """@brief 已归属的成功付款事件 / Attributed successful-payment event."""
    existing = _successful_payment_row(event)
    """@brief 模拟既有成功付款事实 / Simulated existing successful-payment fact."""
    queries: list[tuple[str, object]] = []
    """@brief 低层 lock/query 调用记录 / Recorded low-level lock/query calls."""

    async def fetch_one(
        sql: str,
        params: object = None,
        *,
        mapping: bool = False,
        connection: AsyncConnection | None = None,
    ) -> dict[str, object] | None:
        """@brief 记录付款归属锁和查询 / Record payment-ownership lock and query.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param mapping 是否要求映射行 / Whether a mapping row was requested.
        @param connection 当前连接 / Current connection.
        @return 查询路径上的既有事实，锁路径为 None /
            Existing fact on the query path, or None on the lock path.
        """

        del mapping, connection
        queries.append((sql, params))
        return existing if "FROM billing.payment_events" in sql else None

    monkeypatch.setattr(postgres_module.db_connection, "fetch_one", fetch_one)
    asyncio.run(
        postgres_module._lock_successful_payment(
            event,
            cast(AsyncConnection, object()),
        )
    )
    loaded = asyncio.run(
        postgres_module._load_successful_payment(
            event,
            cast(AsyncConnection, object()),
        )
    )
    assert loaded == existing
    assert queries[0][1] == ("billing:successful-payment:telegram_stars:payment-1",)
    assert "provider_payment_id = %s" in queries[1][0]
    assert "event_kind = %s" in queries[1][0]
    assert queries[1][1] == (
        PaymentProvider.TELEGRAM_STARS.value,
        "payment-1",
        PaymentEventKind.PAYMENT_SUCCEEDED.value,
    )

    postgres_module._validate_existing_successful_payment(existing, event)
    conflicting = _payment_event(order_id=OTHER_ORDER_ID)
    """@brief 复用付款参考号但归属另一订单的事件 / Event reusing a payment reference for another order."""
    with pytest.raises(ValueError, match="Provider payment ID"):
        postgres_module._validate_existing_successful_payment(existing, conflicting)

    async def transaction() -> AsyncIterator[AsyncConnection]:
        """@brief 提供最小异步事务上下文 / Provide a minimal asynchronous transaction context.

        @return 单个虚拟数据库连接 / One dummy database connection.
        """

        yield cast(AsyncConnection, object())

    async def no_receipt(*args: object) -> None:
        """@brief 模拟首次调用没有回执 / Simulate a first call without a receipt.

        @param args 被忽略的调用参数 / Ignored call arguments.
        @return None / None.
        """

        del args
        return None

    async def no_op(*args: object) -> None:
        """@brief 模拟无副作用锁或回执写入 / Simulate a no-op lock or receipt write.

        @param args 被忽略的调用参数 / Ignored call arguments.
        @return None / None.
        """

        del args
        return None

    async def load_existing_success(
        *args: object,
    ) -> dict[str, object]:
        """@brief 返回同一既有成功付款事实 / Return the same existing successful-payment fact.

        @param args 被忽略的调用参数 / Ignored call arguments.
        @return 既有成功付款事实 / Existing successful-payment fact.
        """

        del args
        return existing

    paid_order = _paid_order(event)
    """@brief 对应既有付款事实的已付款订单 / Paid order corresponding to the existing fact."""

    async def load_paid_order(*args: object) -> Order:
        """@brief 返回既有已付款订单 / Return the existing paid order.

        @param args 被忽略的调用参数 / Ignored call arguments.
        @return 已付款订单 / Paid order.
        """

        del args
        return paid_order

    monkeypatch.setattr(
        postgres_module.db_connection, "transaction", asynccontextmanager(transaction)
    )
    monkeypatch.setattr(postgres_module, "_lock_idempotency_key", no_op)
    monkeypatch.setattr(postgres_module, "_load_receipt", no_receipt)
    monkeypatch.setattr(postgres_module, "_lock_provider_event", no_op)
    monkeypatch.setattr(postgres_module, "_lock_successful_payment", no_op)
    monkeypatch.setattr(
        postgres_module, "_load_successful_payment", load_existing_success
    )
    monkeypatch.setattr(postgres_module, "_read_order", load_paid_order)
    monkeypatch.setattr(postgres_module, "_save_receipt", no_op)

    operations = postgres_module.PostgresBillingOperations()
    replay = asyncio.run(
        operations.record_payment_event(
            RecordPaymentEvent(event, "billing:payment:replay")
        )
    )
    assert replay.replayed
    assert replay.order == paid_order

    conflict = asyncio.run(
        operations.record_payment_event(
            RecordPaymentEvent(conflicting, "billing:payment:conflict")
        )
    )
    assert conflict.code.value == "conflict"


def test_prepaid_renewal_refund_revokes_future_rights_at_their_start_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 预付续费在新周期开始前退款时，权益与订阅不会漏撤销或阻断结算 / A prepaid renewal refunded before its new period starts neither leaks rights nor blocks settlement.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    future_start = NOW + timedelta(days=30)
    """@brief 已付款续费对应的未来周期开始时刻 / Future-period start of the paid renewal."""
    future_end = future_start + timedelta(days=30)
    """@brief 已付款续费对应的未来周期结束时刻 / Future-period end of the paid renewal."""
    future_grant = EntitlementGrant.grant(
        grant_id=UUID("00000000-0000-0000-0000-000000000109"),
        code="assistant.priority",
        scope=EntitlementScope.USER,
        subject_id=42,
        source_order_id=ORDER_ID,
        starts_at=future_start,
        expires_at=future_end,
    )
    """@brief 预付续费生成、但尚未生效的权益 / Future entitlement generated by prepaid renewal."""
    future_subscription = Subscription.activate(
        subscription_id=SUBSCRIPTION_ID,
        owner_id=42,
        product_id=UUID("00000000-0000-0000-0000-000000000110"),
        offer_id=OFFER_ID,
        source_order_id=UUID("00000000-0000-0000-0000-000000000111"),
        entitlement_grant_ids=(future_grant.grant_id,),
        period_starts_at=future_start,
        period_ends_at=future_end,
    )
    """@brief 当前行已推进至未来续费周期的订阅 / Subscription row advanced to future renewal period."""
    persisted_grants: list[EntitlementGrant] = []
    """@brief 被写回的权益状态快照 / Entitlement snapshots persisted by the helper."""
    persisted_subscriptions: list[Subscription] = []
    """@brief 被写回的订阅状态快照 / Subscription snapshots persisted by the helper."""

    async def lock_grants(**kwargs: object) -> tuple[EntitlementGrant, ...]:
        """@brief 返回尚未生效的续费权益 / Return the not-yet-active renewal entitlement.

        @param kwargs 被替换端口传入的参数 / Parameters supplied by the replaced port.
        @return 单项未来权益 / One future entitlement.
        """

        del kwargs
        return (future_grant,)

    async def persist_grant(
        entitlement: EntitlementGrant,
        connection: AsyncConnection,
    ) -> None:
        """@brief 记录权益持久化请求 / Record an entitlement persistence request.

        @param entitlement 待写回权益 / Entitlement to persist.
        @param connection 当前连接 / Current connection.
        @return None / None.
        """

        del connection
        persisted_grants.append(entitlement)

    async def lock_subscription(
        order_id: UUID,
        connection: AsyncConnection,
    ) -> Subscription:
        """@brief 返回处于未来周期的订阅 / Return the subscription in its future period.

        @param order_id 当前周期来源订单 / Current-period source order.
        @param connection 当前连接 / Current connection.
        @return 当前订阅 / Current subscription.
        """

        assert order_id == ORDER_ID
        del connection
        return future_subscription

    async def persist_subscription(
        subscription: Subscription,
        *,
        current_order_id: UUID,
        connection: AsyncConnection,
    ) -> None:
        """@brief 记录订阅持久化请求 / Record a subscription persistence request.

        @param subscription 待写回订阅 / Subscription to persist.
        @param current_order_id 当前周期来源订单 / Current-period source order.
        @param connection 当前连接 / Current connection.
        @return None / None.
        """

        assert current_order_id == ORDER_ID
        del connection
        persisted_subscriptions.append(subscription)

    monkeypatch.setattr(postgres_module, "_lock_active_order_entitlements", lock_grants)
    monkeypatch.setattr(postgres_module, "_persist_entitlement", persist_grant)
    monkeypatch.setattr(
        postgres_module,
        "_lock_current_subscription_for_order",
        lock_subscription,
    )
    monkeypatch.setattr(postgres_module, "_persist_subscription", persist_subscription)

    connection = cast(AsyncConnection, object())
    revoked_grants = asyncio.run(
        postgres_module._revoke_order_entitlements(
            order_id=ORDER_ID,
            revoked_at=NOW,
            reason="refund_succeeded",
            connection=connection,
        )
    )
    revoked_subscription = asyncio.run(
        postgres_module._revoke_current_subscription(
            order_id=ORDER_ID,
            revoked_at=NOW,
            reason="refund_succeeded",
            connection=connection,
        )
    )

    assert revoked_grants[0].ended_at == future_start
    assert persisted_grants == list(revoked_grants)
    assert revoked_subscription is not None
    assert revoked_subscription.status is SubscriptionStatus.REVOKED
    assert revoked_subscription.ended_at == future_start
    assert persisted_subscriptions == [revoked_subscription]


def test_future_entitlements_are_locked_for_refund_or_chargeback_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 撤销查询保留未开始但尚未过期的权益 / Revocation query retains future grants that have not naturally expired.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    queries: list[tuple[str, object]] = []
    """@brief 已发出的查询和参数 / Emitted query and parameters."""

    async def fetch_all(
        sql: str,
        params: object = None,
        *,
        mapping: bool = False,
        connection: AsyncConnection | None = None,
    ) -> list[object]:
        """@brief 记录权益锁查询并返回空集合 / Record entitlement-lock query and return no rows.

        @param sql SQL 语句 / SQL statement.
        @param params SQL 参数 / SQL parameters.
        @param mapping 是否请求映射行 / Whether mapping rows were requested.
        @param connection 当前连接 / Current connection.
        @return 空查询结果 / Empty query result.
        """

        del mapping, connection
        queries.append((sql, params))
        return []

    monkeypatch.setattr(postgres_module.db_connection, "fetch_all", fetch_all)
    asyncio.run(
        postgres_module._lock_active_order_entitlements(
            order_id=ORDER_ID,
            observed_at=NOW,
            connection=cast(AsyncConnection, object()),
        )
    )

    sql, params = queries[0]
    assert "starts_at <=" not in sql
    assert "expires_at IS NULL OR expires_at > %s" in sql
    assert params == (ORDER_ID, NOW)


@pytest.mark.parametrize(
    ("period_starts_at", "period_ends_at"),
    (
        (NOW - timedelta(days=30), NOW - timedelta(seconds=1)),
        (NOW + timedelta(seconds=1), NOW + timedelta(days=30)),
    ),
    ids=("expired_active_status", "future_prepaid_period"),
)
def test_renewal_placement_requires_subscription_to_be_active_at_creation_time(
    monkeypatch: pytest.MonkeyPatch,
    period_starts_at: datetime,
    period_ends_at: datetime,
) -> None:
    """@brief 续费下单拒绝状态字段仍为 active 的过期或未来周期 / Renewal placement rejects expired or future periods even while the status field remains active.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @param period_starts_at 模拟订阅周期开始 / Simulated subscription-period start.
    @param period_ends_at 模拟订阅周期结束 / Simulated subscription-period end.
    @return None / None.
    """

    offer = BillingOffer(
        offer_id=OFFER_ID,
        product_id=PRODUCT_ID,
        product_kind=ProductKind.SUBSCRIPTION,
        price=PaymentAmount("XTR", 120),
        entitlement_codes=("assistant.priority",),
        created_at=NOW - timedelta(days=31),
        subscription_period=timedelta(days=30),
    )
    """@brief 与模拟订阅匹配的可售周期报价 / Sellable periodic offer matching the simulated subscription."""
    subscription = Subscription.activate(
        subscription_id=SUBSCRIPTION_ID,
        owner_id=42,
        product_id=PRODUCT_ID,
        offer_id=OFFER_ID,
        source_order_id=UUID("00000000-0000-0000-0000-000000000113"),
        entitlement_grant_ids=(UUID("00000000-0000-0000-0000-000000000114"),),
        period_starts_at=period_starts_at,
        period_ends_at=period_ends_at,
    )
    """@brief 故意未物化为终态的失效或预付周期 / Intentionally non-materialized expired or prepaid period."""

    @asynccontextmanager
    async def transaction() -> AsyncIterator[AsyncConnection]:
        """@brief 提供最小异步事务上下文 / Provide a minimal asynchronous transaction context.

        @return 单个虚拟数据库连接 / One dummy database connection.
        """

        yield cast(AsyncConnection, object())

    async def identity_exists(user_id: int, connection: AsyncConnection) -> bool:
        """@brief 模拟已注册的下单用户 / Simulate a registered ordering user.

        @param user_id 用户标识 / User identity.
        @param connection 当前连接 / Current connection.
        @return 恒为 True / Always True.
        """

        assert user_id == 42
        del connection
        return True

    async def lock_offer(
        offer_id: UUID,
        at: datetime,
        connection: AsyncConnection,
    ) -> BillingOffer:
        """@brief 返回匹配的可售报价 / Return the matching sellable offer.

        @param offer_id 报价标识 / Offer identity.
        @param at 下单时刻 / Order-placement instant.
        @param connection 当前连接 / Current connection.
        @return 可售订阅报价 / Sellable subscription offer.
        """

        assert offer_id == OFFER_ID
        assert at == NOW
        del connection
        return offer

    async def lock_subscription(
        subscription_id: UUID,
        connection: AsyncConnection,
    ) -> Subscription:
        """@brief 返回状态字段仍为 active 的模拟订阅 / Return the simulated subscription whose status remains active.

        @param subscription_id 订阅标识 / Subscription identity.
        @param connection 当前连接 / Current connection.
        @return 模拟订阅 / Simulated subscription.
        """

        assert subscription_id == SUBSCRIPTION_ID
        del connection
        return subscription

    async def fail_open_renewal_lookup(*args: object) -> bool:
        """@brief 证明周期校验先于开放订单检查执行 / Prove period validation runs before the open-order lookup.

        @param args 被忽略的调用参数 / Ignored call arguments.
        @return 永不返回 / Never returns.
        """

        del args
        raise AssertionError("invalid subscription period must not query open renewals")

    async def fail_insert(*args: object) -> BillingResult:
        """@brief 证明无效周期不能插入订单 / Prove an invalid period cannot insert an order.

        @param args 被忽略的调用参数 / Ignored call arguments.
        @return 永不返回 / Never returns.
        """

        del args
        raise AssertionError("invalid subscription period must not create an order")

    async def no_receipt(*args: object) -> None:
        """@brief 模拟首次调用没有幂等回执 / Simulate a first call with no idempotency receipt.

        @param args 被忽略的调用参数 / Ignored call arguments.
        @return None / None.
        """

        del args
        return None

    async def no_op(*args: object) -> None:
        """@brief 模拟无副作用锁和回执写入 / Simulate side-effect-free locks and receipt storage.

        @param args 被忽略的调用参数 / Ignored call arguments.
        @return None / None.
        """

        del args
        return None

    monkeypatch.setattr(postgres_module.db_connection, "transaction", transaction)
    monkeypatch.setattr(postgres_module, "_lock_idempotency_key", no_op)
    monkeypatch.setattr(postgres_module, "_load_receipt", no_receipt)
    monkeypatch.setattr(postgres_module, "_identity_exists", identity_exists)
    monkeypatch.setattr(postgres_module, "_lock_sellable_offer", lock_offer)
    monkeypatch.setattr(postgres_module, "_lock_subscription", lock_subscription)
    monkeypatch.setattr(
        postgres_module, "_has_open_renewal_order", fail_open_renewal_lookup
    )
    monkeypatch.setattr(postgres_module, "_insert_order_from_offer", fail_insert)
    monkeypatch.setattr(postgres_module, "_save_receipt", no_op)

    result = asyncio.run(
        postgres_module.PostgresBillingOperations().place_order(
            PlaceOrder(
                buyer_id=42,
                offer_id=OFFER_ID,
                order_id=ORDER_ID,
                created_at=NOW,
                idempotency_key="billing:renewal:inactive-period",
                renewal_subscription_id=SUBSCRIPTION_ID,
            )
        )
    )

    assert result.code is BillingCode.INVALID_STATE
    assert result.subscription == subscription


def test_open_renewal_lookup_uses_the_same_nonterminal_state_boundary_as_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 应用层开放续费查询只认数据库唯一索引覆盖的未终态 / The application open-renewal query recognizes only the nonterminal states covered by the database unique index.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    queries: list[tuple[str, object]] = []
    """@brief 已发出的开放续费检查查询 / Emitted open-renewal lookup queries."""

    async def fetch_one(
        sql: str,
        params: object = None,
        *,
        mapping: bool = False,
        connection: AsyncConnection | None = None,
    ) -> tuple[int]:
        """@brief 记录开放续费查询并返回存在记录 / Record the open-renewal query and return an existing row.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param mapping 是否要求映射行 / Whether a mapping row was requested.
        @param connection 当前连接 / Current connection.
        @return 单行存在标记 / One-row existence marker.
        """

        del mapping, connection
        queries.append((sql, params))
        return (1,)

    monkeypatch.setattr(postgres_module.db_connection, "fetch_one", fetch_one)
    exists = asyncio.run(
        postgres_module._has_open_renewal_order(
            SUBSCRIPTION_ID,
            cast(AsyncConnection, object()),
        )
    )

    assert exists
    sql, params = queries[0]
    assert "renewal_subscription_id = %s" in sql
    assert "'awaiting_payment', 'paid', 'refund_pending'" in sql
    assert "FOR UPDATE" not in sql
    assert params == (SUBSCRIPTION_ID,)


def _payment_event(
    *,
    order_id: UUID,
    event_id: UUID = UUID("00000000-0000-0000-0000-000000000108"),
    provider_event_id: str = "payment-event-1",
    provider_payment_id: str = "payment-1",
    kind: PaymentEventKind = PaymentEventKind.PAYMENT_SUCCEEDED,
    amount_units: int = 120,
) -> PaymentEvent:
    """@brief 构造固定的已验证支付事件 / Build a stable verified payment event.

    @param order_id 所属订单 UUID / Owning order UUID.
    @param event_id 本地事件 UUID / Local event UUID.
    @param provider_event_id 渠道事件参考号 / Provider event reference.
    @param provider_payment_id 渠道付款参考号 / Provider payment reference.
    @param kind 支付事件种类 / Payment-event kind.
    @param amount_units 原生金额单位 / Native amount units.
    @return 已验证支付事件 / Verified payment event.
    """

    return PaymentEvent(
        event_id=event_id,
        provider=PaymentProvider.TELEGRAM_STARS,
        provider_event_id=provider_event_id,
        provider_payment_id=provider_payment_id,
        order_id=order_id,
        kind=kind,
        amount=PaymentAmount("XTR", amount_units),
        occurred_at=NOW,
    )


def _successful_payment_row(event: PaymentEvent) -> dict[str, object]:
    """@brief 将支付事件表示为既有成功付款数据库行 / Represent a payment event as an existing successful-payment row.

    @param event 已归属成功付款事件 / Attributed successful-payment event.
    @return 模拟 payment_events 映射行 / Simulated payment_events mapping row.
    """

    return {
        "event_id": event.event_id,
        "provider": event.provider.value,
        "provider_event_id": event.provider_event_id,
        "provider_payment_id": event.provider_payment_id,
        "order_id": event.order_id,
        "refund_id": None,
        "event_kind": event.kind.value,
        "currency": event.amount.currency,
        "amount_units": event.amount.units,
        "occurred_at": event.occurred_at,
    }


def _paid_order(event: PaymentEvent) -> Order:
    """@brief 构造与成功付款事件匹配的已付款订单 / Build a paid order matching a successful-payment event.

    @param event 已验证成功付款事件 / Verified successful-payment event.
    @return 已付款订单 / Paid order.
    """

    return Order.create(
        order_id=event.order_id,
        buyer_id=42,
        product_id=UUID("00000000-0000-0000-0000-000000000109"),
        offer_id=OFFER_ID,
        product_kind=ProductKind.ONE_TIME,
        price=event.amount,
        created_at=event.occurred_at - timedelta(seconds=1),
    ).apply_payment_event(event)
