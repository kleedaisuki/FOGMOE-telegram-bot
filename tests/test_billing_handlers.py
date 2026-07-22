"""@brief Durable Telegram Billing 命令测试 / Tests for durable Telegram Billing commands."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from fogmoe_bot.application.billing.models import BillingCode, BillingResult, PlaceOrder
from fogmoe_bot.application.billing.service import BillingService
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.billing.entitlements import EntitlementGrant
from fogmoe_bot.domain.conversation.identity import ConversationId, UpdateId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.billing_handlers import (
    BillingTelegramCommandHandler,
)
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)

NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 测试固定接收时刻 / Fixed receipt instant for tests."""


class _Billing:
    """@brief 记录 Telegram Billing 调用的最小服务替身 / Minimal service double recording Telegram Billing calls."""

    def __init__(self) -> None:
        """@brief 初始化订单调用记录 / Initialize order-call recordings.

        @return None / None.
        """

        self.orders: list[PlaceOrder] = []
        """@brief 已收到的下单命令 / Received order-placement commands."""

    async def place_order(self, command: PlaceOrder) -> BillingResult:
        """@brief 记录下单并模拟不存在报价 / Record an order and simulate an absent offer.

        @param command 下单命令 / Order-placement command.
        @return 可预测的报价不存在结果 / Predictable offer-not-found result.
        """

        self.orders.append(command)
        return BillingResult(BillingCode.NOT_FOUND)

    async def active_user_entitlements(
        self,
        user_id: int,
        *,
        observed_at: datetime,
    ) -> tuple[EntitlementGrant, ...]:
        """@brief 返回空权益集合 / Return an empty entitlement collection.

        @param user_id 读取的用户标识 / User identity being read.
        @param observed_at 权益观察时刻 / Entitlement observation instant.
        @return 空权益元组 / Empty entitlement tuple.
        """

        del user_id, observed_at
        return ()


class _Outbound:
    """@brief 记录 durable 回包的 outbox 替身 / Outbox double recording durable replies."""

    def __init__(self) -> None:
        """@brief 初始化空回包记录 / Initialize empty reply recordings.

        @return None / None.
        """

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief 已入队的回包 / Enqueued replies."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录一条 outbox 命令 / Record one outbox command.

        @param command 待投递 outbox 命令 / Outbox command to deliver.
        @return None / None.
        """

        self.commands.append(command)


def _update(update_id: int) -> InboundUpdate:
    """@brief 构造 durable 来源 Update / Build a durable source update.

    @param update_id Update 标识 / Update identifier.
    @return 待处理 durable Update / Pending durable update.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=ConversationId("assistant-user:42"),
        payload={"update_id": update_id},
        received_at=NOW,
    )


def _command(
    name: str,
    argument_text: str = "",
    *,
    chat_type: str = "private",
) -> ParsedTelegramCommand:
    """@brief 构造已解析 Billing 命令 / Build a parsed Billing command.

    @param name 无 slash 的命令名 / Command name without slash.
    @param argument_text 原始参数文本 / Raw argument text.
    @param chat_type Telegram chat 类型 / Telegram chat type.
    @return 已解析命令 envelope / Parsed command envelope.
    """

    return ParsedTelegramCommand(
        command=name,
        target=None,
        user_id=42,
        chat_id=42 if chat_type == "private" else -100_42,
        message_id=9,
        message_thread_id=None,
        username="klee",
        argument_text=argument_text,
        arguments=tuple(argument_text.split()),
        chat_type=chat_type,
    )


def test_billing_order_freezes_only_an_offer_and_never_accepts_a_token_amount() -> None:
    """@brief `/billing_order` 只提交报价 ID，不能把支付或金币数送入服务 / `/billing_order` submits only an offer ID and cannot pass a payment or token amount.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行私聊下单场景 / Execute the private order-placement scenario.

        @return None / None.
        """

        billing = _Billing()
        outbound = _Outbound()
        handler = BillingTelegramCommandHandler(
            billing=cast(BillingService, billing),
            outbound=outbound,
        )
        offer_id = uuid4()

        await handler.handle(_update(91), _command("billing_order", str(offer_id)))

        assert len(billing.orders) == 1
        command = billing.orders[0]
        assert command.buyer_id == 42
        assert command.offer_id == offer_id
        assert isinstance(command.order_id, UUID)
        assert command.created_at == NOW
        assert command.idempotency_key == "telegram:billing:order:91:42"
        assert not hasattr(command, "price_units")
        assert "未找到对应的报价" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())


def test_billing_commands_in_group_never_call_the_billing_service() -> None:
    """@brief 群聊 Billing 命令不会触达账单服务 / A group Billing command never reaches the billing service.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行群聊边界场景 / Execute the group-boundary scenario.

        @return None / None.
        """

        billing = _Billing()
        outbound = _Outbound()
        handler = BillingTelegramCommandHandler(
            billing=cast(BillingService, billing),
            outbound=outbound,
        )

        await handler.handle(
            _update(92),
            _command("billing_order", str(uuid4()), chat_type="supergroup"),
        )

        assert billing.orders == []
        assert "仅限私聊" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())
