"""@brief 银行应用服务测试 / Banking application-service tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

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
    TokenReviewDecision,
)
from fogmoe_bot.application.banking.service import BankService
from fogmoe_bot.domain.banking.money import TokenAmount, TokenBucket, WalletBalance


class _Operations:
    """@brief 记录银行服务调用的内存端口 / In-memory port recording bank-service calls."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call recordings."""

        self.request_commands: list[RequestTokens] = []
        self.review_commands: list[ReviewTokenRequest] = []
        self.issue_commands: list[IssueTokens] = []
        """@brief 已收到的用户钱包发行命令 / Received user-wallet issuance commands."""
        self.activity_pot_commands: list[FundActivityPot] = []
        """@brief 已收到的活动奖池注资命令 / Received activity-pot funding commands."""
        self.pending_commands: list[ListPendingTokenRequests] = []
        """@brief 已收到的待审批查询命令 / Received pending-request query commands."""
        self.read_overview_users: list[int] = []
        """@brief 纯读取钱包概览的用户 / Users queried through the pure-read overview port."""

    async def request_tokens(self, command: RequestTokens) -> TokenRequestResult:
        """@brief 记录请求命令 / Record a request command.

        @param command 请求命令 / Request command.
        @return 成功结果 / Successful result.
        """

        self.request_commands.append(command)
        return TokenRequestResult(BankCode.SUCCESS, request=command.aggregate())

    async def review_token_request(
        self, command: ReviewTokenRequest
    ) -> TokenRequestResult:
        """@brief 记录审核命令 / Record a review command.

        @param command 审核命令 / Review command.
        @return 成功结果 / Successful result.
        """

        self.review_commands.append(command)
        return TokenRequestResult(BankCode.SUCCESS)

    async def issue_tokens(self, command: IssueTokens) -> TokenRequestResult:
        """@brief 记录发行命令 / Record an issuance command.

        @param command 发行命令 / Issuance command.
        @return 成功结果 / Successful result.
        """

        self.issue_commands.append(command)
        return TokenRequestResult(BankCode.SUCCESS)

    async def fund_activity_pot(
        self, command: FundActivityPot
    ) -> ActivityPotFundingResult:
        """@brief 记录活动奖池注资 / Record activity-pot funding.

        @param command 管理员注资命令 / Administrator-funding command.
        @return 固定成功结果 / Fixed successful result.
        """

        self.activity_pot_commands.append(command)
        return ActivityPotFundingResult(
            BankCode.SUCCESS,
            amount=command.amount,
            activity_pot_balance=command.amount.value,
            ledger_entry_id=uuid4(),
        )

    async def list_pending_token_requests(
        self,
        command: ListPendingTokenRequests,
    ) -> PendingTokenRequestsResult:
        """@brief 记录待审批查询 / Record a pending-request query.

        @param command 管理员查询命令 / Administrator query command.
        @return 空成功列表 / Empty successful list.
        """

        self.pending_commands.append(command)
        return PendingTokenRequestsResult(BankCode.SUCCESS)

    async def overview(self, user_id: int) -> BankOverview | None:
        """@brief 返回固定钱包概览 / Return a fixed wallet overview.

        @param user_id 用户标识 / User identity.
        @return 钱包概览 / Wallet overview.
        """

        return BankOverview(
            user_id,
            WalletBalance(TokenBucket.FREE, 5),
            WalletBalance(TokenBucket.PAID, 2),
        )

    async def read_overview(self, user_id: int) -> BankOverview | None:
        """@brief 记录纯读取钱包概览 / Record a pure-read wallet overview.

        @param user_id 用户标识 / User identity.
        @return 固定钱包概览 / Fixed wallet overview.
        """

        self.read_overview_users.append(user_id)
        return BankOverview(
            user_id,
            WalletBalance(TokenBucket.FREE, 5),
            WalletBalance(TokenBucket.PAID, 2),
        )


def test_bank_service_leaves_user_request_open_but_gates_admin_actions() -> None:
    """@brief 普通用户可申请，只有管理员可审核和发行 / Ordinary users may request while only admin reviews and issues."""

    async def scenario() -> None:
        """@brief 执行授权场景 / Execute authorization scenario.

        @return None / None.
        """

        operations = _Operations()
        service = BankService(operations, administrator_id=1)
        now = datetime.now(UTC)
        request = RequestTokens(
            user_id=42,
            amount=TokenAmount(12),
            purpose="完成个人 RPG 新手任务",
            requested_at=now,
            idempotency_key="test:bank:request:1",
            request_id=uuid4(),
        )
        request_result = await service.request_tokens(request)
        administrator_request = await service.request_tokens(
            RequestTokens(
                user_id=1,
                amount=TokenAmount(12),
                purpose="管理员不应创建无解申请",
                requested_at=now,
                idempotency_key="test:bank:request:administrator",
                request_id=uuid4(),
            )
        )
        forbidden_review = await service.review_token_request(
            ReviewTokenRequest(
                request_id=request.request_id,
                reviewer_id=2,
                decision=TokenReviewDecision.APPROVE,
                reviewed_at=now,
                idempotency_key="test:bank:review:forbidden",
            )
        )
        assert forbidden_review.code is BankCode.FORBIDDEN
        assert operations.review_commands == []
        allowed_review = await service.review_token_request(
            ReviewTokenRequest(
                request_id=request.request_id,
                reviewer_id=1,
                decision=TokenReviewDecision.APPROVE,
                reviewed_at=now,
                idempotency_key="test:bank:review:allowed",
            )
        )
        forbidden_issue = await service.issue_tokens(
            IssueTokens(
                administrator_id=2,
                recipient_id=42,
                amount=TokenAmount(5),
                bucket=TokenBucket.FREE,
                purpose="订阅权益",
                issued_at=now,
                idempotency_key="test:bank:issue:forbidden",
            )
        )
        forbidden_funding = await service.fund_activity_pot(
            FundActivityPot(
                administrator_id=2,
                amount=TokenAmount(8),
                purpose="为可验证概率活动准备派奖储备",
                funded_at=now,
                idempotency_key="test:bank:activity-pot:forbidden",
            )
        )
        allowed_funding = await service.fund_activity_pot(
            FundActivityPot(
                administrator_id=1,
                amount=TokenAmount(8),
                purpose="为可验证概率活动准备派奖储备",
                funded_at=now,
                idempotency_key="test:bank:activity-pot:allowed",
            )
        )
        forbidden_pending = await service.list_pending_token_requests(
            ListPendingTokenRequests(administrator_id=2, limit=5)
        )
        allowed_pending = await service.list_pending_token_requests(
            ListPendingTokenRequests(administrator_id=1, limit=5)
        )

        assert request_result.code is BankCode.SUCCESS
        assert administrator_request.code is BankCode.FORBIDDEN
        assert len(operations.request_commands) == 1
        assert allowed_review.code is BankCode.SUCCESS
        assert len(operations.review_commands) == 1
        assert forbidden_issue.code is BankCode.FORBIDDEN
        assert operations.issue_commands == []
        assert forbidden_funding.code is BankCode.FORBIDDEN
        assert allowed_funding.code is BankCode.SUCCESS
        assert len(operations.activity_pot_commands) == 1
        assert forbidden_pending.code is BankCode.FORBIDDEN
        assert allowed_pending.code is BankCode.SUCCESS
        assert operations.pending_commands == [ListPendingTokenRequests(1, limit=5)]
        assert (await service.overview(42)).total == 7  # type: ignore[union-attr]
        assert (await service.read_overview(42)).total == 7  # type: ignore[union-attr]
        assert operations.read_overview_users == [42]

    asyncio.run(scenario())
