"""@brief Assistant 到银行只读与待审申请操作 / Assistant operations for bank reads and pending applications.

``bank_request_tokens`` 只创建 ``PENDING`` 审核申请，绝不直接改变钱包余额；因此它
不属于资产确认状态机。实际会审核、发行或注资的工具由 asset-action confirmation
operation 单独处理。/ ``bank_request_tokens`` creates only a ``PENDING`` review request and
never changes a wallet balance, so it does not belong to the asset-confirmation state machine.
Tools that actually review, issue, or fund are handled separately by the asset-action confirmation
operation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable
from uuid import UUID, uuid5

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.banking.json_projection import (
    bank_overview_json,
    pending_token_requests_result_json,
    token_request_result_json,
)
from fogmoe_bot.application.banking.models import ListPendingTokenRequests, RequestTokens
from fogmoe_bot.application.banking.service import BankService
from fogmoe_bot.domain.banking.money import TokenAmount
from fogmoe_bot.domain.conversation.payloads import JsonObject

from .parsing import bounded_int, required_text


type UtcNow = Callable[[], datetime]
"""@brief 可注入 UTC 时钟 / Injectable UTC clock."""


_BANK_REQUEST_NAMESPACE = UUID("66a022cb-b48e-4f9b-a620-4126e507b9bc")
"""@brief Assistant 申请 UUIDv5 命名空间 / UUIDv5 namespace for Assistant bank applications."""


def _utc_now() -> datetime:
    """@brief 返回当前 UTC 时刻 / Return the current UTC instant.

    @return aware UTC 时间 / Aware UTC time.
    """

    return datetime.now(UTC)


class AssistantBankToolOperation:
    """@brief 以可信工具上下文调用银行读取与申请 / Call bank reads and applications with trusted tool context.

    所有身份都取自 ``ToolExecutionContext``；工具参数中没有 ``user_id``、
    ``administrator_id`` 或其他 actor 字段。/ Every identity comes from
    ``ToolExecutionContext``; tool arguments contain no ``user_id``,
    ``administrator_id``, or other actor field.
    """

    def __init__(self, *, bank: BankService, now: UtcNow = _utc_now) -> None:
        """@brief 注入银行服务与可测试时钟 / Inject the bank service and a testable clock.

        @param bank 负责最终授权与原子银行操作的服务 / Service owning final authorization and atomic banking operations.
        @param now 可替换 UTC 时钟 / Replaceable UTC clock.
        @return None / None.
        """

        self._bank = bank
        self._now = now

    async def execute(self, request: ToolEffectRequest) -> JsonObject:
        """@brief 执行一项受支持的银行工具 / Execute one supported bank tool.

        @param request 已通过 catalog 校验的工具请求 / Tool request validated by the catalog.
        @return 可回填模型且不含内部对象的 JSON / JSON safe for model feedback and free of internal objects.
        @raise ValueError 工具名、副作用分类或上下文不变量错误时抛出 /
            Raised for an invalid tool name, effect classification, or context invariant.
        """

        private_rejection = _require_private_context(request)
        if private_rejection is not None:
            return private_rejection
        match request.tool_name:
            case "bank_request_tokens":
                return await self._request_tokens(request)
            case "bank_get_overview":
                return await self._get_overview(request)
            case "bank_list_pending_token_requests":
                return await self._list_pending_token_requests(request)
            case _:
                raise ValueError("Assistant bank operation received an unknown tool")

    async def _request_tokens(self, request: ToolEffectRequest) -> JsonObject:
        """@brief 以当前认证用户创建待审核申请 / Create a pending request as the authenticated user.

        @param request 已校验的申请工具请求 / Validated application tool request.
        @return 已创建或幂等重放的申请 JSON / Created or idempotently replayed application JSON.
        @raise ValueError 副作用分类或参数形状不一致时抛出 /
            Raised for inconsistent effect classification or argument shape.
        """

        if not request.mutating or request.effect_kind != "bank.request_tokens":
            raise ValueError("bank_request_tokens requires its direct-request effect kind")
        result = await self._bank.request_tokens(
            RequestTokens(
                user_id=request.context.user_id,
                amount=TokenAmount(
                    bounded_int(
                        request.arguments,
                        "amount",
                        minimum=1,
                        maximum=1_000_000,
                    )
                ),
                purpose=required_text(request.arguments, "purpose"),
                requested_at=self._now(),
                idempotency_key=_request_idempotency_key(request),
                request_id=_request_id(request),
            )
        )
        projected = token_request_result_json(result)
        projected["status"] = (
            "pending_review" if result.code.value == "success" else "rejected"
        )
        return projected

    async def _get_overview(self, request: ToolEffectRequest) -> JsonObject:
        """@brief 读取当前认证用户自己的钱包 / Read only the authenticated user's own wallet.

        @param request 已校验的只读工具请求 / Validated read-only tool request.
        @return 钱包 JSON 或未注册结果 / Wallet JSON or a not-registered result.
        @raise ValueError catalog 将只读工具错误标为 mutation 时抛出 /
            Raised if the catalog incorrectly classifies the read as a mutation.
        """

        if request.mutating:
            raise ValueError("bank_get_overview must be a read-only tool")
        overview = await self._bank.read_overview(request.context.user_id)
        return {
            "code": "success" if overview is not None else "not_registered",
            "overview": bank_overview_json(overview),
        }

    async def _list_pending_token_requests(
        self,
        request: ToolEffectRequest,
    ) -> JsonObject:
        """@brief 列出管理员可见的待审申请 / List pending applications visible to an administrator.

        @param request 已校验的只读工具请求 / Validated read-only tool request.
        @return 银行服务授权后的待审申请 JSON / Pending-application JSON after BankService authorization.
        @raise ValueError catalog 将只读工具错误标为 mutation 时抛出 /
            Raised if the catalog incorrectly classifies the read as a mutation.
        """

        if request.mutating:
            raise ValueError("bank_list_pending_token_requests must be a read-only tool")
        result = await self._bank.list_pending_token_requests(
            ListPendingTokenRequests(
                administrator_id=request.context.user_id,
                limit=bounded_int(
                    request.arguments,
                    "limit",
                    minimum=1,
                    maximum=20,
                    default=20,
                ),
            )
        )
        return pending_token_requests_result_json(result)


def _require_private_context(request: ToolEffectRequest) -> JsonObject | None:
    """@brief 拒绝非当前用户 Telegram 私聊的银行工具 / Reject bank tools outside the current user's Telegram private chat.

    @param request 已校验的工具请求 / Validated tool request.
    @return 无问题时 ``None``，否则安全拒绝 JSON / ``None`` when valid, otherwise a safe rejection JSON.
    """

    context = request.context
    if (
        context.is_group
        or isinstance(context.user_id, bool)
        or not isinstance(context.user_id, int)
        or context.user_id <= 0
        or isinstance(context.chat_id, bool)
        or not isinstance(context.chat_id, int)
        or context.chat_id <= 0
        or context.chat_id != context.user_id
    ):
        return {
            "status": "rejected",
            "reason": "private_chat_required",
            "message": "银行工具只能在当前用户与 Bot 的私聊中使用。",
        }
    return None


def _request_source_key(request: ToolEffectRequest) -> str:
    """@brief 从稳定工具调用构造银行申请来源键 / Build a bank-application source key from a stable tool invocation.

    @param request 已校验的工具请求 / Validated tool request.
    @return 可作为银行幂等键和 UUIDv5 名称的来源键 / Source key usable as a bank idempotency key and UUIDv5 name.
    @raise ValueError 来源键超过银行幂等键上限时抛出 /
        Raised when the source key exceeds the bank idempotency-key bound.
    """

    source_key = (
        f"assistant:bank-request:{request.context.turn_id}:{request.invocation_id}"
    )
    if len(source_key) > 200:
        raise ValueError("Assistant bank-request idempotency key exceeds bank limit")
    return source_key


def _request_id(request: ToolEffectRequest) -> UUID:
    """@brief 从工具调用稳定推导申请 UUID / Derive a stable application UUID from a tool invocation.

    @param request 已校验的工具请求 / Validated tool request.
    @return 稳定请求 UUID / Stable request UUID.
    """

    return uuid5(_BANK_REQUEST_NAMESPACE, _request_source_key(request))


def _request_idempotency_key(request: ToolEffectRequest) -> str:
    """@brief 返回稳定银行申请幂等键 / Return the stable bank-application idempotency key.

    @param request 已校验的工具请求 / Validated tool request.
    @return 长度受限幂等键 / Length-bounded idempotency key.
    """

    return _request_source_key(request)


__all__ = ["AssistantBankToolOperation"]
