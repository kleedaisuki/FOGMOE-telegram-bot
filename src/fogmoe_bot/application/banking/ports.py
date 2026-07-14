"""@brief 银行应用持久化端口 / Banking application persistence ports."""

from __future__ import annotations

from typing import Protocol

from .models import (
    ActivityPotFundingResult,
    BankOverview,
    FundActivityPot,
    IssueTokens,
    ListPendingTokenRequests,
    PendingTokenRequestsResult,
    RequestTokens,
    ReviewTokenRequest,
    TokenRequestResult,
)


class BankOperations(Protocol):
    """@brief 银行原子操作能力 / Atomic banking-operation capability."""

    async def request_tokens(self, command: RequestTokens) -> TokenRequestResult:
        """@brief 创建或重放代币请求 / Create or replay a token request.

        @param command 代币请求命令 / Token-request command.
        @return 请求结果 / Request result.
        """

        ...

    async def review_token_request(
        self, command: ReviewTokenRequest
    ) -> TokenRequestResult:
        """@brief 审核并在批准时原子发行 / Review and atomically issue on approval.

        @param command 审核命令 / Review command.
        @return 审核结果 / Review result.
        """

        ...

    async def issue_tokens(self, command: IssueTokens) -> TokenRequestResult:
        """@brief 由银行管理员直接发行 / Issue directly as a bank administrator.

        @param command 发行命令 / Issuance command.
        @return 发行结果 / Issuance result.
        """

        ...

    async def fund_activity_pot(
        self, command: FundActivityPot
    ) -> ActivityPotFundingResult:
        """@brief 从受控发行账户为活动奖池注资 / Fund the activity pot from controlled issuance.

        @param command 管理员可审计注资命令 / Auditable administrator-funding command.
        @return 注资结果与成功后的奖池余额 / Funding result and post-success pot balance.
        """

        ...

    async def list_pending_token_requests(
        self, command: ListPendingTokenRequests
    ) -> PendingTokenRequestsResult:
        """@brief 读取待管理员审核的代币申请 / Read token requests awaiting administrator review.

        @param command 管理员分页查询命令 / Administrator paginated query command.
        @return 待审批申请列表 / Pending request list.
        """

        ...

    async def overview(self, user_id: int) -> BankOverview | None:
        """@brief 读取用户双钱包概览 / Read a user's dual-wallet overview.

        @param user_id 用户标识 / User identity.
        @return 钱包概览；未注册为 None / Wallet overview, or None when unregistered.
        """

        ...

    async def read_overview(self, user_id: int) -> BankOverview | None:
        """@brief 只读查询已初始化的钱包概览 / Read an initialized wallet overview without creating it.

        @param user_id 用户标识 / User identity.
        @return 已存在的钱包概览；身份或钱包未初始化时为 None /
            Existing wallet overview, or None when the identity or wallet is not initialized.
        @note 此端口不能隐式创建账户、钱包或余额投影，供无确认的 Agent 查询使用。/
            This port must not implicitly create an account, wallet, or balance projection; it is
            used by Agent reads that need no confirmation.
        """

        ...
