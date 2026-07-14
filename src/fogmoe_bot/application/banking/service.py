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

    @property
    def administrator_id(self) -> int:
        """@brief 返回受配置保护的银行管理员身份 / Return the configured bank-administrator identity.

        @return 银行管理员 Telegram 用户 ID / Bank-administrator Telegram user ID.
        @note 此只读投影只供 presentation 层把待审核事实投递给正确的私聊；所有实际
            授权仍由本服务的写用例执行。/
            This read-only projection lets the presentation layer route pending facts to the
            right private chat; every state-changing authorization remains enforced by this
            service's write use cases.
        """

        return self._administrator_id

    async def request_tokens(self, command: RequestTokens) -> TokenRequestResult:
        """@brief 创建用户免费代币申请 / Create a user's free-token request.

        @param command 请求命令 / Request command.
        @return 请求结果 / Request result.
        @note 部署中只有一个银行管理员。管理员不能审核自己的申请，因此不允许创建一个
            永远无法决议的待审核请求；管理员应使用可审计的直接发行命令。/
            A deployment has one bank administrator. The administrator cannot review their own
            request, so this method refuses a pending request that could never be resolved; the
            administrator must use the auditable direct-issuance command instead.
        """

        if command.user_id == self._administrator_id:
            return TokenRequestResult(BankCode.FORBIDDEN)
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

    async def read_overview(self, user_id: int) -> BankOverview | None:
        """@brief 只读查询已初始化的钱包 / Read an initialized wallet without lazy creation.

        @param user_id 用户标识 / User identity.
        @return 已初始化的钱包概览；不存在或尚未初始化时为 None /
            Initialized wallet overview, or None when it does not exist yet.
        @raise ValueError 用户 ID 非正时抛出 / Raised when the user ID is not positive.
        @note ``overview`` 保留既有命令的惰性钱包初始化兼容性；此方法专供没有确认卡的
            Agent 查询，保证该工具是真正的读取。/ ``overview`` preserves the existing
            lazy-wallet initialization behavior for commands; this method is exclusively for
            confirmation-free Agent reads and guarantees a genuine read.
        """

        if user_id <= 0:
            raise ValueError("Bank overview user must be positive")
        return await self._operations.read_overview(user_id)
