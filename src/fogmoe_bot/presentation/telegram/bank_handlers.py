"""@brief Durable Telegram 银行命令 / Durable Telegram bank commands."""

from __future__ import annotations

from uuid import UUID, uuid4

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
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCapability,
)
from fogmoe_bot.domain.banking.money import TokenAmount, TokenBucket
from fogmoe_bot.domain.banking.requests import TokenRequestStatus
from fogmoe_bot.domain.conversation.inbox import InboundUpdate

from .command_cooldown_guard import ParsedTelegramCommand
from .delivery import enqueue_command_reply


_PRIVATE_ONLY_TEXT = "银行命令仅限私聊使用喵，请私聊 Bot 后再试。"
"""@brief 银行私聊边界提示 / Bank private-chat boundary prompt."""


class BankTelegramCommandHandler:
    """@brief 将银行命令映射到 typed service 与 durable outbox / Map bank commands to a typed service and durable outbox."""

    def __init__(
        self,
        *,
        bank: BankService,
        outbound: StandaloneOutboundCapability,
    ) -> None:
        """@brief 注入银行服务与可靠投递能力 / Inject the bank service and durable delivery capability.

        @param bank 银行应用服务 / Banking application service.
        @param outbound standalone outbox 能力 / Standalone-outbox capability.
        """

        self._bank = bank
        self._outbound = outbound

    @property
    def commands(self) -> frozenset[str]:
        """@brief 返回银行命令所有权 / Return bank command ownership.

        @return bank/request_tokens/recharge/bank_review/bank_issue/bank_fund_activity/bank_pending /
            Bank command set.
        """

        return frozenset(
            {
                "bank",
                "request_tokens",
                "recharge",
                "bank_review",
                "bank_issue",
                "bank_fund_activity",
                "bank_pending",
            }
        )

    async def handle(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> None:
        """@brief 执行银行命令并写入确定性回复 / Execute a bank command and enqueue a deterministic reply.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析的命令 envelope / Parsed command envelope.
        @return None / None.
        """

        if command.chat_type != "private":
            text = _PRIVATE_ONLY_TEXT
        elif command.command == "bank":
            text = await self._overview_text(command)
        elif command.command in {"request_tokens", "recharge"}:
            text = await self._request_text(update, command)
        elif command.command == "bank_review":
            text = await self._review_text(update, command)
        elif command.command == "bank_issue":
            text = await self._issue_text(update, command)
        elif command.command == "bank_fund_activity":
            text = await self._fund_activity_text(update, command)
        elif command.command == "bank_pending":
            text = await self._pending_text(command)
        else:
            raise ValueError("Bank handler received an unowned command")
        await enqueue_command_reply(self._outbound, update, command, text)

    async def _overview_text(self, command: ParsedTelegramCommand) -> str:
        """@brief 读取并渲染个人银行钱包 / Load and render a personal bank wallet.

        @param command 已解析 `/bank` 命令 / Parsed `/bank` command.
        @return 用户可见文本 / User-facing text.
        """

        overview = await self._bank.overview(command.user_id)
        if overview is None:
            return "请先使用 /me 注册账户，再查看银行钱包喵。"
        return _overview_text(overview)

    async def _request_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 创建免费代币申请 / Create a free-token request.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析申请命令 / Parsed request command.
        @return 用户可见文本 / User-facing text.
        """

        parsed = _request_arguments(command.argument_text)
        if isinstance(parsed, str):
            return parsed
        amount, purpose = parsed
        result = await self._bank.request_tokens(
            RequestTokens(
                user_id=command.user_id,
                amount=amount,
                purpose=purpose,
                requested_at=update.received_at,
                idempotency_key=(
                    f"telegram:bank-request:{int(update.update_id)}:{command.user_id}"
                ),
                request_id=uuid4(),
            )
        )
        return _request_result_text(result)

    async def _review_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 审核一笔待处理代币申请 / Review one pending token request.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析审核命令 / Parsed review command.
        @return 用户可见文本 / User-facing text.
        """

        parsed = _review_arguments(command.argument_text)
        if isinstance(parsed, str):
            return parsed
        request_id, decision, note = parsed
        result = await self._bank.review_token_request(
            ReviewTokenRequest(
                request_id=request_id,
                reviewer_id=command.user_id,
                decision=decision,
                reviewed_at=update.received_at,
                idempotency_key=(
                    f"telegram:bank-review:{int(update.update_id)}:{command.user_id}"
                ),
                note=note,
            )
        )
        return _review_result_text(result)

    async def _issue_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 以管理员权限直接发行 free 代币 / Directly issue free tokens as an administrator.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析发行命令 / Parsed issuance command.
        @return 用户可见文本 / User-facing text.
        """

        parsed = _issue_arguments(command.argument_text)
        if isinstance(parsed, str):
            return parsed
        recipient_id, amount, purpose = parsed
        result = await self._bank.issue_tokens(
            IssueTokens(
                administrator_id=command.user_id,
                recipient_id=recipient_id,
                amount=amount,
                bucket=TokenBucket.FREE,
                purpose=purpose,
                issued_at=update.received_at,
                idempotency_key=(
                    f"telegram:bank-issue:{int(update.update_id)}:{command.user_id}"
                ),
            )
        )
        if result.code is BankCode.FORBIDDEN:
            return "只有银行管理员可以直接发行代币。"
        if result.code is BankCode.NOT_REGISTERED:
            return "收款用户尚未注册，不能发行代币。"
        if result.code is not BankCode.SUCCESS or result.overview is None:
            return "银行发行未完成，请稍后重试。"
        return (
            f"已向用户 {recipient_id} 发行 {amount.value} 枚免费金币。\n"
            f"该用户当前免费余额：{result.overview.free.value}。"
        )

    async def _fund_activity_text(
        self,
        update: InboundUpdate,
        command: ParsedTelegramCommand,
    ) -> str:
        """@brief 显式为概率活动奖池注资 / Explicitly fund the chance-activity pot.

        @param update durable 来源 Update / Durable source Update.
        @param command 已解析管理员注资命令 / Parsed administrator-funding command.
        @return 用户可见成功或授权错误文本 / User-visible success or authorization text.
        @note 此命令是活动派彩所需发行的唯一人工入口；活动结算不自行铸币。/
            This command is the sole manual issuance ingress for activity payouts; settlement
            never mints tokens itself.
        """

        parsed = _fund_activity_arguments(command.argument_text)
        if isinstance(parsed, str):
            return parsed
        amount, purpose = parsed
        result = await self._bank.fund_activity_pot(
            FundActivityPot(
                administrator_id=command.user_id,
                amount=amount,
                purpose=purpose,
                funded_at=update.received_at,
                idempotency_key=(
                    f"telegram:bank-activity-pot:{int(update.update_id)}:"
                    f"{command.user_id}"
                ),
            )
        )
        return _activity_pot_funding_text(result)

    async def _pending_text(self, command: ParsedTelegramCommand) -> str:
        """@brief 列出管理员待审核的代币申请 / List token requests awaiting administrator review.

        @param command 已解析管理员查询命令 / Parsed administrator query command.
        @return 用户可见列表或授权错误 / User-visible list or authorization error.
        """

        limit = _pending_limit(command.argument_text)
        if isinstance(limit, str):
            return limit
        result = await self._bank.list_pending_token_requests(
            ListPendingTokenRequests(
                administrator_id=command.user_id,
                limit=limit,
            )
        )
        return _pending_requests_text(result)


def _request_arguments(argument_text: str) -> tuple[TokenAmount, str] | str:
    """@brief 解析 `/request_tokens <amount> <purpose>` / Parse `/request_tokens <amount> <purpose>`.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 金额和用途，或用户可见用法错误 / Amount and purpose, or a user-visible usage error.
    """

    parts = argument_text.split(maxsplit=1)
    if len(parts) != 2:
        return "用法：/request_tokens <数量> <用途说明>\n/recharge 也会创建同样的代币申请。"
    amount = _positive_amount(parts[0])
    if isinstance(amount, str):
        return amount
    purpose = parts[1].strip()
    if not 1 <= len(purpose) <= 500:
        return "用途说明需要 1–500 个字符。"
    return amount, purpose


def _review_arguments(
    argument_text: str,
) -> tuple[UUID, TokenReviewDecision, str | None] | str:
    """@brief 解析 `/bank_review <request_id> approve|reject [note]` / Parse an administrator review command.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 请求 ID、决定和说明，或用户可见用法错误 / Request ID, decision and note, or a usage error.
    """

    parts = argument_text.split(maxsplit=2)
    if len(parts) < 2:
        return "用法：/bank_review <申请ID> approve|reject [说明]"
    try:
        request_id = UUID(parts[0])
    except ValueError:
        return "申请 ID 必须是有效 UUID。"
    try:
        decision = TokenReviewDecision(parts[1].casefold())
    except ValueError:
        return "审核决定只能是 approve 或 reject。"
    note = parts[2].strip() if len(parts) == 3 else None
    if note is not None and len(note) > 500:
        return "审核说明最多 500 个字符。"
    return request_id, decision, note or None


def _issue_arguments(argument_text: str) -> tuple[int, TokenAmount, str] | str:
    """@brief 解析 `/bank_issue <user_id> <amount> <purpose>` / Parse a direct bank-issuance command.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 收款人、金额和用途，或用户可见用法错误 / Recipient, amount and purpose, or a usage error.
    """

    parts = argument_text.split(maxsplit=2)
    if len(parts) != 3:
        return "用法：/bank_issue <用户ID> <数量> <审计用途>"
    try:
        recipient_id = int(parts[0])
    except ValueError:
        return "用户 ID 必须是正整数。"
    if recipient_id <= 0:
        return "用户 ID 必须是正整数。"
    amount = _positive_amount(parts[1])
    if isinstance(amount, str):
        return amount
    purpose = parts[2].strip()
    if not 1 <= len(purpose) <= 500:
        return "审计用途需要 1–500 个字符。"
    return recipient_id, amount, purpose


def _fund_activity_arguments(argument_text: str) -> tuple[TokenAmount, str] | str:
    """@brief 解析 `/bank_fund_activity <amount> <purpose>` / Parse an activity-pot funding command.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 金额和审计用途，或用户可见用法错误 / Amount and audit purpose, or a user-visible usage error.
    """

    parts = argument_text.split(maxsplit=1)
    if len(parts) != 2:
        return "用法：/bank_fund_activity <数量> <审计用途>"
    amount = _positive_amount(parts[0])
    if isinstance(amount, str):
        return amount
    purpose = parts[1].strip()
    if not 1 <= len(purpose) <= 500:
        return "审计用途需要 1–500 个字符。"
    return amount, purpose


def _pending_limit(argument_text: str) -> int | str:
    """@brief 解析 `/bank_pending [limit]` 的有界分页参数 / Parse bounded `/bank_pending [limit]` pagination.

    @param argument_text 原始参数文本 / Raw argument text.
    @return 1–20 的 limit，或用户可见用法错误 / Limit from 1 to 20, or a user-visible usage error.
    """

    normalized = argument_text.strip()
    if not normalized:
        return 20
    if len(normalized.split()) != 1:
        return "用法：/bank_pending [1–20]"
    try:
        limit = int(normalized)
    except ValueError:
        return "数量必须是 1–20 的整数。"
    if not 1 <= limit <= 20:
        return "数量必须是 1–20 的整数。"
    return limit


def _positive_amount(raw_amount: str) -> TokenAmount | str:
    """@brief 解析严格正整数金币 / Parse a strictly positive integral token amount.

    @param raw_amount 原始金额 / Raw amount.
    @return 金额值对象，或用户可见错误 / Amount value object, or a user-visible error.
    """

    try:
        return TokenAmount(int(raw_amount))
    except TypeError, ValueError:
        return "数量必须是正整数。"


def _overview_text(overview: BankOverview) -> str:
    """@brief 渲染用户银行概览 / Render a user's bank overview.

    @param overview 双钱包概览 / Two-pocket wallet overview.
    @return 用户可见文本 / User-facing text.
    """

    return (
        "🏦 FogMoe 银行\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"免费金币（Free）：{overview.free.value}\n"
        f"历史付费金币（Paid legacy）：{overview.paid.value}\n"
        f"总余额：{overview.total}\n\n"
        "免费金币可由任务、活动或银行审批发行；历史付费金币保持隔离，"
        "不会通过充值或订阅继续售卖。\n"
        "申请代币：/request_tokens <数量> <用途>"
    )


def _activity_pot_funding_text(result: ActivityPotFundingResult) -> str:
    """@brief 渲染管理员活动奖池注资结果 / Render administrator activity-pot funding result.

    @param result 类型化注资结果 / Typed funding result.
    @return 用户可见注资结果文本 / User-visible funding-result text.
    """

    if result.code is BankCode.FORBIDDEN:
        return "只有银行管理员可以为活动奖池注资。"
    if result.code is BankCode.NOT_REGISTERED:
        return "银行管理员身份尚未注册，不能执行奖池注资。"
    if result.code is not BankCode.SUCCESS:
        return "活动奖池注资未完成，请稍后重试。"
    if (
        result.amount is None
        or result.activity_pot_balance is None
        or result.ledger_entry_id is None
    ):
        raise RuntimeError("Successful activity-pot funding lacks audit facts")
    replay = "\n（本次为同源幂等重放。）" if result.replayed else ""
    return (
        f"已向可验证活动奖池注资 {result.amount.value} 枚免费金币。\n"
        f"奖池当前余额：{result.activity_pot_balance}\n"
        f"账本分录：{result.ledger_entry_id}{replay}"
    )


def _pending_requests_text(result: PendingTokenRequestsResult) -> str:
    """@brief 渲染待审批代币申请的紧凑审计列表 / Render a compact auditable pending-token-request list.

    @param result 类型化待审批列表结果 / Typed pending-list result.
    @return 用户可见列表文本 / User-visible list text.
    """

    if result.code is BankCode.FORBIDDEN:
        return "只有银行管理员可以查看待审核代币申请。"
    if result.code is BankCode.NOT_REGISTERED:
        return "银行管理员身份尚未注册，不能查看待审核申请。"
    if result.code is not BankCode.SUCCESS:
        return "待审核申请暂时无法读取，请稍后重试。"
    if not result.requests:
        return "当前没有待审核的代币申请。"
    lines = [f"待审核代币申请（{len(result.requests)} 条）："]
    for request in result.requests:
        purpose = " ".join(request.purpose.split())[:80]
        lines.append(
            f"• {request.request_id}\n"
            f"  用户 {request.requester_id}｜{request.requested_amount.value} 枚｜"
            f"{request.requested_at.isoformat()}\n"
            f"  用途：{purpose}"
        )
    lines.append("审核：/bank_review <申请ID> approve|reject [说明]")
    return "\n".join(lines)


def _request_result_text(result: TokenRequestResult) -> str:
    """@brief 渲染代币申请结果 / Render a token-request result.

    @param result typed 申请结果 / Typed request result.
    @return 用户可见文本 / User-facing text.
    """

    if result.code is BankCode.NOT_REGISTERED:
        return "请先使用 /me 注册账户，再申请代币。"
    if result.code is not BankCode.SUCCESS or result.request is None:
        return "代币申请暂未创建，请稍后重试。"
    request = result.request
    return (
        "代币申请已提交，等待银行管理员审核喵。\n"
        f"申请 ID：{request.request_id}\n"
        f"数量：{request.requested_amount.value}\n"
        f"用途：{request.purpose}"
    )


def _review_result_text(result: TokenRequestResult) -> str:
    """@brief 渲染管理员审核结果 / Render an administrator review result.

    @param result typed 审核结果 / Typed review result.
    @return 用户可见文本 / User-facing text.
    """

    if result.code is BankCode.FORBIDDEN:
        return "没有审核权限；申请者也不能审核自己的申请。"
    if result.code is BankCode.NOT_FOUND:
        return "未找到该代币申请。"
    if result.code is BankCode.NOT_PENDING:
        return "该申请已不是待审核状态。"
    if result.code is not BankCode.SUCCESS or result.request is None:
        return "审核未完成，请稍后重试。"
    request = result.request
    if request.status is TokenRequestStatus.APPROVED:
        balance = (
            result.overview.free.value if result.overview is not None else "已更新"
        )
        return (
            f"申请已批准，已发行 {request.requested_amount.value} 枚免费金币。\n"
            f"账本分录：{request.ledger_entry_id}\n"
            f"申请人当前免费余额：{balance}"
        )
    return f"申请已拒绝。申请 ID：{request.request_id}"


__all__ = ["BankTelegramCommandHandler"]
