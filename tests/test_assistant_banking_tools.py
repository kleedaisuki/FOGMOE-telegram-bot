"""@brief Assistant 银行直执行工具测试 / Tests for direct Assistant banking tools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.banking.models import (
    BankCode,
    BankOverview,
    RequestTokens,
    TokenRequestResult,
)
from fogmoe_bot.application.banking.service import BankService
from fogmoe_bot.domain.banking.money import TokenBucket, WalletBalance
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.infrastructure.assistant.tool_operations.banking import (
    AssistantBankToolOperation,
)


_NOW = datetime(2026, 7, 14, 10, tzinfo=UTC)
"""@brief 测试固定 UTC 时刻 / Fixed UTC instant for tests."""


class _Bank:
    """@brief 记录 Assistant 看到的银行调用 / Record banking calls visible to Assistant tools."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call recordings.

        @return None / None.
        """

        self.requests: list[RequestTokens] = []
        """@brief 已创建的待审核申请 / Created pending requests."""
        self.read_overview_users: list[int] = []
        """@brief 纯读取钱包调用的用户 / Users queried through the pure-read path."""
        self.lazy_overview_users: list[int] = []
        """@brief 不应由 Agent 触发的惰性钱包读取 / Lazy wallet reads that Agent must not trigger."""

    async def request_tokens(self, command: RequestTokens) -> TokenRequestResult:
        """@brief 记录申请并返回 pending 聚合 / Record a request and return its pending aggregate.

        @param command 已认证用户的申请 / Authenticated user's request.
        @return 成功申请结果 / Successful request result.
        """

        self.requests.append(command)
        return TokenRequestResult(BankCode.SUCCESS, request=command.aggregate())

    async def read_overview(self, user_id: int) -> BankOverview:
        """@brief 返回纯读取钱包概览 / Return a pure-read wallet overview.

        @param user_id 已认证用户 ID / Authenticated user ID.
        @return 固定概览 / Fixed overview.
        """

        self.read_overview_users.append(user_id)
        return BankOverview(
            user_id=user_id,
            free=WalletBalance(TokenBucket.FREE, 5),
            paid=WalletBalance(TokenBucket.PAID, 2),
        )

    async def overview(self, user_id: int) -> BankOverview:
        """@brief 记录错误的惰性读取调用 / Record an invalid lazy-read invocation.

        @param user_id 用户 ID / User ID.
        @return 永不返回 / Never returns.
        @raise AssertionError Agent 读取走错路径时抛出 / Raised when the Agent uses the wrong path.
        """

        self.lazy_overview_users.append(user_id)
        raise AssertionError("Agent bank_get_overview must not lazily create wallets")


def _request(
    tool_name: str,
    *,
    arguments: JsonObject,
    effect_kind: str,
    mutating: bool,
    is_group: bool = False,
    chat_id: int = 7,
) -> ToolEffectRequest:
    """@brief 构造经过 catalog 形状约束的工具请求 / Build a catalog-shaped tool request.

    @param tool_name 工具名称 / Tool name.
    @param arguments 已验证参数 / Validated arguments.
    @param effect_kind 持久 receipt 副作用类别 / Durable receipt effect kind.
    @param mutating 是否标记为业务 mutation / Whether marked as a business mutation.
    @param is_group 是否模拟群聊 / Whether to simulate a group.
    @param chat_id 当前 chat ID / Current chat ID.
    @return 稳定、私聊 owner 绑定的工具请求 / Stable private-chat owner-bound tool request.
    """

    return ToolEffectRequest(
        context=ToolExecutionContext(
            turn_id=TurnId(UUID("00000000-0000-0000-0000-000000000007")),
            conversation_id=ConversationId("assistant-user:7"),
            delivery_stream_id=DeliveryStreamId("telegram:primary:chat:7:thread:0"),
            user_id=7,
            chat_id=chat_id,
            is_group=is_group,
            group_id=-100 if is_group else None,
            message_id=99,
        ),
        invocation_id="step:0:call:0",
        provider_call_id="provider-call",
        tool_name=tool_name,
        effect_kind=effect_kind,
        mutating=mutating,
        arguments=arguments,
        request_hash="a" * 64,
    )


def test_agent_overview_is_a_genuine_read() -> None:
    """@brief Agent 余额查询不触发旧的惰性钱包初始化 / Agent overview does not trigger legacy lazy wallet initialization."""

    async def scenario() -> None:
        """@brief 执行纯读取场景 / Run the pure-read scenario.

        @return None / None.
        """

        bank = _Bank()
        operation = AssistantBankToolOperation(bank=cast(BankService, bank), now=lambda: _NOW)

        result = await operation.execute(
            _request(
                "bank_get_overview",
                arguments={},
                effect_kind="read.bank_get_overview",
                mutating=False,
            )
        )

        assert result == {
            "code": "success",
            "overview": {"user_id": 7, "free": 5, "paid": 2, "total": 7},
        }
        assert bank.read_overview_users == [7]
        assert bank.lazy_overview_users == []

    asyncio.run(scenario())


def test_agent_pending_request_uses_a_stable_bank_identity() -> None:
    """@brief 同一 Agent 调用重放使用同一银行请求 ID 与幂等键 / Replay of one Agent invocation reuses one bank request ID and idempotency key."""

    async def scenario() -> None:
        """@brief 执行稳定申请 ID 场景 / Run the stable application-identity scenario.

        @return None / None.
        """

        bank = _Bank()
        operation = AssistantBankToolOperation(bank=cast(BankService, bank), now=lambda: _NOW)
        request = _request(
            "bank_request_tokens",
            arguments={"amount": 12, "purpose": "完成活动任务"},
            effect_kind="bank.request_tokens",
            mutating=True,
        )

        first = await operation.execute(request)
        replay = await operation.execute(request)

        assert first["status"] == "pending_review"
        assert replay["status"] == "pending_review"
        assert len(bank.requests) == 2
        assert bank.requests[0].request_id == bank.requests[1].request_id
        assert bank.requests[0].idempotency_key == bank.requests[1].idempotency_key
        assert bank.requests[0].idempotency_key.startswith("assistant:bank-request:")

    asyncio.run(scenario())


def test_agent_bank_tools_reject_group_context_before_calling_bank() -> None:
    """@brief Agent 银行工具在群聊中不读取也不申请 / Agent bank tools neither read nor request in groups."""

    async def scenario() -> None:
        """@brief 执行群聊拒绝场景 / Run the group-rejection scenario.

        @return None / None.
        """

        bank = _Bank()
        operation = AssistantBankToolOperation(bank=cast(BankService, bank), now=lambda: _NOW)

        result = await operation.execute(
            _request(
                "bank_request_tokens",
                arguments={"amount": 12, "purpose": "不应写入"},
                effect_kind="bank.request_tokens",
                mutating=True,
                is_group=True,
                chat_id=-100,
            )
        )

        assert result["reason"] == "private_chat_required"
        assert bank.requests == []
        assert bank.read_overview_users == []

    asyncio.run(scenario())
