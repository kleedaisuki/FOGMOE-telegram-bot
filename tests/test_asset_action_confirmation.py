"""@brief Agent 资产确认状态机测试 / Tests for the Agent asset-confirmation state machine."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.asset_actions.callbacks import AssetActionCallbackData
from fogmoe_bot.application.asset_actions.models import (
    AssetActionDecisionCode,
    AssetActionDecisionCommand,
    AssetActionExecutionClaim,
)
from fogmoe_bot.application.asset_actions.recovery_worker import (
    AssetActionRecoveryWorker,
)
from fogmoe_bot.application.asset_actions.service import AssetActionConfirmationService
from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.domain.asset_actions.confirmation import (
    AssetActionConfirmation,
    AssetActionDecision,
    AssetActionKind,
    AssetActionStatus,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.conversation.outbox import OutboundDraft
from fogmoe_bot.infrastructure.assistant.tool_operations.asset_actions.proposals import (
    AssistantAssetActionProposalOperation,
)


NOW = datetime(2026, 7, 14, 9, tzinfo=timezone.utc)
"""@brief 测试用 UTC 基准时刻 / UTC reference time for tests."""


def _pending_confirmation(
    *,
    confirmation_id: UUID | None = None,
    owner_user_id: int = 7,
) -> AssetActionConfirmation:
    """@brief 构造 owner 私聊绑定的 pending 确认 / Build an owner-private-chat-bound pending confirmation.

    @param confirmation_id 可选稳定确认 ID / Optional stable confirmation ID.
    @param owner_user_id owner Telegram ID / Owner Telegram ID.
    @return 有效 pending 确认 / Valid pending confirmation.
    """

    return AssetActionConfirmation(
        confirmation_id=confirmation_id or uuid4(),
        source_key="asset-action:test:1",
        kind=AssetActionKind.BANK_ISSUE_TOKENS,
        owner_user_id=owner_user_id,
        chat_id=owner_user_id,
        conversation_id=f"assistant-user:{owner_user_id}",
        delivery_stream_id=f"telegram:chat:{owner_user_id}",
        arguments={"recipient_id": 9, "amount": 12, "purpose": "test"},
        status=AssetActionStatus.PENDING,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        updated_at=NOW,
    )


def _executing_claim() -> AssetActionExecutionClaim:
    """@brief 构造带有效 fencing token 的执行 claim / Build an execution claim with a valid fencing token.

    @return 有效执行 claim / Valid execution claim.
    """

    token = uuid4()
    confirmation = replace(
        _pending_confirmation(),
        status=AssetActionStatus.EXECUTING,
        execution_token=token,
        execution_lease_expires_at=NOW + timedelta(minutes=2),
        execution_attempts=1,
        updated_at=NOW + timedelta(seconds=1),
    )
    return AssetActionExecutionClaim(confirmation=confirmation, token=token)


def _executed_confirmation(claim: AssetActionExecutionClaim) -> AssetActionConfirmation:
    """@brief 从执行 claim 构造终态确认 / Build a terminal confirmation from an execution claim.

    @param claim 已领取的执行权 / Claimed execution authority.
    @return 有效 executed 确认 / Valid executed confirmation.
    """

    return replace(
        claim.confirmation,
        status=AssetActionStatus.EXECUTED,
        execution_token=None,
        execution_lease_expires_at=None,
        result={"code": "success"},
        executed_at=NOW + timedelta(seconds=2),
        updated_at=NOW + timedelta(seconds=2),
    )


class _Store:
    """@brief 记录确认服务状态转换的 store 替身 / Store double recording confirmation-service transitions."""

    def __init__(
        self,
        transition: AssetActionExecutionClaim,
        *,
        recoverable: tuple[AssetActionExecutionClaim, ...] = (),
    ) -> None:
        """@brief 初始化固定 transition 与恢复队列 / Initialize a fixed transition and recovery queue.

        @param transition ``begin_decision`` 的返回 claim / Claim returned by ``begin_decision``.
        @param recoverable 恢复轮应返回的 claim / Claims returned by a recovery poll.
        @return None / None.
        """

        self.transition = transition
        self.recoverable = recoverable
        self.commands: list[AssetActionDecisionCommand] = []
        self.finalized: list[tuple[AssetActionExecutionClaim, dict[str, object], str]] = []
        self.recovery_calls: list[tuple[datetime, timedelta, int]] = []

    async def begin_decision(
        self,
        command: AssetActionDecisionCommand,
        *,
        lease_for: timedelta,
    ) -> AssetActionExecutionClaim:
        """@brief 记录 owner 决定并返回 claim / Record an owner decision and return a claim.

        @param command owner 决定 / Owner decision.
        @param lease_for 未使用租约 / Unused lease duration.
        @return 固定 claim / Fixed claim.
        """

        del lease_for
        self.commands.append(command)
        return self.transition

    async def finalize_execution(
        self,
        claim: AssetActionExecutionClaim,
        *,
        result: dict[str, object],
        completed_at: datetime,
        outcome_text: str,
    ) -> AssetActionConfirmation:
        """@brief 记录原子终结请求 / Record an atomic finalization request.

        @param claim 当前 claim / Current claim.
        @param result 业务结果 / Business result.
        @param completed_at 完成时间 / Completion time.
        @param outcome_text 用户通知文本 / User notification text.
        @return 已执行确认 / Executed confirmation.
        """

        del completed_at
        self.finalized.append((claim, result, outcome_text))
        return _executed_confirmation(claim)

    async def claim_expired_executions(
        self,
        *,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> tuple[AssetActionExecutionClaim, ...]:
        """@brief 记录恢复领取并返回固定 claims / Record recovery claim and return fixed claims.

        @param now 恢复时刻 / Recovery time.
        @param lease_for 新租约 / New lease.
        @param limit 批量上限 / Batch limit.
        @return 固定恢复 claims / Fixed recovery claims.
        """

        self.recovery_calls.append((now, lease_for, limit))
        return self.recoverable


class _Executor:
    """@brief 记录 confirmation-derived 幂等键的执行器替身 / Executor double recording confirmation-derived idempotency keys."""

    def __init__(self) -> None:
        """@brief 初始化调用记录 / Initialize call records.

        @return None / None.
        """

        self.calls: list[tuple[AssetActionConfirmation, str, datetime]] = []

    async def execute(
        self,
        confirmation: AssetActionConfirmation,
        *,
        idempotency_key: str,
        executed_at: datetime,
    ) -> dict[str, object]:
        """@brief 记录执行并返回成功 JSON / Record execution and return successful JSON.

        @param confirmation 已确认聚合 / Confirmed aggregate.
        @param idempotency_key 目标幂等键 / Target idempotency key.
        @param executed_at 执行时刻 / Execution time.
        @return 成功结果 / Successful result.
        """

        self.calls.append((confirmation, idempotency_key, executed_at))
        return {"code": "success"}


def test_callback_codec_is_compact_and_rejects_tampering() -> None:
    """@brief callback 仅含短 opaque ID，且严格拒绝篡改 / Callback contains only a short opaque ID and rejects tampering."""

    confirmation_id = uuid4()
    encoded = AssetActionCallbackData(
        confirmation_id=confirmation_id,
        decision=AssetActionDecision.APPROVE,
    ).encode()

    assert len(encoded.encode("utf-8")) <= 64
    assert AssetActionCallbackData.decode(encoded).confirmation_id == confirmation_id
    with pytest.raises(ValueError):
        AssetActionCallbackData.decode("asset_confirm:a:not-a-uuid")


def test_confirmation_requires_owner_private_chat_identity() -> None:
    """@brief 确认聚合拒绝负 chat 和 owner/chat 不匹配 / Confirmation aggregate rejects non-private or mismatched owner/chat identity."""

    with pytest.raises(ValueError, match="private-chat"):
        replace(_pending_confirmation(), chat_id=-7)
    with pytest.raises(ValueError, match="bind Telegram private chat"):
        replace(_pending_confirmation(), chat_id=8)


def test_decision_executes_with_confirmation_derived_idempotency_key() -> None:
    """@brief 真实执行只使用 confirmation ID 派生的稳定幂等键 / Real execution uses only the stable confirmation-ID-derived key."""

    async def scenario() -> None:
        """@brief 运行 owner 批准场景 / Run the owner-approval scenario.

        @return None / None.
        """

        claim = _executing_claim()
        store = _Store(claim)
        executor = _Executor()
        service = AssetActionConfirmationService(store=cast(object, store), executor=cast(object, executor))

        result = await service.decide(
            AssetActionDecisionCommand(
                confirmation_id=claim.confirmation.confirmation_id,
                decision=AssetActionDecision.APPROVE,
                actor_user_id=7,
                chat_id=7,
                update_id=42,
                decided_at=NOW,
            )
        )

        assert result.code is AssetActionDecisionCode.EXECUTED
        assert executor.calls[0][1] == (
            f"asset-confirmation:{claim.confirmation.confirmation_id}"
        )
        assert store.finalized[0][2].startswith("已完成已确认的资产操作")

    asyncio.run(scenario())


def test_recovery_only_uses_claimed_expired_executions() -> None:
    """@brief 恢复服务只消费 store 显式领取的 executing lease / Recovery service consumes only executing leases explicitly claimed by the store."""

    async def scenario() -> None:
        """@brief 运行恢复场景 / Run the recovery scenario.

        @return None / None.
        """

        claim = _executing_claim()
        store = _Store(claim, recoverable=(claim,))
        executor = _Executor()
        service = AssetActionConfirmationService(store=cast(object, store), executor=cast(object, executor))

        completed = await service.recover_expired(now=NOW + timedelta(minutes=3), limit=8)

        assert completed == 1
        assert store.recovery_calls[0][2] == 8
        assert executor.calls[0][1] == (
            f"asset-confirmation:{claim.confirmation.confirmation_id}"
        )

    asyncio.run(scenario())


class _ProposalStore:
    """@brief 记录 Agent proposal 命令的 store 替身 / Store double recording Agent proposal commands."""

    def __init__(self) -> None:
        """@brief 初始化空记录 / Initialize empty records.

        @return None / None.
        """

        self.commands: list[object] = []

    async def propose_in_transaction(
        self,
        command: object,
        *,
        connection: AsyncConnection,
    ) -> AssetActionConfirmation:
        """@brief 记录命令并构造 pending 确认 / Record a command and build a pending confirmation.

        @param command 提议命令 / Proposal command.
        @param connection 调用方事务 / Caller transaction.
        @return pending 确认 / Pending confirmation.
        """

        del connection
        self.commands.append(command)
        proposed = cast(object, command)
        return AssetActionConfirmation(
            confirmation_id=cast(object, proposed).confirmation_id,
            source_key=cast(object, proposed).source_key,
            kind=cast(object, proposed).kind,
            owner_user_id=cast(object, proposed).owner_user_id,
            chat_id=cast(object, proposed).chat_id,
            conversation_id=cast(object, proposed).conversation_id,
            delivery_stream_id=cast(object, proposed).delivery_stream_id,
            arguments=cast(object, proposed).arguments,
            status=AssetActionStatus.PENDING,
            created_at=cast(object, proposed).created_at,
            expires_at=cast(object, proposed).expires_at,
            updated_at=cast(object, proposed).created_at,
        )


class _Outbound:
    """@brief 记录 confirmation prompt outbox 草稿 / Outbox double recording confirmation-prompt drafts."""

    def __init__(self) -> None:
        """@brief 初始化空草稿列表 / Initialize an empty draft list.

        @return None / None.
        """

        self.drafts: list[OutboundDraft] = []

    async def enqueue_standalone_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
    ) -> None:
        """@brief 捕获同事务 outbox 草稿 / Capture a same-transaction outbox draft.

        @param connection 调用方事务 / Caller transaction.
        @param draft 资产确认草稿 / Asset-confirmation draft.
        @return None / None.
        """

        del connection
        self.drafts.append(draft)


def _request(*, is_group: bool = False, chat_id: int = 7) -> ToolEffectRequest:
    """@brief 构造已校验 review proposal 工具请求 / Build a validated review-proposal tool request.

    @param is_group 是否模拟群聊 / Whether to simulate a group chat.
    @param chat_id 模拟 chat ID / Simulated chat ID.
    @return 资产确认 proposal 请求 / Asset-confirmation proposal request.
    """

    context = ToolExecutionContext(
        turn_id=TurnId(UUID("00000000-0000-0000-0000-000000000001")),
        conversation_id=ConversationId("assistant-user:7"),
        delivery_stream_id=DeliveryStreamId("telegram:chat:7"),
        user_id=7,
        chat_id=chat_id,
        is_group=is_group,
        group_id=-100 if is_group else None,
        message_id=12,
    )
    return ToolEffectRequest(
        context=context,
        invocation_id="step:0:call:0",
        provider_call_id="provider-call",
        tool_name="bank_review_token_request",
        effect_kind="asset.propose.bank.review_token_request",
        mutating=True,
        arguments={
            "request_id": "00000000-0000-0000-0000-000000000999",
            "decision": "approve",
        },
        request_hash="f" * 64,
    )


def test_proposal_binds_owner_to_context_and_writes_durable_prompt() -> None:
    """@brief proposal 从上下文绑定 owner，并以同一 receipt 事务写 prompt / Proposal binds owner from context and writes the prompt in the receipt transaction."""

    async def scenario() -> None:
        """@brief 运行私聊管理员 proposal 场景 / Run a private administrator-proposal scenario.

        @return None / None.
        """

        store = _ProposalStore()
        outbound = _Outbound()
        operation = AssistantAssetActionProposalOperation(
            store=cast(object, store),
            administrator_id=7,
            now=lambda: NOW,
        )
        request = _request()
        connection = cast(AsyncConnection, object())

        result = await operation.execute(request, connection=connection)
        await operation.finalize(
            request,
            result,
            connection=connection,
            outbox=cast(object, outbound),
        )

        assert result["status"] == "confirmation_required"
        assert len(store.commands) == 1
        assert len(outbound.drafts) == 1
        draft = outbound.drafts[0]
        assert draft.payload["chat_id"] == 7
        assert draft.kind.value == "telegram.send_asset_confirmation"
        assert len(str(draft.payload["approve_callback_data"]).encode("utf-8")) <= 64

    asyncio.run(scenario())


def test_proposal_rejects_group_or_non_owner_private_chat_before_storage() -> None:
    """@brief proposal 在存储前拒绝群聊和 chat/owner 不匹配 / Proposal rejects groups and mismatched chat/owner before storage."""

    async def scenario() -> None:
        """@brief 运行无存储拒绝场景 / Run the no-storage rejection scenario.

        @return None / None.
        """

        store = _ProposalStore()
        operation = AssistantAssetActionProposalOperation(
            store=cast(object, store),
            administrator_id=7,
            now=lambda: NOW,
        )
        connection = cast(AsyncConnection, object())

        group_result = await operation.execute(_request(is_group=True), connection=connection)
        mismatch_result = await operation.execute(_request(chat_id=8), connection=connection)

        assert group_result["reason"] == "private_chat_required"
        assert mismatch_result["reason"] == "private_chat_identity_invalid"
        assert store.commands == []

    asyncio.run(scenario())


def test_recovery_worker_validates_small_batch_bounds() -> None:
    """@brief 恢复 worker 强制小批量上限 / Recovery worker enforces a small batch bound."""

    with pytest.raises(ValueError, match="batch_size"):
        AssetActionRecoveryWorker(service=cast(object, object()), batch_size=101)
