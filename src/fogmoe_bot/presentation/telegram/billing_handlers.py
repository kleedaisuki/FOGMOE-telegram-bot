"""@brief Durable Telegram Billing 与权益管理命令 / Durable Telegram billing and entitlement-management commands."""

from __future__ import annotations

from uuid import UUID, uuid4

from fogmoe_bot.application.billing.models import (
    BillingCode,
    BillingResult,
    CancelSubscription,
    FulfillOrder,
    PlaceOrder,
    RefundReviewDecision,
    RequestRefund,
    ReviewRefund,
)
from fogmoe_bot.application.billing.service import BillingService
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
)
from fogmoe_bot.domain.billing.entitlements import EntitlementGrant
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import enqueue_command_reply


_PRIVATE_ONLY_TEXT = "账单、订阅和退款命令仅限私聊使用喵。"
"""@brief Billing 私聊边界提示 / Billing private-chat boundary prompt."""


class BillingTelegramCommandHandler:
    """@brief 将 Billing 管理命令映射到 typed 服务和 durable 回包 / Map billing-management commands to typed services and durable replies.

    @note 此 handler 只创建订单、展示权益、发起退款和执行受控后台状态变迁；它不接受
        用户自报的付款成功，更不会把支付金额兑换为金币。/ This handler creates orders,
        displays entitlements, initiates refunds, and performs controlled back-office state
        transitions; it never accepts a user-asserted successful payment or converts payment
        amounts into tokens.
    """

    def __init__(
        self,
        *,
        billing: BillingService,
        outbound: StandaloneOutboundCapability,
    ) -> None:
        """@brief 注入 Billing 服务和 durable 回包能力 / Inject the billing service and durable reply capability.

        @param billing Billing 应用服务 / Billing application service.
        @param outbound standalone outbox 能力 / Standalone-outbox capability.
        """

        self._billing = billing
        """@brief Billing 应用服务 / Billing application service."""
        self._outbound = outbound
        """@brief durable standalone outbox 能力 / Durable standalone-outbox capability."""

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回 Billing 命令所有权 / Return billing command ownership.

        @return billing/billing_order/refund/subscription_cancel/billing_fulfill/billing_refund_review /
            Billing command set.
        """

        return frozenset(
            {
                "billing",
                "billing_order",
                "refund",
                "subscription_cancel",
                "billing_fulfill",
                "billing_refund_review",
            }
        )

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行 Billing 命令并写入确定性回包 / Execute a billing command and enqueue a deterministic reply.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析命令 envelope / Parsed command envelope.
        @return None / None.
        """

        if command.chat_type != "private":
            text = _PRIVATE_ONLY_TEXT
        elif command.command == "billing":
            text = await self._overview_text(update, command)
        elif command.command == "billing_order":
            text = await self._order_text(update, command)
        elif command.command == "refund":
            text = await self._refund_text(update, command)
        elif command.command == "subscription_cancel":
            text = await self._cancel_text(update, command)
        elif command.command == "billing_fulfill":
            text = await self._fulfill_text(update, command)
        elif command.command == "billing_refund_review":
            text = await self._review_text(update, command)
        else:
            raise ValueError("Billing handler received an unowned command")
        await enqueue_command_reply(self._outbound, update, command, text)

    async def _overview_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 加载当前有效权益 / Load currently effective entitlements.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析 `/billing` 命令 / Parsed `/billing` command.
        @return 用户可见账单概览 / User-visible billing overview.
        """

        if command.argument_text:
            return _usage_text()
        entitlements = await self._billing.active_user_entitlements(
            command.user_id,
            observed_at=update.received_at,
        )
        return _entitlements_text(entitlements)

    async def _order_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 创建一个等待受控渠道付款的订单 / Create an order awaiting a controlled provider payment.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析下单命令 / Parsed order command.
        @return 用户可见订单结果 / User-visible order result.
        """

        parsed = _order_arguments(command.argument_text)
        if isinstance(parsed, str):
            return parsed
        offer_id, renewal_subscription_id = parsed
        result = await self._billing.place_order(
            PlaceOrder(
                buyer_id=command.user_id,
                offer_id=offer_id,
                order_id=uuid4(),
                created_at=update.received_at,
                idempotency_key=_key(update, command, "order"),
                renewal_subscription_id=renewal_subscription_id,
            )
        )
        return _result_text(result, action="order")

    async def _refund_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 发起一笔订单退款申请 / Start one order refund request.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析退款命令 / Parsed refund command.
        @return 用户可见退款结果 / User-visible refund result.
        """

        parsed = _refund_arguments(command.argument_text)
        if isinstance(parsed, str):
            return parsed
        order_id, reason = parsed
        result = await self._billing.request_refund(
            RequestRefund(
                requester_id=command.user_id,
                order_id=order_id,
                refund_id=uuid4(),
                reason=reason,
                requested_at=update.received_at,
                idempotency_key=_key(update, command, "refund"),
            )
        )
        return _result_text(result, action="refund")

    async def _cancel_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 请求在当前订阅周期结束时取消 / Request cancellation at the current subscription-period end.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析取消命令 / Parsed cancellation command.
        @return 用户可见取消结果 / User-visible cancellation result.
        """

        subscription_id = _one_uuid_argument(
            command.argument_text,
            usage="用法：/subscription_cancel <订阅ID>",
        )
        if isinstance(subscription_id, str):
            return subscription_id
        result = await self._billing.cancel_subscription(
            CancelSubscription(
                owner_id=command.user_id,
                subscription_id=subscription_id,
                requested_at=update.received_at,
                idempotency_key=_key(update, command, "subscription-cancel"),
            )
        )
        return _result_text(result, action="subscription_cancel")

    async def _fulfill_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 由后台管理员履约一笔已验证付款订单 / Fulfill one verified-payment order as back office.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析后台履约命令 / Parsed back-office fulfillment command.
        @return 用户可见履约结果 / User-visible fulfillment result.
        """

        order_id = _one_uuid_argument(
            command.argument_text,
            usage="用法：/billing_fulfill <订单ID>",
        )
        if isinstance(order_id, str):
            return order_id
        result = await self._billing.fulfill_order(
            FulfillOrder(
                order_id=order_id,
                operator_id=command.user_id,
                fulfilled_at=update.received_at,
                idempotency_key=_key(update, command, "fulfill"),
            )
        )
        return _result_text(result, action="fulfill")

    async def _review_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 由后台管理员审核一笔退款 / Review one refund as back office.

        @param update durable 来源 Update / Durable source update.
        @param command 已解析后台退款审核命令 / Parsed back-office refund-review command.
        @return 用户可见审核结果 / User-visible review result.
        """

        parsed = _review_arguments(command.argument_text)
        if isinstance(parsed, str):
            return parsed
        refund_id, decision, note = parsed
        result = await self._billing.review_refund(
            ReviewRefund(
                refund_id=refund_id,
                reviewer_id=command.user_id,
                decision=decision,
                reviewed_at=update.received_at,
                idempotency_key=_key(update, command, "refund-review"),
                note=note,
            )
        )
        return _result_text(result, action="refund_review")


def _order_arguments(argument_text: str) -> tuple[UUID, UUID | None] | str:
    """@brief 解析 `/billing_order <报价ID> [续费订阅ID]` / Parse `/billing_order <offer-id> [renewal-subscription-id]`.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 报价和可选续费订阅，或用法错误 / Offer and optional renewal subscription, or a usage error.
    """

    parts = argument_text.split()
    if len(parts) not in {1, 2}:
        return "用法：/billing_order <报价ID> [续费订阅ID]"
    offer_id = _parse_uuid(parts[0])
    if offer_id is None:
        return "报价 ID 必须是有效 UUID。"
    renewal_subscription_id = _parse_uuid(parts[1]) if len(parts) == 2 else None
    if len(parts) == 2 and renewal_subscription_id is None:
        return "续费订阅 ID 必须是有效 UUID。"
    return offer_id, renewal_subscription_id


def _refund_arguments(argument_text: str) -> tuple[UUID, str] | str:
    """@brief 解析 `/refund <订单ID> <原因>` / Parse `/refund <order-id> <reason>`.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 订单和退款原因，或用法错误 / Order and refund reason, or a usage error.
    """

    parts = argument_text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return "用法：/refund <订单ID> <退款原因>"
    order_id = _parse_uuid(parts[0])
    if order_id is None:
        return "订单 ID 必须是有效 UUID。"
    if len(parts[1].strip()) > 1_000:
        return "退款原因最多 1000 个字符。"
    return order_id, parts[1].strip()


def _review_arguments(
    argument_text: str,
) -> tuple[UUID, RefundReviewDecision, str | None] | str:
    """@brief 解析 `/billing_refund_review <退款ID> approve|reject [说明]` / Parse a billing refund-review command.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 退款、决定和可选说明，或用法错误 / Refund, decision and optional note, or a usage error.
    """

    parts = argument_text.split(maxsplit=2)
    if len(parts) < 2:
        return "用法：/billing_refund_review <退款ID> approve|reject [说明]"
    refund_id = _parse_uuid(parts[0])
    if refund_id is None:
        return "退款 ID 必须是有效 UUID。"
    try:
        decision = RefundReviewDecision(parts[1].casefold())
    except ValueError:
        return "审核决定只能是 approve 或 reject。"
    note = parts[2].strip() if len(parts) == 3 else None
    if note is not None and len(note) > 1_000:
        return "审核说明最多 1000 个字符。"
    return refund_id, decision, note or None


def _one_uuid_argument(argument_text: str, *, usage: str) -> UUID | str:
    """@brief 解析只有一个 UUID 的命令参数 / Parse a command argument consisting of exactly one UUID.

    @param argument_text 原始参数文本 / Raw argument text.
    @param usage 失败时展示的用法 / Usage shown on failure.
    @return UUID，或用户可见用法错误 / UUID, or a user-visible usage error.
    """

    parts = argument_text.split()
    if len(parts) != 1:
        return usage
    value = _parse_uuid(parts[0])
    return value if value is not None else "ID 必须是有效 UUID。"


def _parse_uuid(raw_value: str) -> UUID | None:
    """@brief 解析 UUID 文本 / Parse UUID text.

    @param raw_value 原始 UUID 文本 / Raw UUID text.
    @return UUID，非法时为 None / UUID, or None when invalid.
    """

    try:
        return UUID(raw_value)
    except ValueError:
        return None


def _key(
    update: InboundUpdate,
    command: ParsedTelegramCommand,
    action: str,
) -> str:
    """@brief 从 durable Update 构造 Billing 幂等键 / Construct a billing idempotency key from a durable update.

    @param update durable 来源 Update / Durable source update.
    @param command 已解析命令 envelope / Parsed command envelope.
    @param action 稳定业务动作 / Stable business action.
    @return Billing 业务幂等键 / Billing business idempotency key.
    """

    return f"telegram:billing:{action}:{int(update.update_id)}:{command.user_id}"


def _entitlements_text(entitlements: tuple[EntitlementGrant, ...]) -> str:
    """@brief 渲染当前有效权益 / Render currently effective entitlements.

    @param entitlements 有效权益快照 / Effective entitlement snapshots.
    @return 用户可见账单概览 / User-visible billing overview.
    """

    lines = [
        "🧾 Billing 与权益",
        "支付金额只产生产品权益，不兑换为金币。",
    ]
    if not entitlements:
        lines.append("当前没有有效权益。")
    else:
        lines.append("当前有效权益：")
        for grant in entitlements:
            expiry = (
                grant.expires_at.isoformat() if grant.expires_at is not None else "永久"
            )
            lines.append(f"- {grant.code}（至 {expiry}）")
    lines.append("下单：/billing_order <报价ID> [续费订阅ID]")
    lines.append("退款：/refund <订单ID> <原因>")
    lines.append("取消订阅：/subscription_cancel <订阅ID>")
    return "\n".join(lines)


def _result_text(result: BillingResult, *, action: str) -> str:
    """@brief 渲染 Billing 写操作结果 / Render a billing write-operation result.

    @param result typed Billing 结果 / Typed billing result.
    @param action 稳定动作名称 / Stable action name.
    @return 用户可见文本 / User-visible text.
    """

    if result.code is BillingCode.NOT_REGISTERED:
        return "请先使用 /me 注册账户，再管理订单或权益。"
    if result.code is BillingCode.NOT_FOUND:
        return "未找到对应的报价、订单、退款或订阅。"
    if result.code is BillingCode.FORBIDDEN:
        return "你没有执行这项账单操作的权限。"
    if result.code is BillingCode.OFFER_UNAVAILABLE:
        return "该报价当前不可售。"
    if result.code is BillingCode.PAYMENT_UNVERIFIED:
        return "支付事件未通过渠道验证，因此没有改变订单或权益。"
    if result.code is BillingCode.INVALID_PAYMENT_EVENT:
        return "该支付事件不能用于当前账单操作。"
    if result.code is BillingCode.INVALID_STATE:
        return "订单、退款或订阅当前状态不能执行该操作。"
    if result.code is BillingCode.CONFLICT:
        return "请求与已有账单回执冲突；请不要复用同一操作键表达不同请求。"
    prefix = "已回放同一请求的结果。\n" if result.replayed else ""
    if action == "order" and result.order is not None:
        return (
            f"{prefix}订单已创建：{result.order.order_id}\n"
            f"状态：{result.order.status.value}\n"
            f"冻结支付金额：{result.order.price.units} {result.order.price.currency}\n"
            "等待受控支付渠道的已验证事件；支付金额不会兑换为金币。"
        )
    if action == "refund" and result.refund is not None:
        return (
            f"{prefix}退款申请已记录：{result.refund.refund_id}\n"
            f"状态：{result.refund.status.value}\n"
            "后续审核和渠道结算会以独立、可审计状态变迁完成。"
        )
    if action == "subscription_cancel" and result.subscription is not None:
        return (
            f"{prefix}订阅将在本期结束时取消：{result.subscription.subscription_id}\n"
            f"周期结束：{result.subscription.period_ends_at.isoformat()}"
        )
    if action == "fulfill" and result.order is not None:
        return (
            f"{prefix}订单已履约：{result.order.order_id}\n"
            f"已授予权益：{', '.join(grant.code for grant in result.entitlements) or '无'}"
        )
    if action == "refund_review" and result.refund is not None:
        return f"{prefix}退款审核已记录：{result.refund.refund_id}（{result.refund.status.value}）。"
    return f"{prefix}账单操作已完成。"


def _usage_text() -> str:
    """@brief 返回 Billing 固定用法 / Return fixed billing usage.

    @return 用户可见用法 / User-visible usage.
    """

    return (
        "Billing 用法：\n"
        "/billing\n"
        "/billing_order <报价ID> [续费订阅ID]\n"
        "/refund <订单ID> <原因>\n"
        "/subscription_cancel <订阅ID>"
    )


__all__ = ["BillingTelegramCommandHandler"]
