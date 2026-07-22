"""@brief Durable Telegram 银行命令测试 / Durable Telegram bank-command tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fogmoe_bot.application.banking.models import (
    ActivityPotFundingResult,
    BankCode,
    BankOverview,
    FundActivityPot,
    IssueTokens,
    ListPendingTokenRequests,
    PendingTokenRequestsResult,
    RequestTokens,
    ReviewTokenRequest,
    TokenRequestResult,
)
from fogmoe_bot.application.banking.service import BankService
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.banking.money import TokenAmount, TokenBucket, WalletBalance
from fogmoe_bot.domain.banking.requests import TokenRequest
from fogmoe_bot.domain.conversation.identity import ConversationId, UpdateId
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram.bank_handlers import BankTelegramCommandHandler
from fogmoe_bot.presentation.telegram.command_cooldown_guard import (
    ParsedTelegramCommand,
)


NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 固定测试时刻 / Fixed test instant."""


class _Operations:
    """@brief 记录银行命令的内存端口 / In-memory port recording bank commands."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call recordings."""

        self.requests: list[RequestTokens] = []
        """@brief 已收到的申请 / Received requests."""
        self.reviews: list[ReviewTokenRequest] = []
        """@brief 已收到的审核 / Received reviews."""
        self.issues: list[IssueTokens] = []
        """@brief 已收到的发行 / Received issuances."""
        self.activity_pot_fundings: list[FundActivityPot] = []
        """@brief 已收到的活动奖池注资 / Received activity-pot fundings."""
        self.pending_queries: list[ListPendingTokenRequests] = []
        """@brief 已收到的待审批查询 / Received pending-request queries."""
        self.pending_requests: tuple[TokenRequest, ...] = ()
        """@brief 测试注入的待审批请求 / Test-injected pending requests."""

    async def request_tokens(self, command: RequestTokens) -> TokenRequestResult:
        """@brief 记录并接受申请 / Record and accept a request.

        @param command 申请命令 / Request command.
        @return 成功结果 / Successful result.
        """

        self.requests.append(command)
        return TokenRequestResult(BankCode.SUCCESS, request=command.aggregate())

    async def review_token_request(
        self,
        command: ReviewTokenRequest,
    ) -> TokenRequestResult:
        """@brief 记录审核 / Record a review.

        @param command 审核命令 / Review command.
        @return 成功结果 / Successful result.
        """

        self.reviews.append(command)
        return TokenRequestResult(BankCode.SUCCESS)

    async def issue_tokens(self, command: IssueTokens) -> TokenRequestResult:
        """@brief 记录发行 / Record an issuance.

        @param command 发行命令 / Issuance command.
        @return 成功结果 / Successful result.
        """

        self.issues.append(command)
        return TokenRequestResult(
            BankCode.SUCCESS,
            overview=BankOverview(
                command.recipient_id,
                WalletBalance(TokenBucket.FREE, command.amount.value),
                WalletBalance(TokenBucket.PAID, 0),
            ),
        )

    async def fund_activity_pot(
        self,
        command: FundActivityPot,
    ) -> ActivityPotFundingResult:
        """@brief 记录活动奖池注资 / Record activity-pot funding.

        @param command 管理员注资命令 / Administrator-funding command.
        @return 带可审计余额的成功结果 / Success result carrying an auditable balance.
        """

        self.activity_pot_fundings.append(command)
        return ActivityPotFundingResult(
            BankCode.SUCCESS,
            amount=command.amount,
            activity_pot_balance=40 + command.amount.value,
            ledger_entry_id=UUID("11111111-1111-4111-8111-111111111111"),
        )

    async def list_pending_token_requests(
        self,
        command: ListPendingTokenRequests,
    ) -> PendingTokenRequestsResult:
        """@brief 记录待审批查询并返回注入结果 / Record a pending query and return injected results.

        @param command 管理员查询命令 / Administrator query command.
        @return 成功列表结果 / Successful list result.
        """

        self.pending_queries.append(command)
        return PendingTokenRequestsResult(BankCode.SUCCESS, self.pending_requests)

    async def overview(self, user_id: int) -> BankOverview | None:
        """@brief 返回固定钱包概览 / Return a fixed wallet overview.

        @param user_id 用户标识 / User identity.
        @return 双钱包概览 / Two-pocket wallet overview.
        """

        return BankOverview(
            user_id,
            WalletBalance(TokenBucket.FREE, 9),
            WalletBalance(TokenBucket.PAID, 3),
        )


class _Outbound:
    """@brief 记录 durable 回包 / Record durable command replies."""

    def __init__(self) -> None:
        """@brief 初始化空输出 / Initialize empty output."""

        self.commands: list[StandaloneOutboundCommand] = []
        """@brief 已入队回包 / Enqueued replies."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 记录一个回包 / Record one reply.

        @param command outbox 命令 / Outbox command.
        @return None / None.
        """

        self.commands.append(command)


def _update(update_id: int) -> InboundUpdate:
    """@brief 构造 durable Update / Build a durable update.

    @param update_id Update 标识 / Update identity.
    @return 待处理 Update / Pending update.
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
    user_id: int = 42,
    chat_type: str = "private",
) -> ParsedTelegramCommand:
    """@brief 构造 parsed bank command / Build a parsed bank command.

    @param name 命令名 / Command name.
    @param argument_text 原始参数 / Raw arguments.
    @param user_id 调用用户 / Calling user.
    @param chat_type Telegram chat 类型 / Telegram chat type.
    @return parsed command envelope / Parsed command envelope.
    """

    return ParsedTelegramCommand(
        command=name,
        target=None,
        user_id=user_id,
        chat_id=user_id if chat_type == "private" else -100_42,
        message_id=9,
        message_thread_id=None,
        username="klee",
        argument_text=argument_text,
        arguments=tuple(argument_text.split()),
        chat_type=chat_type,
    )


def test_recharge_now_creates_a_token_request_in_private_chat() -> None:
    """@brief `/recharge` 变为代币申请，而非人工充值 / `/recharge` now creates a token request rather than a manual top-up."""

    async def scenario() -> None:
        """@brief 执行申请场景 / Execute the request scenario.

        @return None / None.
        """

        operations = _Operations()
        outbound = _Outbound()
        handler = BankTelegramCommandHandler(
            bank=BankService(operations, administrator_id=1),
            outbound=outbound,
        )

        await handler.handle(_update(20), _command("recharge", "12 修复个人灯塔"))

        assert len(operations.requests) == 1
        request = operations.requests[0]
        assert request.amount == TokenAmount(12)
        assert request.purpose == "修复个人灯塔"
        assert (
            outbound.commands[0].idempotency_key
            == "update:20:command:recharge:response"
        )
        assert "申请 ID" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())


def test_bank_commands_reject_group_context_without_touching_money() -> None:
    """@brief 群聊中的银行命令不会触达银行服务 / A group bank command never reaches the bank service."""

    async def scenario() -> None:
        """@brief 执行群聊边界场景 / Execute the group-boundary scenario.

        @return None / None.
        """

        operations = _Operations()
        outbound = _Outbound()
        handler = BankTelegramCommandHandler(
            bank=BankService(operations, administrator_id=1),
            outbound=outbound,
        )

        await handler.handle(
            _update(21),
            _command("request_tokens", "5 群聊中不能申请", chat_type="supergroup"),
        )

        assert operations.requests == []
        assert "仅限私聊" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())


def test_bank_admin_issue_is_gated_before_the_persistence_port() -> None:
    """@brief 非管理员不能触达直接发行端口 / A non-admin cannot reach the direct-issuance port."""

    async def scenario() -> None:
        """@brief 执行权限场景 / Execute the authorization scenario.

        @return None / None.
        """

        operations = _Operations()
        outbound = _Outbound()
        handler = BankTelegramCommandHandler(
            bank=BankService(operations, administrator_id=1),
            outbound=outbound,
        )

        await handler.handle(_update(22), _command("bank_issue", "99 8 新手补偿"))

        assert operations.issues == []
        assert "只有银行管理员" in str(outbound.commands[0].payload["text"])

    asyncio.run(scenario())


def test_bank_activity_pot_funding_is_private_admin_only_and_auditable() -> None:
    """@brief `/bank_fund_activity` 仅管理员可发起且回包含账本事实 /
    `/bank_fund_activity` is admin-only and returns ledger facts.
    """

    async def scenario() -> None:
        """@brief 执行拒绝和成功注资场景 / Exercise rejected and successful funding paths."""

        operations = _Operations()
        outbound = _Outbound()
        handler = BankTelegramCommandHandler(
            bank=BankService(operations, administrator_id=1),
            outbound=outbound,
        )

        await handler.handle(
            _update(24),
            _command("bank_fund_activity", "12 Chance payout reserve", user_id=99),
        )
        assert operations.activity_pot_fundings == []
        assert "只有银行管理员" in str(outbound.commands[-1].payload["text"])

        await handler.handle(
            _update(25),
            _command("bank_fund_activity", "12 Chance payout reserve", user_id=1),
        )
        assert len(operations.activity_pot_fundings) == 1
        funding = operations.activity_pot_fundings[0]
        assert funding.amount == TokenAmount(12)
        assert funding.purpose == "Chance payout reserve"
        assert funding.idempotency_key == "telegram:bank-activity-pot:25:1"
        text = str(outbound.commands[-1].payload["text"])
        assert "奖池当前余额：52" in text
        assert "11111111-1111-4111-8111-111111111111" in text

    asyncio.run(scenario())


def test_bank_pending_lists_only_for_admin_with_a_bounded_limit() -> None:
    """@brief `/bank_pending [limit]` 仅管理员可读且返回可审核事实 /
    `/bank_pending [limit]` is admin-only and returns reviewable facts.
    """

    async def scenario() -> None:
        """@brief 执行管理员和非管理员查询 / Execute administrator and non-administrator queries.

        @return None / None.
        """

        operations = _Operations()
        pending = RequestTokens(
            user_id=42,
            amount=TokenAmount(7),
            purpose="补充概率活动测试储备",
            requested_at=NOW,
            idempotency_key="test:pending:request",
            request_id=UUID("22222222-2222-4222-8222-222222222222"),
        ).aggregate()
        operations.pending_requests = (pending,)
        outbound = _Outbound()
        handler = BankTelegramCommandHandler(
            bank=BankService(operations, administrator_id=1),
            outbound=outbound,
        )

        await handler.handle(_update(26), _command("bank_pending", "5", user_id=99))
        assert operations.pending_queries == []
        assert "只有银行管理员" in str(outbound.commands[-1].payload["text"])

        await handler.handle(_update(27), _command("bank_pending", "5", user_id=1))
        assert operations.pending_queries == [ListPendingTokenRequests(1, limit=5)]
        text = str(outbound.commands[-1].payload["text"])
        assert str(pending.request_id) in text
        assert "用户 42｜7 枚" in text
        assert "/bank_review" in text

    asyncio.run(scenario())


def test_bank_overview_keeps_free_and_paid_legacy_separate() -> None:
    """@brief `/bank` 明确展示两种口袋 / `/bank` explicitly displays both pockets."""

    async def scenario() -> None:
        """@brief 执行概览场景 / Execute the overview scenario.

        @return None / None.
        """

        operations = _Operations()
        outbound = _Outbound()
        handler = BankTelegramCommandHandler(
            bank=BankService(operations, administrator_id=1),
            outbound=outbound,
        )

        await handler.handle(_update(23), _command("bank"))

        text = str(outbound.commands[0].payload["text"])
        assert "免费金币（Free）：9" in text
        assert "历史付费金币（Paid legacy）：3" in text

    asyncio.run(scenario())
