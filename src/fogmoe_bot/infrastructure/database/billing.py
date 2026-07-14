"""@brief PostgreSQL Billing 与权益适配器 / PostgreSQL Billing and entitlement adapter.

``billing.operation_receipts`` 由迁移维护，必须包含 ``request_fingerprint CHAR(64)``；全零
摘要仅表示旧版本留下的不可验证回执，读取时必须 fail closed。成功付款还依赖
``billing.payment_events`` 上按 ``(provider, provider_payment_id)`` 的部分唯一约束。
The migration-owned ``billing.operation_receipts`` table must contain
``request_fingerprint CHAR(64)``; an all-zero digest marks an unverifiable legacy receipt and
must fail closed on read. Successful payments also rely on the partial unique constraint on
``billing.payment_events`` over ``(provider, provider_payment_id)``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from typing import Any, cast
from uuid import UUID, uuid4

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
from fogmoe_bot.application.billing.ports import BillingOperations
from fogmoe_bot.domain.billing.catalog import (
    BillingOffer,
    BillingProduct,
    OfferStatus,
    PaymentAmount,
    ProductKind,
    ProductStatus,
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
from fogmoe_bot.infrastructure.database import connection as db_connection


class _BillingConflictError(ValueError):
    """@brief Billing 回执或支付事实语义冲突 / Billing receipt or payment-fact semantics conflict."""


class PostgresBillingCatalog:
    """@brief PostgreSQL Billing 产品与报价目录写入器 / PostgreSQL writer for the Billing product and offer catalog.

    @note 目录仅保存渠道原生金额；它不接受银行余额、金币或兑换率。/
        The catalog stores provider-native amounts only; it accepts no bank balance, token, or
        exchange-rate value.
    """

    async def create_product(
        self,
        product: BillingProduct,
        *,
        created_at: datetime,
    ) -> None:
        """@brief 创建一份不可重用的产品目录记录 / Create a non-reusable product catalog record.

        @param product 待持久化产品 / Product to persist.
        @param created_at 产品创建时刻 / Product-creation instant.
        @return None / None.
        @raise ValueError 创建时刻无时区时由数据库映射层抛出 /
            Raised by the database mapping layer when creation time lacks a timezone.
        @raise IntegrityError 产品标识或代码已存在时由数据库驱动抛出 /
            Raised by the database driver when product ID or code already exists.
        """

        normalized_created_at = _as_utc(created_at)
        await db_connection.execute(
            "INSERT INTO billing.products ("
            "product_id, code, display_name, kind, status, description, created_at, "
            "retired_at, version, updated_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, CURRENT_TIMESTAMP)",
            (
                product.product_id,
                product.code,
                product.display_name,
                product.kind.value,
                product.status.value,
                product.description,
                normalized_created_at,
                (
                    normalized_created_at
                    if product.status is ProductStatus.RETIRED
                    else None
                ),
            ),
        )

    async def create_offer(self, offer: BillingOffer) -> None:
        """@brief 创建引用有效产品的报价快照 / Create an offer snapshot referencing an active product.

        @param offer 待持久化报价 / Offer to persist.
        @return None / None.
        @raise LookupError 所属产品不存在时抛出 / Raised when the owning product does not exist.
        @raise ValueError 产品不可售、产品类别不匹配或周期不可精确表示时抛出 /
            Raised when product is unavailable, kind mismatches, or period is not exactly representable.
        """

        period_seconds = _subscription_period_seconds(offer.subscription_period)
        async with db_connection.transaction() as connection:
            product = await db_connection.fetch_one(
                "SELECT kind, status FROM billing.products WHERE product_id = %s "
                "FOR KEY SHARE",
                (offer.product_id,),
                connection=connection,
            )
            if product is None:
                raise LookupError("Billing offer product does not exist")
            if str(product[0]) != offer.product_kind.value:
                raise ValueError("Billing offer kind does not match its product")
            if str(product[1]) != ProductStatus.ACTIVE.value:
                raise ValueError("Cannot create an offer for a retired product")
            await db_connection.execute(
                "INSERT INTO billing.offers ("
                "offer_id, product_id, product_kind, currency, price_units, "
                "entitlement_codes, created_at, subscription_period_seconds, "
                "available_from, available_until, status, retired_at, version, updated_at"
                ") VALUES (%s, %s, %s, %s, %s, CAST(%s AS JSONB), %s, %s, %s, %s, "
                "%s, %s, 0, CURRENT_TIMESTAMP)",
                (
                    offer.offer_id,
                    offer.product_id,
                    offer.product_kind.value,
                    offer.price.currency,
                    offer.price.units,
                    json.dumps(list(offer.entitlement_codes)),
                    offer.created_at,
                    period_seconds,
                    offer.available_from,
                    offer.available_until,
                    offer.status.value,
                    offer.created_at if offer.status is OfferStatus.RETIRED else None,
                ),
                connection=connection,
            )

    async def retire_product(self, product_id: UUID, *, retired_at: datetime) -> bool:
        """@brief 停售产品但不删除历史订单 / Retire a product without deleting historical orders.

        @param product_id 待停售产品标识 / Product identity to retire.
        @param retired_at 停售生效时刻 / Retirement effective instant.
        @return 发生状态变迁时为 True / True when a state transition occurred.
        """

        changed = await db_connection.execute(
            "UPDATE billing.products SET status = 'retired', retired_at = %s, "
            "version = version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE product_id = %s AND status = 'active'",
            (_as_utc(retired_at), product_id),
        )
        return changed == 1

    async def retire_offer(self, offer_id: UUID, *, retired_at: datetime) -> bool:
        """@brief 停售报价但保留冻结价格审计 / Retire an offer while retaining frozen-price audit records.

        @param offer_id 待停售报价标识 / Offer identity to retire.
        @param retired_at 停售生效时刻 / Retirement effective instant.
        @return 发生状态变迁时为 True / True when a state transition occurred.
        """

        changed = await db_connection.execute(
            "UPDATE billing.offers SET status = 'retired', retired_at = %s, "
            "version = version + 1, updated_at = CURRENT_TIMESTAMP "
            "WHERE offer_id = %s AND status = 'active'",
            (_as_utc(retired_at), offer_id),
        )
        return changed == 1

    async def load_offer(self, offer_id: UUID) -> BillingOffer | None:
        """@brief 读取报价目录快照（包括停售报价） / Read an offer catalog snapshot, including retired offers.

        @param offer_id 报价标识 / Offer identity.
        @return 报价；不存在时为 None / Offer, or None when absent.
        """

        row = await db_connection.fetch_one(
            "SELECT offer_id, product_id, product_kind, currency, price_units, "
            "entitlement_codes, created_at, subscription_period_seconds, "
            "available_from, available_until, status "
            "FROM billing.offers WHERE offer_id = %s",
            (offer_id,),
            mapping=True,
        )
        return _offer_from_row(_mapping(row)) if row is not None else None


class PostgresBillingOperations(BillingOperations):
    """@brief 以 PostgreSQL 状态机和 append-only 事实实现 Billing 端口 / Implement Billing ports with PostgreSQL state machines and append-only facts.

    @note 所有 SQL 都使用 ``billing`` 全限定名称；该适配器不依赖银行、金币或兑换率。/
        All SQL uses fully qualified ``billing`` names; this adapter has no dependency on banking,
        tokens, or exchange rates.
    """

    async def place_order(self, command: PlaceOrder) -> BillingResult:
        """@brief 从可售报价创建或重放订单 / Create or replay an order from a sellable offer.

        @param command 下单命令 / Order-placement command.
        @return 稳定下单结果 / Stable order-placement result.
        """

        operation_kind = "order.place"
        request_fingerprint = _place_order_request_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            try:
                replay = await _load_receipt(
                    command.idempotency_key,
                    operation_kind,
                    command.buyer_id,
                    request_fingerprint,
                    connection,
                )
            except _BillingConflictError:
                return BillingResult(BillingCode.CONFLICT)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)
            if not await _identity_exists(command.buyer_id, connection):
                result = BillingResult(BillingCode.NOT_REGISTERED)
            else:
                offer = await _lock_sellable_offer(
                    command.offer_id,
                    command.created_at,
                    connection,
                )
                if offer is None:
                    result = BillingResult(BillingCode.OFFER_UNAVAILABLE)
                elif command.renewal_subscription_id is not None:
                    subscription = await _lock_subscription(
                        command.renewal_subscription_id,
                        connection,
                    )
                    if subscription is None:
                        result = BillingResult(BillingCode.NOT_FOUND)
                    elif (
                        subscription.owner_id != command.buyer_id
                        or subscription.product_id != offer.product_id
                        or subscription.offer_id != offer.offer_id
                    ):
                        result = BillingResult(BillingCode.FORBIDDEN)
                    elif not subscription.is_active_at(command.created_at):
                        # @brief 用订单创建时刻验证当前周期，拒绝已经到期或已经推进到未来的订阅 / Validate the current period at the order-creation instant and reject expired or already-prepaid future subscriptions.
                        # 仅检查 ``status == active`` 会把尚未物化为 expired 的旧周期、或已被上一次
                        # 续费推进到未来的周期误认为可续；两者都会产生无法由 ``renew`` 履约的订单。/
                        # Checking only ``status == active`` would accept an old period not yet
                        # materialized as expired, or a period already advanced to the future by a
                        # prior renewal; neither can be fulfilled through ``renew``.
                        result = BillingResult(
                            BillingCode.INVALID_STATE,
                            subscription=subscription,
                        )
                    elif await _has_open_renewal_order(
                        subscription.subscription_id,
                        connection,
                    ):
                        # @brief 订阅行锁把同订阅下单串行化；partial unique index 是绕过适配器写入时的最终防线 / The subscription-row lock serializes same-subscription placement; the partial unique index is the final defense against writes that bypass this adapter.
                        result = BillingResult(
                            BillingCode.INVALID_STATE,
                            subscription=subscription,
                        )
                    else:
                        result = await _insert_order_from_offer(
                            command,
                            offer,
                            connection,
                        )
                else:
                    result = await _insert_order_from_offer(command, offer, connection)
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.buyer_id,
                request_fingerprint,
                _result_mapping(result),
                connection,
            )
            return result

    async def record_payment_event(self, command: RecordPaymentEvent) -> BillingResult:
        """@brief 原子记录付款失败、成功或拒付事件 / Atomically record payment failure, success, or chargeback event.

        @param command 已验证的支付事件命令 / Verified payment-event command.
        @return 订单和可选撤销权益结果 / Order and optional revoked-entitlement result.
        """

        operation_kind = "payment_event.record"
        request_fingerprint = _record_payment_event_request_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            try:
                replay = await _load_receipt(
                    command.idempotency_key,
                    operation_kind,
                    None,
                    request_fingerprint,
                    connection,
                )
            except _BillingConflictError:
                return BillingResult(BillingCode.CONFLICT)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)
            await _lock_provider_event(command.event, connection)
            if command.event.kind is PaymentEventKind.PAYMENT_SUCCEEDED:
                await _lock_successful_payment(command.event, connection)
                existing_success = await _load_successful_payment(
                    command.event,
                    connection,
                )
                if existing_success is not None:
                    try:
                        _validate_existing_successful_payment(
                            existing_success,
                            command.event,
                        )
                    except _BillingConflictError:
                        return BillingResult(BillingCode.CONFLICT)
                    order = await _read_order(
                        _uuid_value(
                            existing_success["order_id"],
                            field="Existing successful-payment order ID",
                        ),
                        connection,
                    )
                    result = (
                        BillingResult(BillingCode.SUCCESS, order=order, replayed=True)
                        if order is not None
                        else BillingResult(BillingCode.NOT_FOUND, replayed=True)
                    )
                    await _save_receipt(
                        command.idempotency_key,
                        operation_kind,
                        None,
                        request_fingerprint,
                        _result_mapping(result),
                        connection,
                    )
                    return result
            existing = await _load_provider_event(command.event, connection)
            if existing is not None:
                try:
                    _validate_existing_provider_event(
                        existing,
                        command.event,
                        refund_id=None,
                    )
                except _BillingConflictError:
                    return BillingResult(BillingCode.CONFLICT)
                order = await _read_order(command.event.order_id, connection)
                result = (
                    BillingResult(BillingCode.SUCCESS, order=order, replayed=True)
                    if order is not None
                    else BillingResult(BillingCode.NOT_FOUND, replayed=True)
                )
            else:
                order = await _lock_order(command.event.order_id, connection)
                if order is None:
                    result = BillingResult(BillingCode.NOT_FOUND)
                else:
                    try:
                        updated_order = order.apply_payment_event(command.event)
                    except ValueError:
                        result = BillingResult(BillingCode.INVALID_STATE, order=order)
                    else:
                        await _persist_order(updated_order, connection)
                        await _insert_payment_event(
                            command.event,
                            refund_id=None,
                            connection=connection,
                        )
                        entitlements: tuple[EntitlementGrant, ...] = ()
                        subscription: Subscription | None = None
                        if command.event.kind is PaymentEventKind.CHARGEBACK_OPENED:
                            entitlements = await _revoke_order_entitlements(
                                order_id=updated_order.order_id,
                                revoked_at=command.event.occurred_at,
                                reason="payment_chargeback",
                                connection=connection,
                            )
                            subscription = await _revoke_current_subscription(
                                order_id=updated_order.order_id,
                                revoked_at=command.event.occurred_at,
                                reason="payment_chargeback",
                                connection=connection,
                            )
                        result = BillingResult(
                            BillingCode.SUCCESS,
                            order=updated_order,
                            entitlements=entitlements,
                            subscription=subscription,
                        )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                None,
                request_fingerprint,
                _result_mapping(result),
                connection,
            )
            return result

    async def fulfill_order(self, command: FulfillOrder) -> BillingResult:
        """@brief 将已付款订单与权益、订阅原子履约 / Atomically fulfill a paid order with entitlements and subscription.

        @param command 履约命令 / Fulfillment command.
        @return 已履约订单、权益与可选订阅 / Fulfilled order, entitlements, and optional subscription.
        """

        operation_kind = "order.fulfill"
        request_fingerprint = _fulfill_order_request_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            try:
                replay = await _load_receipt(
                    command.idempotency_key,
                    operation_kind,
                    command.operator_id,
                    request_fingerprint,
                    connection,
                )
            except _BillingConflictError:
                return BillingResult(BillingCode.CONFLICT)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)
            if not await _identity_exists(command.operator_id, connection):
                result = BillingResult(BillingCode.NOT_REGISTERED)
            else:
                order = await _lock_order(command.order_id, connection)
                if order is None:
                    result = BillingResult(BillingCode.NOT_FOUND)
                elif order.status is not OrderStatus.PAID:
                    result = BillingResult(BillingCode.INVALID_STATE, order=order)
                else:
                    offer = await _load_order_offer(order, connection)
                    if offer is None:
                        result = BillingResult(BillingCode.INVALID_STATE, order=order)
                    else:
                        result = await _fulfill_locked_order(
                            order,
                            offer=offer,
                            command=command,
                            connection=connection,
                        )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.operator_id,
                request_fingerprint,
                _result_mapping(result),
                connection,
            )
            return result

    async def request_refund(self, command: RequestRefund) -> BillingResult:
        """@brief 原子创建退款请求并冻结订单为等待退款 / Atomically create a refund request and freeze order as refund pending.

        @param command 用户退款申请命令 / User refund-request command.
        @return 退款和订单结果 / Refund and order result.
        """

        operation_kind = "refund.request"
        request_fingerprint = _request_refund_request_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            try:
                replay = await _load_receipt(
                    command.idempotency_key,
                    operation_kind,
                    command.requester_id,
                    request_fingerprint,
                    connection,
                )
            except _BillingConflictError:
                return BillingResult(BillingCode.CONFLICT)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)
            order = await _lock_order(command.order_id, connection)
            if order is None:
                result = BillingResult(BillingCode.NOT_FOUND)
            elif order.buyer_id != command.requester_id:
                result = BillingResult(BillingCode.FORBIDDEN, order=order)
            else:
                try:
                    pending_order = order.request_refund(
                        requested_at=command.requested_at,
                    )
                    refund = Refund.request(
                        refund_id=command.refund_id,
                        order_id=order.order_id,
                        requester_id=command.requester_id,
                        amount=order.price,
                        reason=command.reason,
                        requested_at=command.requested_at,
                    )
                except ValueError:
                    result = BillingResult(BillingCode.INVALID_STATE, order=order)
                else:
                    await _persist_order(pending_order, connection)
                    await _insert_refund(refund, connection)
                    result = BillingResult(
                        BillingCode.SUCCESS,
                        order=pending_order,
                        refund=refund,
                    )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.requester_id,
                request_fingerprint,
                _result_mapping(result),
                connection,
            )
            return result

    async def review_refund(self, command: ReviewRefund) -> BillingResult:
        """@brief 审核退款，拒绝时恢复订单履约 / Review a refund and restore fulfillment on rejection.

        @param command 后台退款审核命令 / Back-office refund-review command.
        @return 退款和订单结果 / Refund and order result.
        """

        operation_kind = "refund.review"
        request_fingerprint = _review_refund_request_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            try:
                replay = await _load_receipt(
                    command.idempotency_key,
                    operation_kind,
                    command.reviewer_id,
                    request_fingerprint,
                    connection,
                )
            except _BillingConflictError:
                return BillingResult(BillingCode.CONFLICT)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)
            refund = await _lock_refund(command.refund_id, connection)
            if refund is None:
                result = BillingResult(BillingCode.NOT_FOUND)
            elif refund.requester_id == command.reviewer_id:
                result = BillingResult(BillingCode.FORBIDDEN, refund=refund)
            else:
                order = await _lock_order(refund.order_id, connection)
                if order is None:
                    result = BillingResult(BillingCode.NOT_FOUND, refund=refund)
                else:
                    try:
                        if command.decision is RefundReviewDecision.APPROVE:
                            reviewed = refund.approve(
                                reviewer_id=command.reviewer_id,
                                reviewed_at=command.reviewed_at,
                                note=command.note,
                            )
                            updated_order = order
                        else:
                            reviewed = refund.reject(
                                reviewer_id=command.reviewer_id,
                                reviewed_at=command.reviewed_at,
                                note=command.note,
                            )
                            updated_order = order.reject_refund(
                                reviewed_at=command.reviewed_at,
                            )
                    except ValueError:
                        result = BillingResult(
                            BillingCode.INVALID_STATE,
                            order=order,
                            refund=refund,
                        )
                    else:
                        await _persist_refund(reviewed, connection)
                        if updated_order is not order:
                            await _persist_order(updated_order, connection)
                        result = BillingResult(
                            BillingCode.SUCCESS,
                            order=updated_order,
                            refund=reviewed,
                        )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.reviewer_id,
                request_fingerprint,
                _result_mapping(result),
                connection,
            )
            return result

    async def settle_refund(self, command: SettleRefund) -> BillingResult:
        """@brief 以渠道事件原子结算退款与撤销权益 / Atomically settle a refund and revoke entitlements from a provider event.

        @param command 已验证的退款结算命令 / Verified refund-settlement command.
        @return 退款、订单、权益与可选订阅结果 / Refund, order, entitlement, and optional subscription result.
        """

        operation_kind = "refund.settle"
        request_fingerprint = _settle_refund_request_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            try:
                replay = await _load_receipt(
                    command.idempotency_key,
                    operation_kind,
                    None,
                    request_fingerprint,
                    connection,
                )
            except _BillingConflictError:
                return BillingResult(BillingCode.CONFLICT)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)
            await _lock_provider_event(command.event, connection)
            existing = await _load_provider_event(command.event, connection)
            if existing is not None:
                try:
                    _validate_existing_provider_event(
                        existing,
                        command.event,
                        refund_id=command.refund_id,
                    )
                except _BillingConflictError:
                    return BillingResult(BillingCode.CONFLICT)
                refund = await _read_refund(command.refund_id, connection)
                order = await _read_order(command.event.order_id, connection)
                result = BillingResult(
                    BillingCode.SUCCESS,
                    order=order,
                    refund=refund,
                    replayed=True,
                )
            else:
                refund = await _lock_refund(command.refund_id, connection)
                if refund is None:
                    result = BillingResult(BillingCode.NOT_FOUND)
                elif refund.order_id != command.event.order_id:
                    result = BillingResult(
                        BillingCode.INVALID_PAYMENT_EVENT, refund=refund
                    )
                else:
                    order = await _lock_order(refund.order_id, connection)
                    if order is None:
                        result = BillingResult(BillingCode.NOT_FOUND, refund=refund)
                    else:
                        try:
                            settled_refund = refund.settle_from_payment_event(
                                command.event
                            )
                            settled_order = order.resolve_refund(command.event)
                        except ValueError:
                            result = BillingResult(
                                BillingCode.INVALID_STATE,
                                order=order,
                                refund=refund,
                            )
                        else:
                            await _persist_refund(settled_refund, connection)
                            await _persist_order(settled_order, connection)
                            await _insert_payment_event(
                                command.event,
                                refund_id=settled_refund.refund_id,
                                connection=connection,
                            )
                            entitlements: tuple[EntitlementGrant, ...] = ()
                            subscription: Subscription | None = None
                            if settled_refund.status is RefundStatus.SUCCEEDED:
                                entitlements = await _revoke_order_entitlements(
                                    order_id=settled_order.order_id,
                                    revoked_at=command.event.occurred_at,
                                    reason="refund_succeeded",
                                    connection=connection,
                                )
                                subscription = await _revoke_current_subscription(
                                    order_id=settled_order.order_id,
                                    revoked_at=command.event.occurred_at,
                                    reason="refund_succeeded",
                                    connection=connection,
                                )
                            result = BillingResult(
                                BillingCode.SUCCESS,
                                order=settled_order,
                                refund=settled_refund,
                                entitlements=entitlements,
                                subscription=subscription,
                            )
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                None,
                request_fingerprint,
                _result_mapping(result),
                connection,
            )
            return result

    async def cancel_subscription(self, command: CancelSubscription) -> BillingResult:
        """@brief 请求订阅在当前周期末结束 / Request a subscription end at its current period boundary.

        @param command 订阅取消命令 / Subscription-cancellation command.
        @return 订阅状态结果 / Subscription-state result.
        """

        operation_kind = "subscription.cancel"
        request_fingerprint = _cancel_subscription_request_fingerprint(command)
        async with db_connection.transaction() as connection:
            await _lock_idempotency_key(command.idempotency_key, connection)
            try:
                replay = await _load_receipt(
                    command.idempotency_key,
                    operation_kind,
                    command.owner_id,
                    request_fingerprint,
                    connection,
                )
            except _BillingConflictError:
                return BillingResult(BillingCode.CONFLICT)
            if replay is not None:
                return _result_from_mapping(replay, replayed=True)
            subscription = await _lock_subscription(command.subscription_id, connection)
            if subscription is None:
                result = BillingResult(BillingCode.NOT_FOUND)
            elif subscription.owner_id != command.owner_id:
                result = BillingResult(BillingCode.FORBIDDEN, subscription=subscription)
            else:
                try:
                    cancelled = subscription.request_cancellation(
                        requested_at=command.requested_at,
                    )
                except ValueError:
                    result = BillingResult(
                        BillingCode.INVALID_STATE,
                        subscription=subscription,
                    )
                else:
                    if cancelled is not subscription:
                        await _persist_subscription(
                            cancelled,
                            current_order_id=await _current_subscription_order_id(
                                command.subscription_id,
                                connection,
                            ),
                            connection=connection,
                        )
                    result = BillingResult(BillingCode.SUCCESS, subscription=cancelled)
            await _save_receipt(
                command.idempotency_key,
                operation_kind,
                command.owner_id,
                request_fingerprint,
                _result_mapping(result),
                connection,
            )
            return result

    async def active_user_entitlements(
        self,
        user_id: int,
        *,
        observed_at: datetime,
    ) -> tuple[EntitlementGrant, ...]:
        """@brief 读取用户在给定时刻确实有效的权益 / Read entitlements actually active for a user at an instant.

        @param user_id 用户标识 / User identity.
        @param observed_at 观察时刻 / Observation instant.
        @return 有效权益的稳定顺序元组 / Stably ordered tuple of active entitlements.
        """

        normalized_observed_at = _as_utc(observed_at)
        rows = await db_connection.fetch_all(
            "SELECT grant_id, code, scope, subject_id, source_order_id, starts_at, "
            "expires_at, status, ended_at, revocation_reason, version "
            "FROM billing.entitlement_grants "
            "WHERE scope = 'user' AND subject_id = %s AND status = 'active' "
            "AND starts_at <= %s AND (expires_at IS NULL OR expires_at > %s) "
            "ORDER BY starts_at, grant_id",
            (user_id, normalized_observed_at, normalized_observed_at),
            mapping=True,
        )
        return tuple(_grant_from_row(_mapping(row)) for row in rows)


async def _fulfill_locked_order(
    order: Order,
    *,
    offer: BillingOffer,
    command: FulfillOrder,
    connection: AsyncConnection,
) -> BillingResult:
    """@brief 在已锁定订单上执行完整履约 / Execute complete fulfillment for an already locked order.

    @param order 已锁定且已付款订单 / Locked and paid order.
    @param offer 与冻结订单匹配的报价 / Offer matching the frozen order.
    @param command 履约命令 / Fulfillment command.
    @param connection 当前事务连接 / Current transactional connection.
    @return 履约结果 / Fulfillment result.
    @raise RuntimeError 订单和报价快照不一致时抛出 /
        Raised when order and offer snapshots are inconsistent.
    """

    if (
        offer.product_id != order.product_id
        or offer.offer_id != order.offer_id
        or offer.product_kind is not order.product_kind
        or offer.price != order.price
    ):
        raise RuntimeError("Order no longer matches its frozen offer snapshot")
    if (
        offer.product_kind is ProductKind.SUBSCRIPTION
        and order.renewal_subscription_id is not None
    ):
        return await _renew_subscription_fulfillment(
            order=order,
            offer=offer,
            operator_id=command.operator_id,
            fulfilled_at=command.fulfilled_at,
            connection=connection,
        )
    try:
        fulfilled_order = order.mark_fulfilled(fulfilled_at=command.fulfilled_at)
    except ValueError:
        return BillingResult(BillingCode.INVALID_STATE, order=order)
    fulfillment_id = uuid4()
    await _persist_order(fulfilled_order, connection)
    await _insert_fulfillment(
        fulfillment_id=fulfillment_id,
        order_id=fulfilled_order.order_id,
        operator_id=command.operator_id,
        fulfilled_at=command.fulfilled_at,
        connection=connection,
    )

    if offer.product_kind is ProductKind.ONE_TIME:
        entitlements = _new_entitlements(
            codes=offer.entitlement_codes,
            order_id=fulfilled_order.order_id,
            fulfillment_id=fulfillment_id,
            owner_id=fulfilled_order.buyer_id,
            starts_at=command.fulfilled_at,
            expires_at=None,
        )
        for entitlement in entitlements:
            await _insert_entitlement(entitlement, fulfillment_id, connection)
        return BillingResult(
            BillingCode.SUCCESS,
            order=fulfilled_order,
            entitlements=entitlements,
        )

    if offer.subscription_period is None:
        raise RuntimeError("Subscription offer lost its subscription period")
    return await _create_subscription_fulfillment(
        order=fulfilled_order,
        offer=offer,
        fulfillment_id=fulfillment_id,
        fulfilled_at=command.fulfilled_at,
        connection=connection,
    )


async def _create_subscription_fulfillment(
    *,
    order: Order,
    offer: BillingOffer,
    fulfillment_id: UUID,
    fulfilled_at: datetime,
    connection: AsyncConnection,
) -> BillingResult:
    """@brief 为首次订阅订单创建周期和权益 / Create a period and entitlements for an initial subscription order.

    @param order 已履约初始订单 / Fulfilled initial order.
    @param offer 周期性报价 / Subscription offer.
    @param fulfillment_id 不可变履约事实标识 / Immutable fulfillment-fact identity.
    @param fulfilled_at 履约时刻 / Fulfillment instant.
    @param connection 当前事务连接 / Current transactional connection.
    @return 带订阅的履约结果 / Fulfillment result with subscription.
    """

    assert offer.subscription_period is not None
    period_ends_at = fulfilled_at + offer.subscription_period
    entitlements = _new_entitlements(
        codes=offer.entitlement_codes,
        order_id=order.order_id,
        fulfillment_id=fulfillment_id,
        owner_id=order.buyer_id,
        starts_at=fulfilled_at,
        expires_at=period_ends_at,
    )
    for entitlement in entitlements:
        await _insert_entitlement(entitlement, fulfillment_id, connection)
    subscription = Subscription.activate(
        subscription_id=uuid4(),
        owner_id=order.buyer_id,
        product_id=order.product_id,
        offer_id=order.offer_id,
        source_order_id=order.order_id,
        entitlement_grant_ids=tuple(item.grant_id for item in entitlements),
        period_starts_at=fulfilled_at,
        period_ends_at=period_ends_at,
    )
    await _insert_subscription(
        subscription,
        current_order_id=order.order_id,
        connection=connection,
    )
    await _insert_subscription_period(
        subscription_id=subscription.subscription_id,
        order_id=order.order_id,
        period_starts_at=subscription.period_starts_at,
        period_ends_at=subscription.period_ends_at,
        connection=connection,
    )
    for entitlement in entitlements:
        await _attach_subscription_entitlement(
            subscription_id=subscription.subscription_id,
            source_order_id=order.order_id,
            grant_id=entitlement.grant_id,
            attached_at=fulfilled_at,
            connection=connection,
        )
    return BillingResult(
        BillingCode.SUCCESS,
        order=order,
        entitlements=entitlements,
        subscription=subscription,
    )


async def _renew_subscription_fulfillment(
    *,
    order: Order,
    offer: BillingOffer,
    operator_id: int,
    fulfilled_at: datetime,
    connection: AsyncConnection,
) -> BillingResult:
    """@brief 为续费订单创建新权益并推进订阅周期 / Create new entitlements and advance subscription period for a renewal order.

    @param order 已付款续费订单 / Paid renewal order.
    @param offer 周期性报价 / Subscription offer.
    @param operator_id 执行履约的后台人员 / Back-office operator performing fulfillment.
    @param fulfilled_at 履约时刻 / Fulfillment instant.
    @param connection 当前事务连接 / Current transactional connection.
    @return 带新周期的履约结果 / Fulfillment result with next period.
    """

    assert order.renewal_subscription_id is not None
    assert offer.subscription_period is not None
    subscription = await _lock_subscription(order.renewal_subscription_id, connection)
    if subscription is None:
        return BillingResult(BillingCode.NOT_FOUND, order=order)
    if (
        subscription.owner_id != order.buyer_id
        or subscription.product_id != order.product_id
        or subscription.offer_id != order.offer_id
    ):
        return BillingResult(
            BillingCode.FORBIDDEN, order=order, subscription=subscription
        )
    try:
        fulfilled_order = order.mark_fulfilled(fulfilled_at=fulfilled_at)
    except ValueError:
        return BillingResult(
            BillingCode.INVALID_STATE, order=order, subscription=subscription
        )
    starts_at = subscription.period_ends_at
    ends_at = starts_at + offer.subscription_period
    fulfillment_id = uuid4()
    entitlements = _new_entitlements(
        codes=offer.entitlement_codes,
        order_id=fulfilled_order.order_id,
        fulfillment_id=fulfillment_id,
        owner_id=fulfilled_order.buyer_id,
        starts_at=starts_at,
        expires_at=ends_at,
    )
    try:
        renewed = subscription.renew(
            renewed_at=fulfilled_at,
            next_period_ends_at=ends_at,
            entitlement_grant_ids=tuple(item.grant_id for item in entitlements),
        )
    except ValueError:
        return BillingResult(
            BillingCode.INVALID_STATE, order=order, subscription=subscription
        )
    await _persist_order(fulfilled_order, connection)
    await _insert_fulfillment(
        fulfillment_id=fulfillment_id,
        order_id=fulfilled_order.order_id,
        operator_id=operator_id,
        fulfilled_at=fulfilled_at,
        connection=connection,
    )
    for entitlement in entitlements:
        await _insert_entitlement(entitlement, fulfillment_id, connection)
    await _persist_subscription(
        renewed,
        current_order_id=fulfilled_order.order_id,
        connection=connection,
    )
    await _insert_subscription_period(
        subscription_id=renewed.subscription_id,
        order_id=fulfilled_order.order_id,
        period_starts_at=renewed.period_starts_at,
        period_ends_at=renewed.period_ends_at,
        connection=connection,
    )
    for entitlement in entitlements:
        await _attach_subscription_entitlement(
            subscription_id=renewed.subscription_id,
            source_order_id=fulfilled_order.order_id,
            grant_id=entitlement.grant_id,
            attached_at=fulfilled_at,
            connection=connection,
        )
    return BillingResult(
        BillingCode.SUCCESS,
        order=fulfilled_order,
        entitlements=entitlements,
        subscription=renewed,
    )


def _new_entitlements(
    *,
    codes: tuple[str, ...],
    order_id: UUID,
    fulfillment_id: UUID,
    owner_id: int,
    starts_at: datetime,
    expires_at: datetime | None,
) -> tuple[EntitlementGrant, ...]:
    """@brief 从报价权益代码构造用户权益 / Construct user entitlements from offer entitlement codes.

    @param codes 报价权益代码 / Offer entitlement codes.
    @param order_id 来源订单标识 / Source order identity.
    @param fulfillment_id 履约事实标识 / Fulfillment-fact identity.
    @param owner_id 用户拥有者标识 / User-owner identity.
    @param starts_at 生效时刻 / Effective-start instant.
    @param expires_at 可选到期时刻 / Optional expiration instant.
    @return 新权益授予元组 / Tuple of new entitlement grants.
    @note ``fulfillment_id`` 保留在函数签名以强调每项权益都属于同一履约事实；持久化时作为 FK 写入。
        / ``fulfillment_id`` remains in the signature to make each grant's shared fulfillment fact
        explicit; persistence writes it as an FK.
    """

    del fulfillment_id
    return tuple(
        EntitlementGrant.grant(
            grant_id=uuid4(),
            code=code,
            scope=EntitlementScope.USER,
            subject_id=owner_id,
            source_order_id=order_id,
            starts_at=starts_at,
            expires_at=expires_at,
        )
        for code in codes
    )


async def _revoke_order_entitlements(
    *,
    order_id: UUID,
    revoked_at: datetime,
    reason: str,
    connection: AsyncConnection,
) -> tuple[EntitlementGrant, ...]:
    """@brief 撤销尚未自然结束的订单权益 / Revoke order entitlements not naturally expired at the event instant.

    @param order_id 来源订单标识 / Source order identity.
    @param revoked_at 撤销生效时刻 / Revocation effective instant.
    @param reason 可审计撤销原因 / Auditable revocation reason.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已撤销权益 / Revoked entitlements.
    """

    entitlements = await _lock_active_order_entitlements(
        order_id=order_id,
        observed_at=revoked_at,
        connection=connection,
    )
    revoked: list[EntitlementGrant] = []
    for entitlement in entitlements:
        # @brief 为未来续费权益取合法撤销边界 / Choose a valid revocation boundary for a future renewal grant.
        # 预付续费可创建尚未开始的权益；在其开始边界撤销可确保权益永不生效，同时保持
        # ``ended_at >= starts_at`` 的领域时间不变量。/ A prepaid renewal may create a
        # not-yet-started grant; revoking it at the start boundary keeps it unusable while
        # preserving the ``ended_at >= starts_at`` temporal invariant.
        effective_revoked_at = max(revoked_at, entitlement.starts_at)
        transitioned = entitlement.revoke(
            revoked_at=effective_revoked_at,
            reason=reason,
        )
        await _persist_entitlement(transitioned, connection)
        revoked.append(transitioned)
    return tuple(revoked)


async def _revoke_current_subscription(
    *,
    order_id: UUID,
    revoked_at: datetime,
    reason: str,
    connection: AsyncConnection,
) -> Subscription | None:
    """@brief 仅撤销当前周期由该订单提供的订阅 / Revoke only a subscription whose current period is provided by this order.

    @param order_id 当前周期来源订单标识 / Current-period source-order identity.
    @param revoked_at 撤销生效时刻 / Revocation effective instant.
    @param reason 可审计撤销原因 / Auditable revocation reason.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已撤销订阅；不存在或周期已结束时为 None /
        Revoked subscription, or None when absent or period already ended.
    """

    subscription = await _lock_current_subscription_for_order(order_id, connection)
    if subscription is None:
        return None
    if (
        subscription.status is not SubscriptionStatus.ACTIVE
        or revoked_at >= subscription.period_ends_at
    ):
        return None
    # @brief 为未来续费周期取合法订阅撤销边界 / Choose a valid subscription-revocation boundary for a future renewal period.
    # 续费履约可把订阅行推进至已付款的未来周期；该周期开始前的退款或拒付仍有效，因此在未来
    # 开始边界结束订阅，而不是拒绝整个结算事务。/ Renewal fulfillment can advance the
    # subscription row to a paid future period; a refund or chargeback before that start is
    # still valid, so terminate at the future boundary rather than reject the settlement.
    effective_revoked_at = max(revoked_at, subscription.period_starts_at)
    revoked = subscription.revoke(
        revoked_at=effective_revoked_at,
        reason=reason,
    )
    await _persist_subscription(
        revoked,
        current_order_id=order_id,
        connection=connection,
    )
    return revoked


async def _identity_exists(user_id: int, connection: AsyncConnection) -> bool:
    """@brief 检查 identity 用户是否存在 / Check whether an identity user exists.

    @param user_id 用户标识 / User identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 用户存在时为 True / True when user exists.
    """

    row = await db_connection.fetch_one(
        "SELECT 1 FROM identity.users WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return row is not None


async def _lock_idempotency_key(
    idempotency_key: str,
    connection: AsyncConnection,
) -> None:
    """@brief 对一个 Billing 幂等键持有事务 advisory lock / Hold a transaction advisory lock for one Billing idempotency key.

    @param idempotency_key 业务幂等键 / Business idempotency key.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"billing:receipt:{idempotency_key}",),
        connection=connection,
    )


async def _lock_provider_event(
    event: PaymentEvent,
    connection: AsyncConnection,
) -> None:
    """@brief 对渠道事件键持有事务 advisory lock / Hold a transaction advisory lock for a provider-event key.

    @param event 已验证支付事件 / Verified payment event.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"billing:provider-event:{event.receipt_key}",),
        connection=connection,
    )


async def _lock_successful_payment(
    event: PaymentEvent,
    connection: AsyncConnection,
) -> None:
    """@brief 串行化同一成功付款参考号的归属判定 / Serialize ownership decisions for one successful-payment reference.

    @param event 已验证的成功付款事件 / Verified successful-payment event.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @note 仅 ``payment_succeeded`` 调用此函数；锁键固定为渠道和付款参考号，避免不同订单
        并发把同一渠道付款归属到不同订单。/ Only ``payment_succeeded`` calls this helper;
        the lock key is fixed to provider plus payment reference so concurrent orders cannot claim
        one provider payment differently.
    """

    if event.kind is not PaymentEventKind.PAYMENT_SUCCEEDED:
        raise ValueError("Only successful payments have a payment-ownership lock")
    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (
            "billing:successful-payment:"
            f"{event.provider.value}:{event.provider_payment_id}",
        ),
        connection=connection,
    )


async def _load_receipt(
    idempotency_key: str,
    operation_kind: str,
    actor_id: int | None,
    request_fingerprint: str,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 读取并验证 Billing 操作回执 / Load and validate a Billing operation receipt.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation_kind 预期操作种类 / Expected operation kind.
    @param actor_id 预期操作主体，可为 None / Expected actor identity, possibly None.
    @param request_fingerprint 完整命令语义的 SHA-256 指纹 / SHA-256 fingerprint of complete command semantics.
    @param connection 当前事务连接 / Current transactional connection.
    @return 回执结果对象；首次调用时为 None / Receipt result object, or None on first call.
    @raise _BillingConflictError 同一幂等键改变操作、主体或命令语义时抛出 /
        Raised when one idempotency key changes operation, actor, or command semantics.
    """

    row = await db_connection.fetch_one(
        "SELECT operation_kind, actor_id, request_fingerprint, result "
        "FROM billing.operation_receipts WHERE idempotency_key = %s",
        (idempotency_key,),
        mapping=True,
        connection=connection,
    )
    if row is None:
        return None
    value = _mapping(row)
    persisted_fingerprint = str(value["request_fingerprint"])
    if persisted_fingerprint == "0" * 64:
        raise _BillingConflictError(
            "Billing receipt has no request fingerprint and cannot be replayed"
        )
    if (
        str(value["operation_kind"]) != operation_kind
        or _optional_int(value["actor_id"]) != actor_id
        or persisted_fingerprint != request_fingerprint
    ):
        raise _BillingConflictError(
            "Billing idempotency key changed operation, actor, or request semantics"
        )
    raw_result: object = value["result"]
    decoded: object = (
        json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    )
    if not isinstance(decoded, Mapping):
        raise ValueError("Invalid Billing operation receipt")
    return cast(Mapping[str, Any], decoded)


async def _save_receipt(
    idempotency_key: str,
    operation_kind: str,
    actor_id: int | None,
    request_fingerprint: str,
    result: Mapping[str, object],
    connection: AsyncConnection,
) -> None:
    """@brief 与 Billing 状态变更同事务保存不可变回执 / Save immutable receipt in the Billing-state transaction.

    @param idempotency_key 幂等键 / Idempotency key.
    @param operation_kind 操作种类 / Operation kind.
    @param actor_id 可选操作主体 / Optional actor identity.
    @param request_fingerprint 完整命令语义的 SHA-256 指纹 / SHA-256 fingerprint of complete command semantics.
    @param result JSON 兼容结果 / JSON-compatible result.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO billing.operation_receipts "
        "(idempotency_key, operation_kind, actor_id, request_fingerprint, result) "
        "VALUES (%s, %s, %s, %s, CAST(%s AS JSONB))",
        (
            idempotency_key,
            operation_kind,
            actor_id,
            request_fingerprint,
            json.dumps(dict(result), ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        ),
        connection=connection,
    )


async def _lock_sellable_offer(
    offer_id: UUID,
    at: datetime,
    connection: AsyncConnection,
) -> BillingOffer | None:
    """@brief 锁定在指定时刻可售的报价 / Lock an offer that is sellable at an instant.

    @param offer_id 报价标识 / Offer identity.
    @param at 下单时刻 / Order-placement instant.
    @param connection 当前事务连接 / Current transactional connection.
    @return 可售报价；不可售或不存在时为 None / Sellable offer, or None when unavailable or absent.
    """

    row = await db_connection.fetch_one(
        "SELECT offer.offer_id, offer.product_id, offer.product_kind, offer.currency, "
        "offer.price_units, offer.entitlement_codes, offer.created_at, "
        "offer.subscription_period_seconds, offer.available_from, offer.available_until, "
        "offer.status FROM billing.offers AS offer "
        "JOIN billing.products AS product ON product.product_id = offer.product_id "
        "WHERE offer.offer_id = %s AND product.status = 'active' "
        "AND offer.status = 'active' AND product.kind = offer.product_kind "
        "AND (offer.available_from IS NULL OR offer.available_from <= %s) "
        "AND (offer.available_until IS NULL OR offer.available_until > %s) "
        "FOR UPDATE OF offer, product",
        (offer_id, at, at),
        mapping=True,
        connection=connection,
    )
    return _offer_from_row(_mapping(row)) if row is not None else None


async def _load_order_offer(
    order: Order,
    connection: AsyncConnection,
) -> BillingOffer | None:
    """@brief 读取与订单冻结快照完全一致的报价 / Read an offer exactly matching an order's frozen snapshot.

    @param order 已锁定订单 / Locked order.
    @param connection 当前事务连接 / Current transactional connection.
    @return 匹配报价；目录损坏时为 None / Matching offer, or None on catalog corruption.
    """

    row = await db_connection.fetch_one(
        "SELECT offer.offer_id, offer.product_id, offer.product_kind, offer.currency, "
        "offer.price_units, offer.entitlement_codes, offer.created_at, "
        "offer.subscription_period_seconds, offer.available_from, offer.available_until, "
        "offer.status FROM billing.offers AS offer "
        "JOIN billing.products AS product ON product.product_id = offer.product_id "
        "WHERE offer.offer_id = %s AND offer.product_id = %s "
        "AND offer.product_kind = %s AND offer.currency = %s AND offer.price_units = %s "
        "AND product.kind = offer.product_kind FOR SHARE OF offer, product",
        (
            order.offer_id,
            order.product_id,
            order.product_kind.value,
            order.price.currency,
            order.price.units,
        ),
        mapping=True,
        connection=connection,
    )
    return _offer_from_row(_mapping(row)) if row is not None else None


async def _read_order(
    order_id: UUID,
    connection: AsyncConnection,
) -> Order | None:
    """@brief 读取订单及其支付事件去重键 / Read an order and its payment-event deduplication keys.

    @param order_id 订单标识 / Order identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 订单；不存在时为 None / Order, or None when absent.
    """

    row = await _select_order(order_id, for_update=False, connection=connection)
    return _order_from_row(_mapping(row)) if row is not None else None


async def _lock_order(
    order_id: UUID,
    connection: AsyncConnection,
) -> Order | None:
    """@brief 锁定订单及其已处理事件键 / Lock an order and its processed-event keys.

    @param order_id 订单标识 / Order identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已锁定订单；不存在时为 None / Locked order, or None when absent.
    """

    row = await _select_order(order_id, for_update=True, connection=connection)
    return _order_from_row(_mapping(row)) if row is not None else None


async def _select_order(
    order_id: UUID,
    *,
    for_update: bool,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 执行订单选择并可选加行锁 / Execute order selection with an optional row lock.

    @param order_id 订单标识 / Order identity.
    @param for_update 是否加写锁 / Whether to take a write lock.
    @param connection 当前事务连接 / Current transactional connection.
    @return 原始行映射；不存在时为 None / Raw row mapping, or None when absent.
    """

    lock_clause = " FOR UPDATE OF orders" if for_update else ""
    row = await db_connection.fetch_one(
        "SELECT orders.order_id, orders.buyer_id, orders.product_id, orders.offer_id, "
        "orders.renewal_subscription_id, orders.product_kind, orders.currency, "
        "orders.price_units, orders.status, orders.created_at, orders.payment_provider, "
        "orders.provider_payment_id, orders.paid_at, orders.fulfilled_at, "
        "orders.refund_requested_at, orders.refunded_at, orders.cancelled_at, "
        "orders.chargeback_at, orders.version, "
        "COALESCE(ARRAY(SELECT event.provider || ':' || event.provider_event_id "
        "FROM billing.payment_events AS event WHERE event.order_id = orders.order_id), "
        "ARRAY[]::TEXT[]) AS event_keys "
        "FROM billing.orders AS orders WHERE orders.order_id = %s" + lock_clause,
        (order_id,),
        mapping=True,
        connection=connection,
    )
    return _mapping(row) if row is not None else None


async def _read_refund(
    refund_id: UUID,
    connection: AsyncConnection,
) -> Refund | None:
    """@brief 读取退款聚合 / Read a refund aggregate.

    @param refund_id 退款标识 / Refund identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 退款；不存在时为 None / Refund, or None when absent.
    """

    row = await _select_refund(refund_id, for_update=False, connection=connection)
    return _refund_from_row(_mapping(row)) if row is not None else None


async def _lock_refund(
    refund_id: UUID,
    connection: AsyncConnection,
) -> Refund | None:
    """@brief 锁定退款聚合 / Lock a refund aggregate.

    @param refund_id 退款标识 / Refund identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已锁定退款；不存在时为 None / Locked refund, or None when absent.
    """

    row = await _select_refund(refund_id, for_update=True, connection=connection)
    return _refund_from_row(_mapping(row)) if row is not None else None


async def _select_refund(
    refund_id: UUID,
    *,
    for_update: bool,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 执行退款选择并可选加行锁 / Execute refund selection with an optional row lock.

    @param refund_id 退款标识 / Refund identity.
    @param for_update 是否加写锁 / Whether to take a write lock.
    @param connection 当前事务连接 / Current transactional connection.
    @return 原始行映射；不存在时为 None / Raw row mapping, or None when absent.
    """

    lock_clause = " FOR UPDATE" if for_update else ""
    row = await db_connection.fetch_one(
        "SELECT refund_id, order_id, requester_id, currency, amount_units, reason, "
        "status, requested_at, reviewer_id, reviewed_at, review_note, "
        "settlement_provider, provider_settlement_id, settled_at, cancelled_at, version "
        "FROM billing.refunds WHERE refund_id = %s" + lock_clause,
        (refund_id,),
        mapping=True,
        connection=connection,
    )
    return _mapping(row) if row is not None else None


async def _lock_subscription(
    subscription_id: UUID,
    connection: AsyncConnection,
) -> Subscription | None:
    """@brief 锁定订阅及当前周期权益标识 / Lock a subscription and current-period entitlement identities.

    @param subscription_id 订阅标识 / Subscription identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已锁定订阅；不存在时为 None / Locked subscription, or None when absent.
    """

    row = await _select_subscription(
        "subscription_id = %s",
        (subscription_id,),
        for_update=True,
        connection=connection,
    )
    return _subscription_from_row(_mapping(row)) if row is not None else None


async def _has_open_renewal_order(
    subscription_id: UUID,
    connection: AsyncConnection,
) -> bool:
    """@brief 检查订阅是否已有未终态续费订单 / Check whether a subscription already has an open renewal order.

    @param subscription_id 待续费订阅标识 / Subscription identity being renewed.
    @param connection 当前事务连接；调用方必须已持有该订阅行的写锁 /
        Current transaction connection; the caller must already hold the subscription row write lock.
    @return 存在等待付款、已付款待履约或退款结算中的续费订单时为 True /
        True when an awaiting-payment, paid-awaiting-fulfillment, or refund-pending renewal exists.
    @note 不为订单再加锁，以避免与“先锁订单再锁订阅”的履约路径形成锁顺序环；订阅行锁
        串行化正常下单，数据库 partial unique index 防住越过该路径的并发写入。/
        Do not lock orders here: that would create a lock-order cycle with fulfillment, which locks
        order then subscription. The subscription lock serializes normal placement, while the
        database partial unique index protects concurrent writes that bypass this path.
    """

    row = await db_connection.fetch_one(
        "SELECT 1 FROM billing.orders "
        "WHERE renewal_subscription_id = %s "
        "AND status IN ('awaiting_payment', 'paid', 'refund_pending') "
        "LIMIT 1",
        (subscription_id,),
        connection=connection,
    )
    return row is not None


async def _lock_current_subscription_for_order(
    order_id: UUID,
    connection: AsyncConnection,
) -> Subscription | None:
    """@brief 锁定把给定订单作为当前周期来源的订阅 / Lock a subscription whose current period comes from an order.

    @param order_id 当前周期来源订单标识 / Current-period source-order identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已锁定订阅；不存在时为 None / Locked subscription, or None when absent.
    """

    row = await _select_subscription(
        "current_order_id = %s",
        (order_id,),
        for_update=True,
        connection=connection,
    )
    return _subscription_from_row(_mapping(row)) if row is not None else None


async def _select_subscription(
    predicate: str,
    params: tuple[object, ...],
    *,
    for_update: bool,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 选择一份订阅并携带当前 grant IDs / Select one subscription with current grant IDs.

    @param predicate 已固定的内部 SQL 谓词 / Fixed internal SQL predicate.
    @param params 谓词参数 / Predicate parameters.
    @param for_update 是否加写锁 / Whether to take a write lock.
    @param connection 当前事务连接 / Current transactional connection.
    @return 原始行映射；不存在时为 None / Raw row mapping, or None when absent.
    """

    lock_clause = " FOR UPDATE OF subscriptions" if for_update else ""
    row = await db_connection.fetch_one(
        "SELECT subscriptions.subscription_id, subscriptions.owner_id, "
        "subscriptions.product_id, subscriptions.offer_id, subscriptions.source_order_id, "
        "subscriptions.current_order_id, subscriptions.period_starts_at, "
        "subscriptions.period_ends_at, subscriptions.status, "
        "subscriptions.cancellation_requested_at, subscriptions.ended_at, "
        "subscriptions.revocation_reason, subscriptions.version, "
        "COALESCE(ARRAY(SELECT link.grant_id "
        "FROM billing.subscription_entitlement_grants AS link "
        "WHERE link.subscription_id = subscriptions.subscription_id "
        "AND link.source_order_id = subscriptions.current_order_id "
        "ORDER BY link.grant_id), ARRAY[]::UUID[]) AS grant_ids "
        "FROM billing.subscriptions AS subscriptions WHERE " + predicate + lock_clause,
        params,
        mapping=True,
        connection=connection,
    )
    return _mapping(row) if row is not None else None


async def _current_subscription_order_id(
    subscription_id: UUID,
    connection: AsyncConnection,
) -> UUID:
    """@brief 读取已锁订阅的当前来源订单标识 / Read current source-order identity of an already locked subscription.

    @param subscription_id 订阅标识 / Subscription identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return 当前周期来源订单标识 / Current-period source-order identity.
    @raise RuntimeError 锁定订阅消失时抛出 / Raised when locked subscription disappears.
    """

    row = await db_connection.fetch_one(
        "SELECT current_order_id FROM billing.subscriptions WHERE subscription_id = %s",
        (subscription_id,),
        connection=connection,
    )
    if row is None:
        raise RuntimeError("Locked subscription disappeared")
    return cast(UUID, row[0])


async def _lock_active_order_entitlements(
    *,
    order_id: UUID,
    observed_at: datetime,
    connection: AsyncConnection,
) -> tuple[EntitlementGrant, ...]:
    """@brief 锁定订单中尚未自然结束的权益 / Lock order entitlements not naturally expired at an instant.

    @param order_id 来源订单标识 / Source order identity.
    @param observed_at 观察时刻 / Observation instant.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已锁定有效权益 / Locked active entitlements.
    """

    rows = await db_connection.fetch_all(
        "SELECT grant_id, code, scope, subject_id, source_order_id, starts_at, "
        "expires_at, status, ended_at, revocation_reason, version "
        "FROM billing.entitlement_grants WHERE source_order_id = %s "
        "AND status = 'active' AND (expires_at IS NULL OR expires_at > %s) "
        "ORDER BY grant_id FOR UPDATE",
        (order_id, observed_at),
        mapping=True,
        connection=connection,
    )
    return tuple(_grant_from_row(_mapping(row)) for row in rows)


async def _load_provider_event(
    event: PaymentEvent,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 按渠道事件唯一键读取事实 / Read a fact by provider-event uniqueness key.

    @param event 待比较的支付事件 / Payment event to compare.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已记录事实；不存在时为 None / Recorded fact, or None when absent.
    """

    row = await db_connection.fetch_one(
        "SELECT event_id, provider, provider_event_id, provider_payment_id, order_id, "
        "refund_id, event_kind, currency, amount_units, occurred_at "
        "FROM billing.payment_events WHERE provider = %s AND provider_event_id = %s",
        (event.provider.value, event.provider_event_id),
        mapping=True,
        connection=connection,
    )
    return _mapping(row) if row is not None else None


async def _load_successful_payment(
    event: PaymentEvent,
    connection: AsyncConnection,
) -> Mapping[str, Any] | None:
    """@brief 按渠道和付款参考号读取已归属的成功付款 / Read an already-owned successful payment by provider and payment reference.

    @param event 待归属的成功付款事件 / Successful payment event to attribute.
    @param connection 当前事务连接 / Current transactional connection.
    @return 已归属的成功付款事实；未归属时为 None /
        Attributed successful-payment fact, or None when unclaimed.
    @raise ValueError 事件不是成功付款时抛出 /
        Raised when the event is not a successful payment.
    @note 调用方必须先通过 ``_lock_successful_payment`` 获取相同维度的 advisory lock。
        / The caller must first hold the matching advisory lock via
        ``_lock_successful_payment``.
    """

    if event.kind is not PaymentEventKind.PAYMENT_SUCCEEDED:
        raise ValueError("Only successful payments can have payment ownership")
    row = await db_connection.fetch_one(
        "SELECT event_id, provider, provider_event_id, provider_payment_id, order_id, "
        "refund_id, event_kind, currency, amount_units, occurred_at "
        "FROM billing.payment_events WHERE provider = %s "
        "AND provider_payment_id = %s AND event_kind = %s",
        (
            event.provider.value,
            event.provider_payment_id,
            PaymentEventKind.PAYMENT_SUCCEEDED.value,
        ),
        mapping=True,
        connection=connection,
    )
    return _mapping(row) if row is not None else None


def _validate_existing_provider_event(
    existing: Mapping[str, Any],
    event: PaymentEvent,
    *,
    refund_id: UUID | None,
) -> None:
    """@brief 验证重放渠道事件没有改变事实语义 / Validate replayed provider event has not changed fact semantics.

    @param existing 已持久化事件行 / Persisted event row.
    @param event 新提交的支付事件 / Newly submitted payment event.
    @param refund_id 预期可选退款标识 / Expected optional refund identity.
    @return None / None.
    @raise _BillingConflictError 渠道唯一键重用了不同事实时抛出 /
        Raised when provider uniqueness key is reused for a different fact.
    """

    actual_refund_id = existing.get("refund_id")
    same = (
        str(existing["provider"]) == event.provider.value
        and str(existing["provider_event_id"]) == event.provider_event_id
        and str(existing["provider_payment_id"]) == event.provider_payment_id
        and cast(UUID, existing["order_id"]) == event.order_id
        and _optional_uuid(actual_refund_id) == refund_id
        and str(existing["event_kind"]) == event.kind.value
        and str(existing["currency"]) == event.amount.currency
        and int(existing["amount_units"]) == event.amount.units
        and _as_utc(cast(datetime, existing["occurred_at"])) == event.occurred_at
    )
    if not same:
        raise _BillingConflictError(
            "Provider event key was reused with different Billing semantics"
        )


def _validate_existing_successful_payment(
    existing: Mapping[str, Any],
    event: PaymentEvent,
) -> None:
    """@brief 验证成功付款参考号的既有归属与新事件完全一致 / Verify an existing successful-payment ownership matches the new event.

    @param existing 已归属的成功付款事实 / Existing attributed successful-payment fact.
    @param event 新提交的成功付款事件 / Newly submitted successful-payment event.
    @return None / None.
    @raise ValueError 事件不是成功付款时抛出 /
        Raised when the event is not a successful payment.
    @raise _BillingConflictError 同一渠道付款参考号被映射到不同订单或语义时抛出 /
        Raised when one provider payment reference maps to another order or semantics.
    """

    if event.kind is not PaymentEventKind.PAYMENT_SUCCEEDED:
        raise ValueError("Only successful payments can be compared for ownership")
    same = (
        str(existing["provider"]) == event.provider.value
        and str(existing["provider_event_id"]) == event.provider_event_id
        and str(existing["provider_payment_id"]) == event.provider_payment_id
        and _uuid_value(
            existing["order_id"],
            field="Existing successful-payment order ID",
        )
        == event.order_id
        and _optional_uuid(existing.get("refund_id")) is None
        and str(existing["event_kind"]) == PaymentEventKind.PAYMENT_SUCCEEDED.value
        and str(existing["currency"]) == event.amount.currency
        and int(existing["amount_units"]) == event.amount.units
        and _as_utc(cast(datetime, existing["occurred_at"])) == event.occurred_at
    )
    if not same:
        raise _BillingConflictError(
            "Provider payment ID is already mapped to different Billing semantics"
        )


async def _insert_order_from_offer(
    command: PlaceOrder,
    offer: BillingOffer,
    connection: AsyncConnection,
) -> BillingResult:
    """@brief 从报价冻结订单并持久化 / Freeze an order from an offer and persist it.

    @param command 下单命令 / Order-placement command.
    @param offer 已锁定可售报价 / Locked sellable offer.
    @param connection 当前事务连接 / Current transactional connection.
    @return 成功订单结果 / Successful order result.
    """

    order = Order.create(
        order_id=command.order_id,
        buyer_id=command.buyer_id,
        product_id=offer.product_id,
        offer_id=offer.offer_id,
        product_kind=offer.product_kind,
        price=offer.price,
        created_at=command.created_at,
        renewal_subscription_id=command.renewal_subscription_id,
    )
    await db_connection.execute(
        "INSERT INTO billing.orders ("
        "order_id, buyer_id, product_id, offer_id, renewal_subscription_id, "
        "product_kind, currency, price_units, status, created_at, version, updated_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
        (
            order.order_id,
            order.buyer_id,
            order.product_id,
            order.offer_id,
            command.renewal_subscription_id,
            order.product_kind.value,
            order.price.currency,
            order.price.units,
            order.status.value,
            order.created_at,
            order.version,
        ),
        connection=connection,
    )
    return BillingResult(BillingCode.SUCCESS, order=order)


async def _persist_order(order: Order, connection: AsyncConnection) -> None:
    """@brief 以 OCC 保存订单状态机变迁 / Persist an order state-machine transition with OCC.

    @param order 已变迁订单 / Transitioned order.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise RuntimeError 订单在持锁期间发生意外并发变更时抛出 /
        Raised when order unexpectedly changes while locked.
    """

    changed = await db_connection.execute(
        "UPDATE billing.orders SET status = %s, payment_provider = %s, "
        "provider_payment_id = %s, paid_at = %s, fulfilled_at = %s, "
        "refund_requested_at = %s, refunded_at = %s, cancelled_at = %s, "
        "chargeback_at = %s, version = %s, updated_at = CURRENT_TIMESTAMP "
        "WHERE order_id = %s AND version = %s",
        (
            order.status.value,
            order.payment_provider.value
            if order.payment_provider is not None
            else None,
            order.provider_payment_id,
            order.paid_at,
            order.fulfilled_at,
            order.refund_requested_at,
            order.refunded_at,
            order.cancelled_at,
            order.chargeback_at,
            order.version,
            order.order_id,
            order.version - 1,
        ),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Billing order changed while it was locked")


async def _insert_payment_event(
    event: PaymentEvent,
    *,
    refund_id: UUID | None,
    connection: AsyncConnection,
) -> None:
    """@brief 追加不可变支付渠道事实 / Append an immutable payment-provider fact.

    @param event 已验证支付事件 / Verified payment event.
    @param refund_id 可选关联退款标识 / Optional associated refund identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO billing.payment_events ("
        "event_id, provider, provider_event_id, provider_payment_id, order_id, "
        "refund_id, event_kind, currency, amount_units, occurred_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            event.event_id,
            event.provider.value,
            event.provider_event_id,
            event.provider_payment_id,
            event.order_id,
            refund_id,
            event.kind.value,
            event.amount.currency,
            event.amount.units,
            event.occurred_at,
        ),
        connection=connection,
    )


async def _insert_fulfillment(
    *,
    fulfillment_id: UUID,
    order_id: UUID,
    operator_id: int,
    fulfilled_at: datetime,
    connection: AsyncConnection,
) -> None:
    """@brief 追加不可变的订单履约事实 / Append an immutable order-fulfillment fact.

    @param fulfillment_id 履约事实标识 / Fulfillment-fact identity.
    @param order_id 所属订单标识 / Owning order identity.
    @param operator_id 执行履约的后台人员 / Back-office operator performing fulfillment.
    @param fulfilled_at 履约时刻 / Fulfillment instant.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO billing.fulfillments "
        "(fulfillment_id, order_id, operator_id, fulfilled_at) "
        "VALUES (%s, %s, %s, %s)",
        (fulfillment_id, order_id, operator_id, fulfilled_at),
        connection=connection,
    )


async def _insert_entitlement(
    entitlement: EntitlementGrant,
    fulfillment_id: UUID,
    connection: AsyncConnection,
) -> None:
    """@brief 持久化新权益授予 / Persist a new entitlement grant.

    @param entitlement 领域权益授予 / Domain entitlement grant.
    @param fulfillment_id 不可变履约事实标识 / Immutable fulfillment-fact identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO billing.entitlement_grants ("
        "grant_id, code, scope, subject_id, source_order_id, fulfillment_id, "
        "starts_at, expires_at, status, ended_at, revocation_reason, version, updated_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
        (
            entitlement.grant_id,
            entitlement.code,
            entitlement.scope.value,
            entitlement.subject_id,
            entitlement.source_order_id,
            fulfillment_id,
            entitlement.starts_at,
            entitlement.expires_at,
            entitlement.status.value,
            entitlement.ended_at,
            entitlement.revocation_reason,
            entitlement.version,
        ),
        connection=connection,
    )


async def _persist_entitlement(
    entitlement: EntitlementGrant,
    connection: AsyncConnection,
) -> None:
    """@brief 以 OCC 保存权益到期或撤销 / Persist an entitlement expiry or revocation with OCC.

    @param entitlement 已变迁权益 / Transitioned entitlement.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise RuntimeError 权益在持锁期间发生意外并发变更时抛出 /
        Raised when entitlement unexpectedly changes while locked.
    """

    changed = await db_connection.execute(
        "UPDATE billing.entitlement_grants SET status = %s, ended_at = %s, "
        "revocation_reason = %s, version = %s, updated_at = CURRENT_TIMESTAMP "
        "WHERE grant_id = %s AND version = %s",
        (
            entitlement.status.value,
            entitlement.ended_at,
            entitlement.revocation_reason,
            entitlement.version,
            entitlement.grant_id,
            entitlement.version - 1,
        ),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Billing entitlement changed while it was locked")


async def _insert_subscription(
    subscription: Subscription,
    *,
    current_order_id: UUID,
    connection: AsyncConnection,
) -> None:
    """@brief 持久化首次创建的订阅 / Persist an initially created subscription.

    @param subscription 领域订阅 / Domain subscription.
    @param current_order_id 当前周期来源订单标识 / Current-period source-order identity.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO billing.subscriptions ("
        "subscription_id, owner_id, product_id, offer_id, source_order_id, "
        "current_order_id, period_starts_at, period_ends_at, status, "
        "cancellation_requested_at, ended_at, revocation_reason, version, updated_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
        (
            subscription.subscription_id,
            subscription.owner_id,
            subscription.product_id,
            subscription.offer_id,
            subscription.source_order_id,
            current_order_id,
            subscription.period_starts_at,
            subscription.period_ends_at,
            subscription.status.value,
            subscription.cancellation_requested_at,
            subscription.ended_at,
            subscription.revocation_reason,
            subscription.version,
        ),
        connection=connection,
    )


async def _persist_subscription(
    subscription: Subscription,
    *,
    current_order_id: UUID,
    connection: AsyncConnection,
) -> None:
    """@brief 以 OCC 保存订阅变迁和当前订单 / Persist a subscription transition and current order with OCC.

    @param subscription 已变迁订阅 / Transitioned subscription.
    @param current_order_id 新或现有当前周期来源订单 / New or existing current-period source order.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise RuntimeError 订阅在持锁期间发生意外并发变更时抛出 /
        Raised when subscription unexpectedly changes while locked.
    """

    changed = await db_connection.execute(
        "UPDATE billing.subscriptions SET current_order_id = %s, period_starts_at = %s, "
        "period_ends_at = %s, status = %s, cancellation_requested_at = %s, "
        "ended_at = %s, revocation_reason = %s, version = %s, "
        "updated_at = CURRENT_TIMESTAMP WHERE subscription_id = %s AND version = %s",
        (
            current_order_id,
            subscription.period_starts_at,
            subscription.period_ends_at,
            subscription.status.value,
            subscription.cancellation_requested_at,
            subscription.ended_at,
            subscription.revocation_reason,
            subscription.version,
            subscription.subscription_id,
            subscription.version - 1,
        ),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Billing subscription changed while it was locked")


async def _insert_subscription_period(
    *,
    subscription_id: UUID,
    order_id: UUID,
    period_starts_at: datetime,
    period_ends_at: datetime,
    connection: AsyncConnection,
) -> None:
    """@brief 追加不可变订阅周期事实 / Append an immutable subscription-period fact.

    @param subscription_id 订阅标识 / Subscription identity.
    @param order_id 支付该周期的订单标识 / Order identity funding this period.
    @param period_starts_at 周期开始时刻 / Period-start instant.
    @param period_ends_at 周期结束时刻 / Period-end instant.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO billing.subscription_periods "
        "(subscription_id, order_id, period_starts_at, period_ends_at) "
        "VALUES (%s, %s, %s, %s)",
        (subscription_id, order_id, period_starts_at, period_ends_at),
        connection=connection,
    )


async def _attach_subscription_entitlement(
    *,
    subscription_id: UUID,
    source_order_id: UUID,
    grant_id: UUID,
    attached_at: datetime,
    connection: AsyncConnection,
) -> None:
    """@brief 追加订阅与权益之间的不可变关联 / Append immutable association between subscription and entitlement.

    @param subscription_id 订阅标识 / Subscription identity.
    @param source_order_id 产生权益的订单标识 / Order identity producing entitlement.
    @param grant_id 权益授予标识 / Entitlement-grant identity.
    @param attached_at 关联生效时刻 / Association effective instant.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO billing.subscription_entitlement_grants "
        "(subscription_id, source_order_id, grant_id, attached_at) "
        "VALUES (%s, %s, %s, %s)",
        (subscription_id, source_order_id, grant_id, attached_at),
        connection=connection,
    )


async def _insert_refund(refund: Refund, connection: AsyncConnection) -> None:
    """@brief 持久化新的退款申请 / Persist a new refund request.

    @param refund 领域退款聚合 / Domain refund aggregate.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO billing.refunds ("
        "refund_id, order_id, requester_id, currency, amount_units, reason, status, "
        "requested_at, reviewer_id, reviewed_at, review_note, settlement_provider, "
        "provider_settlement_id, settled_at, cancelled_at, version, updated_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
        (
            refund.refund_id,
            refund.order_id,
            refund.requester_id,
            refund.amount.currency,
            refund.amount.units,
            refund.reason,
            refund.status.value,
            refund.requested_at,
            refund.reviewer_id,
            refund.reviewed_at,
            refund.review_note,
            (
                refund.settlement_provider.value
                if refund.settlement_provider is not None
                else None
            ),
            refund.provider_settlement_id,
            refund.settled_at,
            refund.cancelled_at,
            refund.version,
        ),
        connection=connection,
    )


async def _persist_refund(refund: Refund, connection: AsyncConnection) -> None:
    """@brief 以 OCC 保存退款审核或结算状态 / Persist refund review or settlement state with OCC.

    @param refund 已变迁退款 / Transitioned refund.
    @param connection 当前事务连接 / Current transactional connection.
    @return None / None.
    @raise RuntimeError 退款在持锁期间发生意外并发变更时抛出 /
        Raised when refund unexpectedly changes while locked.
    """

    changed = await db_connection.execute(
        "UPDATE billing.refunds SET status = %s, reviewer_id = %s, reviewed_at = %s, "
        "review_note = %s, settlement_provider = %s, provider_settlement_id = %s, "
        "settled_at = %s, cancelled_at = %s, version = %s, "
        "updated_at = CURRENT_TIMESTAMP WHERE refund_id = %s AND version = %s",
        (
            refund.status.value,
            refund.reviewer_id,
            refund.reviewed_at,
            refund.review_note,
            (
                refund.settlement_provider.value
                if refund.settlement_provider is not None
                else None
            ),
            refund.provider_settlement_id,
            refund.settled_at,
            refund.cancelled_at,
            refund.version,
            refund.refund_id,
            refund.version - 1,
        ),
        connection=connection,
    )
    if changed != 1:
        raise RuntimeError("Billing refund changed while it was locked")


def _mapping(row: object) -> Mapping[str, Any]:
    """@brief 将 SQLAlchemy 映射行收窄为普通 Mapping / Narrow a SQLAlchemy mapping row to a normal Mapping.

    @param row 原始映射行 / Raw mapping row.
    @return 带字符串列名的映射 / Mapping with string column names.
    @raise TypeError 行不是映射时抛出 / Raised when row is not a mapping.
    """

    if not isinstance(row, Mapping):
        raise TypeError("Expected a database mapping row")
    return cast(Mapping[str, Any], row)


def _place_order_request_fingerprint(command: PlaceOrder) -> str:
    """@brief 计算下单命令的规范语义指纹 / Compute the canonical semantics fingerprint for order placement.

    @param command 已规范化下单命令 / Normalized order-placement command.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    """

    return _request_fingerprint(
        {
            "operation_kind": "order.place",
            "buyer_id": command.buyer_id,
            "offer_id": str(command.offer_id),
            "order_id": str(command.order_id),
            "created_at": _instant_mapping(command.created_at),
            "renewal_subscription_id": _optional_uuid_mapping(
                command.renewal_subscription_id
            ),
        }
    )


def _record_payment_event_request_fingerprint(command: RecordPaymentEvent) -> str:
    """@brief 计算支付事件记录命令的规范语义指纹 / Compute the canonical semantics fingerprint for payment-event recording.

    @param command 已验证支付事件记录命令 / Verified payment-event recording command.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    """

    return _request_fingerprint(
        {
            "operation_kind": "payment_event.record",
            "event": _payment_event_request_mapping(command.event),
        }
    )


def _fulfill_order_request_fingerprint(command: FulfillOrder) -> str:
    """@brief 计算订单履约命令的规范语义指纹 / Compute the canonical semantics fingerprint for order fulfillment.

    @param command 已规范化订单履约命令 / Normalized order-fulfillment command.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    """

    return _request_fingerprint(
        {
            "operation_kind": "order.fulfill",
            "order_id": str(command.order_id),
            "operator_id": command.operator_id,
            "fulfilled_at": _instant_mapping(command.fulfilled_at),
        }
    )


def _request_refund_request_fingerprint(command: RequestRefund) -> str:
    """@brief 计算退款申请命令的规范语义指纹 / Compute the canonical semantics fingerprint for a refund request.

    @param command 已规范化退款申请命令 / Normalized refund-request command.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    """

    return _request_fingerprint(
        {
            "operation_kind": "refund.request",
            "requester_id": command.requester_id,
            "order_id": str(command.order_id),
            "refund_id": str(command.refund_id),
            "reason": command.reason,
            "requested_at": _instant_mapping(command.requested_at),
        }
    )


def _review_refund_request_fingerprint(command: ReviewRefund) -> str:
    """@brief 计算退款审核命令的规范语义指纹 / Compute the canonical semantics fingerprint for refund review.

    @param command 已规范化退款审核命令 / Normalized refund-review command.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    """

    return _request_fingerprint(
        {
            "operation_kind": "refund.review",
            "refund_id": str(command.refund_id),
            "reviewer_id": command.reviewer_id,
            "decision": command.decision.value,
            "reviewed_at": _instant_mapping(command.reviewed_at),
            "note": command.note,
        }
    )


def _settle_refund_request_fingerprint(command: SettleRefund) -> str:
    """@brief 计算退款结算命令的规范语义指纹 / Compute the canonical semantics fingerprint for refund settlement.

    @param command 已验证退款结算命令 / Verified refund-settlement command.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    """

    return _request_fingerprint(
        {
            "operation_kind": "refund.settle",
            "refund_id": str(command.refund_id),
            "event": _payment_event_request_mapping(command.event),
        }
    )


def _cancel_subscription_request_fingerprint(command: CancelSubscription) -> str:
    """@brief 计算订阅取消命令的规范语义指纹 / Compute the canonical semantics fingerprint for subscription cancellation.

    @param command 已规范化订阅取消命令 / Normalized subscription-cancellation command.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    """

    return _request_fingerprint(
        {
            "operation_kind": "subscription.cancel",
            "owner_id": command.owner_id,
            "subscription_id": str(command.subscription_id),
            "requested_at": _instant_mapping(command.requested_at),
        }
    )


def _payment_event_request_mapping(event: PaymentEvent) -> Mapping[str, object]:
    """@brief 将完整支付事件编码为规范请求字段 / Encode a complete payment event into canonical request fields.

    @param event 已验证支付事件 / Verified payment event.
    @return 只含 JSON 基础类型的完整事件语义 / Complete event semantics using JSON primitive types only.
    """

    return {
        "event_id": str(event.event_id),
        "provider": event.provider.value,
        "provider_event_id": event.provider_event_id,
        "provider_payment_id": event.provider_payment_id,
        "order_id": str(event.order_id),
        "kind": event.kind.value,
        "amount": {
            "currency": event.amount.currency,
            "units": event.amount.units,
        },
        "occurred_at": _instant_mapping(event.occurred_at),
    }


def _request_fingerprint(value: Mapping[str, object]) -> str:
    """@brief 对完整规范命令语义生成 SHA-256 / Generate SHA-256 over complete canonical command semantics.

    @param value 只含确定性 JSON 基础类型的命令语义 / Command semantics containing deterministic JSON primitive types only.
    @return 小写 SHA-256 十六进制摘要 / Lowercase SHA-256 hexadecimal digest.
    @note 幂等键不参与摘要：它选择回执槽位，而摘要负责验证该槽位内的操作语义。
        / The idempotency key is deliberately excluded: it selects the receipt slot, while this
        digest verifies the operation semantics stored in that slot.
    """

    encoded = json.dumps(
        dict(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _offer_from_row(row: Mapping[str, Any]) -> BillingOffer:
    """@brief 从数据库行还原 Billing 报价 / Restore a Billing offer from a database row.

    @param row 报价行映射 / Offer-row mapping.
    @return 领域报价 / Domain offer.
    """

    period_seconds = row.get("subscription_period_seconds")
    return BillingOffer(
        offer_id=cast(UUID, row["offer_id"]),
        product_id=cast(UUID, row["product_id"]),
        product_kind=ProductKind(str(row["product_kind"])),
        price=PaymentAmount(str(row["currency"]), int(row["price_units"])),
        entitlement_codes=_json_codes(row["entitlement_codes"]),
        created_at=_as_utc(cast(datetime, row["created_at"])),
        subscription_period=(
            timedelta(seconds=int(period_seconds))
            if period_seconds is not None
            else None
        ),
        available_from=_optional_datetime(row.get("available_from")),
        available_until=_optional_datetime(row.get("available_until")),
        status=_offer_status(row["status"]),
    )


def _order_from_row(row: Mapping[str, Any]) -> Order:
    """@brief 从数据库行还原订单聚合 / Restore an order aggregate from a database row.

    @param row 订单行映射 / Order-row mapping.
    @return 领域订单 / Domain order.
    """

    raw_event_keys = row.get("event_keys", ())
    event_keys = tuple(str(item) for item in cast(Sequence[object], raw_event_keys))
    return Order(
        order_id=cast(UUID, row["order_id"]),
        buyer_id=int(row["buyer_id"]),
        product_id=cast(UUID, row["product_id"]),
        offer_id=cast(UUID, row["offer_id"]),
        product_kind=ProductKind(str(row["product_kind"])),
        price=PaymentAmount(str(row["currency"]), int(row["price_units"])),
        status=OrderStatus(str(row["status"])),
        created_at=_as_utc(cast(datetime, row["created_at"])),
        renewal_subscription_id=_optional_uuid(row.get("renewal_subscription_id")),
        payment_provider=(
            PaymentProvider(str(row["payment_provider"]))
            if row.get("payment_provider") is not None
            else None
        ),
        provider_payment_id=(
            str(row["provider_payment_id"])
            if row.get("provider_payment_id") is not None
            else None
        ),
        paid_at=_optional_datetime(row.get("paid_at")),
        fulfilled_at=_optional_datetime(row.get("fulfilled_at")),
        refund_requested_at=_optional_datetime(row.get("refund_requested_at")),
        refunded_at=_optional_datetime(row.get("refunded_at")),
        cancelled_at=_optional_datetime(row.get("cancelled_at")),
        chargeback_at=_optional_datetime(row.get("chargeback_at")),
        processed_event_keys=frozenset(event_keys),
        version=int(row["version"]),
    )


def _refund_from_row(row: Mapping[str, Any]) -> Refund:
    """@brief 从数据库行还原退款聚合 / Restore a refund aggregate from a database row.

    @param row 退款行映射 / Refund-row mapping.
    @return 领域退款 / Domain refund.
    """

    return Refund(
        refund_id=cast(UUID, row["refund_id"]),
        order_id=cast(UUID, row["order_id"]),
        requester_id=int(row["requester_id"]),
        amount=PaymentAmount(str(row["currency"]), int(row["amount_units"])),
        reason=str(row["reason"]),
        status=RefundStatus(str(row["status"])),
        requested_at=_as_utc(cast(datetime, row["requested_at"])),
        reviewer_id=_optional_int(row.get("reviewer_id")),
        reviewed_at=_optional_datetime(row.get("reviewed_at")),
        review_note=(
            str(row["review_note"]) if row.get("review_note") is not None else None
        ),
        settlement_provider=(
            PaymentProvider(str(row["settlement_provider"]))
            if row.get("settlement_provider") is not None
            else None
        ),
        provider_settlement_id=(
            str(row["provider_settlement_id"])
            if row.get("provider_settlement_id") is not None
            else None
        ),
        settled_at=_optional_datetime(row.get("settled_at")),
        cancelled_at=_optional_datetime(row.get("cancelled_at")),
        version=int(row["version"]),
    )


def _grant_from_row(row: Mapping[str, Any]) -> EntitlementGrant:
    """@brief 从数据库行还原权益授予 / Restore an entitlement grant from a database row.

    @param row 权益行映射 / Entitlement-row mapping.
    @return 领域权益授予 / Domain entitlement grant.
    """

    return EntitlementGrant(
        grant_id=cast(UUID, row["grant_id"]),
        code=str(row["code"]),
        scope=EntitlementScope(str(row["scope"])),
        subject_id=int(row["subject_id"]),
        source_order_id=cast(UUID, row["source_order_id"]),
        starts_at=_as_utc(cast(datetime, row["starts_at"])),
        expires_at=_optional_datetime(row.get("expires_at")),
        status=EntitlementStatus(str(row["status"])),
        ended_at=_optional_datetime(row.get("ended_at")),
        revocation_reason=(
            str(row["revocation_reason"])
            if row.get("revocation_reason") is not None
            else None
        ),
        version=int(row["version"]),
    )


def _subscription_from_row(row: Mapping[str, Any]) -> Subscription:
    """@brief 从数据库行还原订阅聚合 / Restore a subscription aggregate from a database row.

    @param row 订阅行映射 / Subscription-row mapping.
    @return 领域订阅 / Domain subscription.
    """

    raw_grant_ids = row.get("grant_ids", ())
    if isinstance(raw_grant_ids, (str, bytes)) or not isinstance(
        raw_grant_ids,
        Sequence,
    ):
        raise TypeError("Subscription grant IDs must be a database sequence")
    return Subscription(
        subscription_id=cast(UUID, row["subscription_id"]),
        owner_id=int(row["owner_id"]),
        product_id=cast(UUID, row["product_id"]),
        offer_id=cast(UUID, row["offer_id"]),
        source_order_id=cast(UUID, row["source_order_id"]),
        entitlement_grant_ids=tuple(cast(UUID, item) for item in raw_grant_ids),
        period_starts_at=_as_utc(cast(datetime, row["period_starts_at"])),
        period_ends_at=_as_utc(cast(datetime, row["period_ends_at"])),
        status=SubscriptionStatus(str(row["status"])),
        cancellation_requested_at=_optional_datetime(
            row.get("cancellation_requested_at")
        ),
        ended_at=_optional_datetime(row.get("ended_at")),
        revocation_reason=(
            str(row["revocation_reason"])
            if row.get("revocation_reason") is not None
            else None
        ),
        version=int(row["version"]),
    )


def _result_mapping(result: BillingResult) -> Mapping[str, object]:
    """@brief 将 Billing 结果编码为可审计的 JSON 回执 / Encode a Billing result as an auditable JSON receipt.

    @param result 待持久化的 Billing 结果 / Billing result to persist.
    @return JSON 兼容的不可变回执载荷 / JSON-compatible immutable receipt payload.
    """

    return {
        "code": result.code.value,
        "order": _order_mapping(result.order),
        "refund": _refund_mapping(result.refund),
        "entitlements": [_grant_mapping(item) for item in result.entitlements],
        "subscription": _subscription_mapping(result.subscription),
    }


def _result_from_mapping(
    payload: Mapping[str, Any],
    *,
    replayed: bool,
) -> BillingResult:
    """@brief 从不可变回执还原 Billing 结果 / Restore a Billing result from an immutable receipt.

    @param payload 回执 JSON 对象 / Receipt JSON object.
    @param replayed 是否标注为幂等重放 / Whether to mark the result as an idempotent replay.
    @return 可返回调用方的 Billing 结果 / Billing result safe to return to the caller.
    @raise ValueError 回执结构无效时抛出 / Raised when the receipt structure is invalid.
    """

    order_payload = _optional_payload_mapping(payload.get("order"), field="order")
    refund_payload = _optional_payload_mapping(payload.get("refund"), field="refund")
    subscription_payload = _optional_payload_mapping(
        payload.get("subscription"),
        field="subscription",
    )
    raw_entitlements = payload.get("entitlements", ())
    if isinstance(raw_entitlements, (str, bytes)) or not isinstance(
        raw_entitlements,
        Sequence,
    ):
        raise ValueError("Billing receipt entitlements must be an array")
    entitlements = tuple(
        _grant_from_receipt(_required_payload_mapping(item, field="entitlement"))
        for item in raw_entitlements
    )
    try:
        code = BillingCode(str(payload["code"]))
    except (KeyError, ValueError) as error:
        raise ValueError("Billing receipt has an invalid result code") from error
    return BillingResult(
        code=code,
        order=(
            _order_from_receipt(order_payload) if order_payload is not None else None
        ),
        refund=(
            _refund_from_receipt(refund_payload) if refund_payload is not None else None
        ),
        entitlements=entitlements,
        subscription=(
            _subscription_from_receipt(subscription_payload)
            if subscription_payload is not None
            else None
        ),
        replayed=replayed,
    )


def _order_mapping(order: Order | None) -> Mapping[str, object] | None:
    """@brief 将可选订单快照编码为回执对象 / Encode an optional order snapshot as a receipt object.

    @param order 可选订单快照 / Optional order snapshot.
    @return JSON 订单对象或 None / JSON order object or None.
    """

    if order is None:
        return None
    return {
        "order_id": str(order.order_id),
        "buyer_id": order.buyer_id,
        "product_id": str(order.product_id),
        "offer_id": str(order.offer_id),
        "renewal_subscription_id": _optional_uuid_mapping(
            order.renewal_subscription_id
        ),
        "product_kind": order.product_kind.value,
        "currency": order.price.currency,
        "price_units": order.price.units,
        "status": order.status.value,
        "created_at": _instant_mapping(order.created_at),
        "payment_provider": (
            order.payment_provider.value if order.payment_provider is not None else None
        ),
        "provider_payment_id": order.provider_payment_id,
        "paid_at": _optional_instant_mapping(order.paid_at),
        "fulfilled_at": _optional_instant_mapping(order.fulfilled_at),
        "refund_requested_at": _optional_instant_mapping(order.refund_requested_at),
        "refunded_at": _optional_instant_mapping(order.refunded_at),
        "cancelled_at": _optional_instant_mapping(order.cancelled_at),
        "chargeback_at": _optional_instant_mapping(order.chargeback_at),
        "event_keys": sorted(order.processed_event_keys),
        "version": order.version,
    }


def _order_from_receipt(payload: Mapping[str, Any]) -> Order:
    """@brief 从回执对象还原订单 / Restore an order from a receipt object.

    @param payload JSON 订单对象 / JSON order object.
    @return 订单领域聚合 / Order domain aggregate.
    """

    return Order(
        order_id=_uuid_from_payload(payload, "order_id"),
        buyer_id=_int_from_payload(payload, "buyer_id"),
        product_id=_uuid_from_payload(payload, "product_id"),
        offer_id=_uuid_from_payload(payload, "offer_id"),
        renewal_subscription_id=_optional_uuid(payload.get("renewal_subscription_id")),
        product_kind=ProductKind(_str_from_payload(payload, "product_kind")),
        price=PaymentAmount(
            _str_from_payload(payload, "currency"),
            _int_from_payload(payload, "price_units"),
        ),
        status=OrderStatus(_str_from_payload(payload, "status")),
        created_at=_instant_from_payload(payload, "created_at"),
        payment_provider=_optional_provider(payload.get("payment_provider")),
        provider_payment_id=_optional_string(payload.get("provider_payment_id")),
        paid_at=_optional_instant_from_payload(payload.get("paid_at")),
        fulfilled_at=_optional_instant_from_payload(payload.get("fulfilled_at")),
        refund_requested_at=_optional_instant_from_payload(
            payload.get("refund_requested_at")
        ),
        refunded_at=_optional_instant_from_payload(payload.get("refunded_at")),
        cancelled_at=_optional_instant_from_payload(payload.get("cancelled_at")),
        chargeback_at=_optional_instant_from_payload(payload.get("chargeback_at")),
        processed_event_keys=_string_set_from_payload(payload.get("event_keys", ())),
        version=_int_from_payload(payload, "version"),
    )


def _refund_mapping(refund: Refund | None) -> Mapping[str, object] | None:
    """@brief 将可选退款快照编码为回执对象 / Encode an optional refund snapshot as a receipt object.

    @param refund 可选退款快照 / Optional refund snapshot.
    @return JSON 退款对象或 None / JSON refund object or None.
    """

    if refund is None:
        return None
    return {
        "refund_id": str(refund.refund_id),
        "order_id": str(refund.order_id),
        "requester_id": refund.requester_id,
        "currency": refund.amount.currency,
        "amount_units": refund.amount.units,
        "reason": refund.reason,
        "status": refund.status.value,
        "requested_at": _instant_mapping(refund.requested_at),
        "reviewer_id": refund.reviewer_id,
        "reviewed_at": _optional_instant_mapping(refund.reviewed_at),
        "review_note": refund.review_note,
        "settlement_provider": (
            refund.settlement_provider.value
            if refund.settlement_provider is not None
            else None
        ),
        "provider_settlement_id": refund.provider_settlement_id,
        "settled_at": _optional_instant_mapping(refund.settled_at),
        "cancelled_at": _optional_instant_mapping(refund.cancelled_at),
        "version": refund.version,
    }


def _refund_from_receipt(payload: Mapping[str, Any]) -> Refund:
    """@brief 从回执对象还原退款 / Restore a refund from a receipt object.

    @param payload JSON 退款对象 / JSON refund object.
    @return 退款领域聚合 / Refund domain aggregate.
    """

    return Refund(
        refund_id=_uuid_from_payload(payload, "refund_id"),
        order_id=_uuid_from_payload(payload, "order_id"),
        requester_id=_int_from_payload(payload, "requester_id"),
        amount=PaymentAmount(
            _str_from_payload(payload, "currency"),
            _int_from_payload(payload, "amount_units"),
        ),
        reason=_str_from_payload(payload, "reason"),
        status=RefundStatus(_str_from_payload(payload, "status")),
        requested_at=_instant_from_payload(payload, "requested_at"),
        reviewer_id=_optional_int(payload.get("reviewer_id")),
        reviewed_at=_optional_instant_from_payload(payload.get("reviewed_at")),
        review_note=_optional_string(payload.get("review_note")),
        settlement_provider=_optional_provider(payload.get("settlement_provider")),
        provider_settlement_id=_optional_string(payload.get("provider_settlement_id")),
        settled_at=_optional_instant_from_payload(payload.get("settled_at")),
        cancelled_at=_optional_instant_from_payload(payload.get("cancelled_at")),
        version=_int_from_payload(payload, "version"),
    )


def _grant_mapping(grant: EntitlementGrant) -> Mapping[str, object]:
    """@brief 将权益授予编码为回执对象 / Encode an entitlement grant as a receipt object.

    @param grant 权益授予快照 / Entitlement-grant snapshot.
    @return JSON 权益对象 / JSON entitlement object.
    """

    return {
        "grant_id": str(grant.grant_id),
        "code": grant.code,
        "scope": grant.scope.value,
        "subject_id": grant.subject_id,
        "source_order_id": str(grant.source_order_id),
        "starts_at": _instant_mapping(grant.starts_at),
        "expires_at": _optional_instant_mapping(grant.expires_at),
        "status": grant.status.value,
        "ended_at": _optional_instant_mapping(grant.ended_at),
        "revocation_reason": grant.revocation_reason,
        "version": grant.version,
    }


def _grant_from_receipt(payload: Mapping[str, Any]) -> EntitlementGrant:
    """@brief 从回执对象还原权益授予 / Restore an entitlement grant from a receipt object.

    @param payload JSON 权益对象 / JSON entitlement object.
    @return 权益授予领域聚合 / Entitlement-grant domain aggregate.
    """

    return EntitlementGrant(
        grant_id=_uuid_from_payload(payload, "grant_id"),
        code=_str_from_payload(payload, "code"),
        scope=EntitlementScope(_str_from_payload(payload, "scope")),
        subject_id=_int_from_payload(payload, "subject_id"),
        source_order_id=_uuid_from_payload(payload, "source_order_id"),
        starts_at=_instant_from_payload(payload, "starts_at"),
        expires_at=_optional_instant_from_payload(payload.get("expires_at")),
        status=EntitlementStatus(_str_from_payload(payload, "status")),
        ended_at=_optional_instant_from_payload(payload.get("ended_at")),
        revocation_reason=_optional_string(payload.get("revocation_reason")),
        version=_int_from_payload(payload, "version"),
    )


def _subscription_mapping(
    subscription: Subscription | None,
) -> Mapping[str, object] | None:
    """@brief 将可选订阅快照编码为回执对象 / Encode an optional subscription snapshot as a receipt object.

    @param subscription 可选订阅快照 / Optional subscription snapshot.
    @return JSON 订阅对象或 None / JSON subscription object or None.
    """

    if subscription is None:
        return None
    return {
        "subscription_id": str(subscription.subscription_id),
        "owner_id": subscription.owner_id,
        "product_id": str(subscription.product_id),
        "offer_id": str(subscription.offer_id),
        "source_order_id": str(subscription.source_order_id),
        "entitlement_grant_ids": [
            str(grant_id) for grant_id in subscription.entitlement_grant_ids
        ],
        "period_starts_at": _instant_mapping(subscription.period_starts_at),
        "period_ends_at": _instant_mapping(subscription.period_ends_at),
        "status": subscription.status.value,
        "cancellation_requested_at": _optional_instant_mapping(
            subscription.cancellation_requested_at
        ),
        "ended_at": _optional_instant_mapping(subscription.ended_at),
        "revocation_reason": subscription.revocation_reason,
        "version": subscription.version,
    }


def _subscription_from_receipt(payload: Mapping[str, Any]) -> Subscription:
    """@brief 从回执对象还原订阅 / Restore a subscription from a receipt object.

    @param payload JSON 订阅对象 / JSON subscription object.
    @return 订阅领域聚合 / Subscription domain aggregate.
    """

    raw_grant_ids = payload.get("entitlement_grant_ids", ())
    if isinstance(raw_grant_ids, (str, bytes)) or not isinstance(
        raw_grant_ids,
        Sequence,
    ):
        raise ValueError("Billing receipt subscription grant IDs must be an array")
    return Subscription(
        subscription_id=_uuid_from_payload(payload, "subscription_id"),
        owner_id=_int_from_payload(payload, "owner_id"),
        product_id=_uuid_from_payload(payload, "product_id"),
        offer_id=_uuid_from_payload(payload, "offer_id"),
        source_order_id=_uuid_from_payload(payload, "source_order_id"),
        entitlement_grant_ids=tuple(UUID(str(item)) for item in raw_grant_ids),
        period_starts_at=_instant_from_payload(payload, "period_starts_at"),
        period_ends_at=_instant_from_payload(payload, "period_ends_at"),
        status=SubscriptionStatus(_str_from_payload(payload, "status")),
        cancellation_requested_at=_optional_instant_from_payload(
            payload.get("cancellation_requested_at")
        ),
        ended_at=_optional_instant_from_payload(payload.get("ended_at")),
        revocation_reason=_optional_string(payload.get("revocation_reason")),
        version=_int_from_payload(payload, "version"),
    )


def _json_codes(value: object) -> tuple[str, ...]:
    """@brief 将 JSONB 权益代码数组转换为领域元组 / Convert a JSONB entitlement-code array to a domain tuple.

    @param value 数据库返回的 JSONB 值 / JSONB value returned by the database.
    @return 权益代码元组 / Tuple of entitlement codes.
    @raise TypeError JSONB 不是字符串数组时抛出 / Raised when JSONB is not a string array.
    """

    decoded: object = json.loads(value) if isinstance(value, str) else value
    if isinstance(decoded, (str, bytes)) or not isinstance(decoded, Sequence):
        raise TypeError("Billing entitlement codes must be a JSON array")
    if not all(isinstance(item, str) for item in decoded):
        raise TypeError("Billing entitlement codes must be strings")
    return tuple(cast(str, item) for item in decoded)


def _subscription_period_seconds(value: timedelta | None) -> int | None:
    """@brief 将订阅周期无损编码为 PostgreSQL 秒数 / Encode a subscription period losslessly as PostgreSQL seconds.

    @param value 可选订阅周期 / Optional subscription period.
    @return 整秒周期或 None / Integral-second period or None.
    @raise ValueError 周期包含微秒、无法无损写入目录时抛出 /
        Raised when a period has microseconds and cannot be losslessly written to the catalog.
    """

    if value is None:
        return None
    if value.microseconds != 0:
        raise ValueError(
            "Billing subscription periods must have whole-second precision"
        )
    seconds = value.days * 86_400 + value.seconds
    if seconds <= 0:
        raise ValueError("Billing subscription periods must be positive")
    return seconds


def _offer_status(value: object) -> OfferStatus:
    """@brief 将数据库报价状态转换为领域枚举 / Convert a database offer status to a domain enum.

    @param value 数据库报价状态 / Database offer status.
    @return 报价状态枚举 / Offer-status enum.
    """

    return OfferStatus(str(value))


def _as_utc(value: datetime) -> datetime:
    """@brief 断言数据库时刻带时区并转换为 UTC / Assert a database instant is timezone-aware and convert it to UTC.

    @param value 数据库时刻 / Database instant.
    @return UTC 时刻 / UTC instant.
    @raise ValueError 数据库返回无时区时刻时抛出 / Raised when the database returns a naive instant.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Billing database timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _optional_datetime(value: object) -> datetime | None:
    """@brief 转换数据库可选时刻 / Convert an optional database instant.

    @param value 可选数据库时刻 / Optional database instant.
    @return UTC 时刻或 None / UTC instant or None.
    @raise TypeError 值不是 datetime 或 None 时抛出 / Raised when the value is neither datetime nor None.
    """

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError("Billing timestamp must be a datetime or None")
    return _as_utc(value)


def _optional_int(value: object) -> int | None:
    """@brief 转换可选严格整数 / Convert an optional strict integer.

    @param value 可选数据库整数 / Optional database integer.
    @return 整数或 None / Integer or None.
    @raise TypeError 值不是整数或 None 时抛出 / Raised when the value is neither an integer nor None.
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Billing integer must be an integer or None")
    return value


def _optional_uuid(value: object) -> UUID | None:
    """@brief 转换可选 UUID / Convert an optional UUID.

    @param value 可选数据库或回执 UUID / Optional database or receipt UUID.
    @return UUID 或 None / UUID or None.
    @raise ValueError UUID 文本无效时抛出 / Raised when UUID text is invalid.
    """

    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _uuid_value(value: object, *, field: str) -> UUID:
    """@brief 将必填数据库 UUID 转换为领域 UUID / Convert a required database UUID into a domain UUID.

    @param value 数据库或回执中的 UUID 值 / UUID value from a database row or receipt.
    @param field 出错信息中的字段名 / Field name used in error messages.
    @return 已解析 UUID / Parsed UUID.
    @raise ValueError UUID 缺失或文本非法时抛出 / Raised when the UUID is missing or malformed.
    """

    parsed = _optional_uuid(value)
    if parsed is None:
        raise ValueError(f"{field} must be a UUID")
    return parsed


def _optional_provider(value: object) -> PaymentProvider | None:
    """@brief 转换可选支付渠道 / Convert an optional payment provider.

    @param value 可选渠道文本 / Optional provider text.
    @return 支付渠道枚举或 None / Payment-provider enum or None.
    """

    if value is None:
        return None
    return PaymentProvider(str(value))


def _optional_string(value: object) -> str | None:
    """@brief 转换可选字符串 / Convert an optional string.

    @param value 可选字符串 / Optional string value.
    @return 字符串或 None / String or None.
    @raise TypeError 值不是字符串或 None 时抛出 / Raised when the value is neither string nor None.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Billing optional text must be a string or None")
    return value


def _optional_uuid_mapping(value: UUID | None) -> str | None:
    """@brief 将可选 UUID 编码为 JSON 文本 / Encode an optional UUID as JSON text.

    @param value 可选 UUID / Optional UUID.
    @return UUID 文本或 None / UUID text or None.
    """

    return str(value) if value is not None else None


def _instant_mapping(value: datetime) -> str:
    """@brief 将时刻编码为 UTC ISO-8601 文本 / Encode an instant as UTC ISO-8601 text.

    @param value 待编码时刻 / Instant to encode.
    @return UTC ISO-8601 文本 / UTC ISO-8601 text.
    """

    return _as_utc(value).isoformat()


def _optional_instant_mapping(value: datetime | None) -> str | None:
    """@brief 将可选时刻编码为 JSON 文本 / Encode an optional instant as JSON text.

    @param value 可选时刻 / Optional instant.
    @return UTC ISO-8601 文本或 None / UTC ISO-8601 text or None.
    """

    return _instant_mapping(value) if value is not None else None


def _instant_from_payload(payload: Mapping[str, Any], field: str) -> datetime:
    """@brief 从回执对象读取必填时刻 / Read a required instant from a receipt object.

    @param payload 回执对象 / Receipt object.
    @param field 时刻字段名 / Instant field name.
    @return UTC 时刻 / UTC instant.
    @raise ValueError 字段缺失或不是有效时刻文本时抛出 / Raised when the field is absent or invalid.
    """

    value = _str_from_payload(payload, field)
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError as error:
        raise ValueError(f"Billing receipt field {field} is not an instant") from error


def _optional_instant_from_payload(value: object) -> datetime | None:
    """@brief 从回执值读取可选时刻 / Read an optional instant from a receipt value.

    @param value 可选 ISO-8601 时刻文本 / Optional ISO-8601 instant text.
    @return UTC 时刻或 None / UTC instant or None.
    @raise ValueError 时刻文本无效时抛出 / Raised when the instant text is invalid.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Billing receipt instant must be text or null")
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError as error:
        raise ValueError(
            "Billing receipt contains an invalid optional instant"
        ) from error


def _required_payload_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    """@brief 验证回执中的必填对象 / Validate a required object in a receipt.

    @param value 原始回执值 / Raw receipt value.
    @param field 出错信息字段名 / Field name for error messages.
    @return 受窄后的对象映射 / Narrowed object mapping.
    @raise ValueError 值不是对象时抛出 / Raised when the value is not an object.
    """

    if not isinstance(value, Mapping):
        raise ValueError(f"Billing receipt {field} must be an object")
    return cast(Mapping[str, Any], value)


def _optional_payload_mapping(
    value: object,
    *,
    field: str,
) -> Mapping[str, Any] | None:
    """@brief 验证回执中的可选对象 / Validate an optional object in a receipt.

    @param value 原始回执值 / Raw receipt value.
    @param field 出错信息字段名 / Field name for error messages.
    @return 对象映射或 None / Object mapping or None.
    """

    if value is None:
        return None
    return _required_payload_mapping(value, field=field)


def _str_from_payload(payload: Mapping[str, Any], field: str) -> str:
    """@brief 从回执对象读取必填字符串 / Read a required string from a receipt object.

    @param payload 回执对象 / Receipt object.
    @param field 字段名 / Field name.
    @return 字符串字段值 / String field value.
    @raise ValueError 字段缺失或类型非法时抛出 / Raised when field is absent or has an invalid type.
    """

    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"Billing receipt field {field} must be text")
    return value


def _int_from_payload(payload: Mapping[str, Any], field: str) -> int:
    """@brief 从回执对象读取必填严格整数 / Read a required strict integer from a receipt object.

    @param payload 回执对象 / Receipt object.
    @param field 字段名 / Field name.
    @return 整数字段值 / Integer field value.
    @raise ValueError 字段缺失或类型非法时抛出 / Raised when field is absent or has an invalid type.
    """

    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Billing receipt field {field} must be an integer")
    return value


def _uuid_from_payload(payload: Mapping[str, Any], field: str) -> UUID:
    """@brief 从回执对象读取必填 UUID / Read a required UUID from a receipt object.

    @param payload 回执对象 / Receipt object.
    @param field 字段名 / Field name.
    @return UUID 字段值 / UUID field value.
    @raise ValueError 字段缺失或 UUID 非法时抛出 / Raised when field is absent or UUID is invalid.
    """

    try:
        return UUID(_str_from_payload(payload, field))
    except ValueError as error:
        raise ValueError(f"Billing receipt field {field} must be a UUID") from error


def _string_set_from_payload(value: object) -> frozenset[str]:
    """@brief 从回执对象读取字符串集合 / Read a string set from a receipt object.

    @param value 原始回执数组 / Raw receipt array.
    @return 不可变字符串集合 / Immutable string set.
    @raise ValueError 值不是字符串数组时抛出 / Raised when the value is not a string array.
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("Billing receipt event keys must be an array")
    if not all(isinstance(item, str) for item in value):
        raise ValueError("Billing receipt event keys must be text")
    return frozenset(cast(str, item) for item in value)


__all__ = ["PostgresBillingCatalog", "PostgresBillingOperations"]
"""@brief Billing PostgreSQL 公开适配器 / Public Billing PostgreSQL adapters."""
