"""@brief 资产动作确认应用端口 / Application ports for asset-action confirmation."""

from __future__ import annotations

from datetime import datetime, timedelta
from collections.abc import Sequence
from typing import Any, Protocol

from fogmoe_bot.application.asset_actions.models import (
    AssetActionDecisionCommand,
    AssetActionDecisionResult,
    AssetActionExecutionClaim,
    ProposeAssetAction,
)
from fogmoe_bot.domain.asset_actions.confirmation import AssetActionConfirmation
from fogmoe_bot.domain.conversation.payloads import JsonObject


type AtomicTransaction = Any
"""@brief 由外层基础设施持有的不可解释事务句柄 / Opaque transaction handle owned by outer infrastructure.

应用层只把该句柄原样交给同一事务 port，绝不导入或调用数据库驱动 API。/
The application layer merely passes this handle to the same-transaction port and never imports or
calls a database-driver API.
"""


class AssetActionConfirmationStore(Protocol):
    """@brief 资产确认聚合及其 fenced 执行状态的持久化端口 / Persistence port for asset confirmations and their fenced execution state."""

    async def propose_in_transaction(
        self,
        command: ProposeAssetAction,
        *,
        connection: AtomicTransaction,
    ) -> AssetActionConfirmation:
        """@brief 在调用方事务内创建或重放提议 / Create or replay a proposal in the caller transaction.

        @param command 类型化提议 / Typed proposal.
        @param connection 调用方拥有的活动短事务 / Active short transaction owned by the caller.
        @return 规范确认聚合 / Canonical confirmation aggregate.
        """

        ...

    async def begin_decision(
        self,
        command: AssetActionDecisionCommand,
        *,
        lease_for: timedelta,
    ) -> AssetActionExecutionClaim | AssetActionDecisionResult:
        """@brief 原子验证 owner、过期和状态，并可领取执行 / Atomically validate owner, expiry, and state, optionally claiming execution.

        @param command 类型化 owner 选择 / Typed owner choice.
        @param lease_for 允许恢复的执行租约时长 / Execution-lease duration allowing recovery.
        @return fenced 执行 claim 或无需执行的结果 / Fenced execution claim or a no-execution result.
        """

        ...

    async def finalize_execution(
        self,
        claim: AssetActionExecutionClaim,
        *,
        result: JsonObject,
        completed_at: datetime,
        outcome_text: str,
    ) -> AssetActionConfirmation:
        """@brief 原子持久化业务结果及其 Telegram outbox 通知 / Atomically persist business result and its Telegram outbox notification.

        @param claim 当前 fenced 执行 claim / Current fenced execution claim.
        @param result 已执行的 JSON 业务结果 / Executed JSON business result.
        @param completed_at 完成时刻 / Completion time.
        @param outcome_text 用户可见终态说明 / User-visible terminal explanation.
        @return 已完成的规范确认记录 / Canonical completed confirmation.
        @note 实现必须以 claim token fencing，并和确定性 outbox 同事务提交。/
            Implementations must fence by claim token and commit with deterministic outbox in one transaction.
        """

        ...

    async def claim_expired_executions(
        self,
        *,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> Sequence[AssetActionExecutionClaim]:
        """@brief 领取已过期的 approved 执行租约以恢复 / Claim expired approved-execution leases for recovery.

        @param now 可信 UTC 当前时刻 / Trusted current UTC time.
        @param lease_for 新 fencing 租约时长 / New fencing-lease duration.
        @param limit 此短事务最多领取的记录数 / Maximum records claimed by this short transaction.
        @return 仅原本 ``executing`` 且租约过期的 fenced claims /
            Fenced claims that were already ``executing`` and whose leases expired.
        @note 实现必须使用跳过已锁行的行级领取或等价机制；不得扫描、领取
            pending/cancelled/expired 记录。/ Implementations must use a row-level
            skip-locked claim or equivalent and must never scan or claim
            pending/cancelled/expired records.
        """

        ...


class AssetActionExecutor(Protocol):
    """@brief 将已确认动作映射为目标 bounded context 操作 / Map an approved action to a target bounded-context operation."""

    async def execute(
        self,
        confirmation: AssetActionConfirmation,
        *,
        idempotency_key: str,
        executed_at: datetime,
    ) -> JsonObject:
        """@brief 执行或重放一个已批准动作 / Execute or replay one approved action.

        @param confirmation 已获 owner 同意的规范聚合 / Canonical aggregate approved by its owner.
        @param idempotency_key 确认 ID 派生的稳定目标幂等键 / Stable target idempotency key derived from confirmation ID.
        @param executed_at 执行时刻 / Execution time.
        @return JSON 可持久化业务结果 / JSON-persistable business result.
        """

        ...


__all__ = [
    "AssetActionConfirmationStore",
    "AssetActionExecutor",
    "AtomicTransaction",
]
