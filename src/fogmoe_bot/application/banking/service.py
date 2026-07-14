"""@brief 银行应用服务 / Banking application service."""

from __future__ import annotations

from .models import (
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
from .ports import BankOperations


BANK_SERVICE_DATA_KEY = "banking.service"
"""@brief runtime capability 中银行服务的稳定键 / Stable bank-service key in runtime capability."""


class BankService:
    """@brief 集中执行银行授权与代币流程 / Centrally execute bank authorization and token workflows."""

    def __init__(self, operations: BankOperations, *, administrator_id: int) -> None:
        """@brief 注入银行原子操作和管理员身份 / Inject atomic bank operations and administrator identity.

        @param operations 原子银行操作 / Atomic banking operations.
        @param administrator_id 银行管理员 Telegram ID / Bank administrator Telegram ID.
        """

        if administrator_id <= 0:
            raise ValueError("Bank administrator must be positive")
        self._operations = operations
        self._administrator_id = administrator_id

    async def request_tokens(self, command: RequestTokens) -> TokenRequestResult:
        """@brief 创建用户免费代币申请 / Create a user's free-token request.

        @param command 请求命令 / Request command.
        @return 请求结果 / Request result.
        """

        return await self._operations.request_tokens(command)

    async def review_token_request(
        self, command: ReviewTokenRequest
    ) -> TokenRequestResult:
        """@brief 审核请求并保护银行管理员权限 / Review a request and protect bank-admin authority.

        @param command 审核命令 / Review command.
        @return 审核结果 / Review result.
        """

        if command.reviewer_id != self._administrator_id:
            return TokenRequestResult(BankCode.FORBIDDEN)
        return await self._operations.review_token_request(command)

    async def issue_tokens(self, command: IssueTokens) -> TokenRequestResult:
        """@brief 执行管理员直接发行 / Execute an administrator's direct issuance.

        @param command 发行命令 / Issuance command.
        @return 发行结果 / Issuance result.
        """

        if command.administrator_id != self._administrator_id:
            return TokenRequestResult(BankCode.FORBIDDEN)
        return await self._operations.issue_tokens(command)

    async def fund_activity_pot(
        self, command: FundActivityPot
    ) -> ActivityPotFundingResult:
        """@brief 由银行管理员显式注资概率活动奖池 / Explicitly fund the chance-activity pot as bank administrator.

        @param command 管理员注资命令 / Administrator funding command.
        @return 可审计注资结果 / Auditable funding result.
        """

        if command.administrator_id != self._administrator_id:
            return ActivityPotFundingResult(BankCode.FORBIDDEN)
        return await self._operations.fund_activity_pot(command)

    async def list_pending_token_requests(
        self, command: ListPendingTokenRequests
    ) -> PendingTokenRequestsResult:
        """@brief 列出管理员待审核的免费代币申请 / List free-token requests awaiting administrator review.

        @param command 管理员分页查询命令 / Administrator paginated query command.
        @return 待审批列表或授权错误 / Pending list or authorization error.
        """

        if command.administrator_id != self._administrator_id:
            return PendingTokenRequestsResult(BankCode.FORBIDDEN)
        return await self._operations.list_pending_token_requests(command)

    async def overview(self, user_id: int) -> BankOverview | None:
        """@brief 查询用户钱包概览 / Query a user's wallet overview.

        @param user_id 用户标识 / User identity.
        @return 钱包概览；未注册为 None / Wallet overview, or None when unregistered.
        """

        if user_id <= 0:
            raise ValueError("Bank overview user must be positive")
        return await self._operations.overview(user_id)
