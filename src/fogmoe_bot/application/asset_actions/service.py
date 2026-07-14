"""@brief 资产动作确认的恢复型应用服务 / Recoverable application service for asset-action confirmation."""

from __future__ import annotations

from datetime import datetime, timedelta

from fogmoe_bot.application.asset_actions.models import (
    AssetActionDecisionCode,
    AssetActionDecisionCommand,
    AssetActionDecisionResult,
    AssetActionExecutionClaim,
)
from fogmoe_bot.application.asset_actions.ports import (
    AssetActionConfirmationStore,
    AssetActionExecutor,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.temporal import ensure_utc


ASSET_ACTION_CONFIRMATION_SERVICE_DATA_KEY = "asset_actions.confirmation_service"
"""@brief Telegram runtime 中确认服务的稳定 capability 键 / Stable confirmation-service capability key in Telegram runtime."""


class AssetActionConfirmationService:
    """@brief 将确认 callback 编排为短事务 + 租约外执行 / Orchestrate confirmation callbacks as short transactions plus lease-outside execution.

    该服务故意不在数据库锁内调用银行。先以 fencing token 领取执行权，再在锁外调用
    目标 bounded context；若进程在结果落库前中断，租约届满后会用同一目标幂等键恢复。/
    This service deliberately never calls the bank under a database lock. It first claims execution
    with a fencing token, invokes the target bounded context outside the lock, and recovers after a
    crash with the same target idempotency key once the lease expires.
    """

    def __init__(
        self,
        *,
        store: AssetActionConfirmationStore,
        executor: AssetActionExecutor,
        execution_lease: timedelta = timedelta(minutes=2),
    ) -> None:
        """@brief 注入状态存储、执行器与恢复租约 / Inject state store, executor, and recovery lease.

        @param store 确认状态与原子 outbox 存储 / Confirmation-state and atomic-outbox store.
        @param executor 已同意动作的目标执行器 / Target executor for approved actions.
        @param execution_lease 单次执行的最大独占时长 / Maximum exclusive duration of one execution.
        @return None / None.
        """

        if execution_lease <= timedelta():
            raise ValueError("Asset-action execution_lease must be positive")
        self._store = store
        self._executor = executor
        self._execution_lease = execution_lease

    async def decide(
        self,
        command: AssetActionDecisionCommand,
    ) -> AssetActionDecisionResult:
        """@brief 验证 owner 的选择，并在批准时执行一次 / Validate an owner's choice and execute once when approved.

        @param command 从 durable Telegram callback 重建的选择 / Choice reconstructed from a durable Telegram callback.
        @return 终态、处理中或拒绝结果 / Terminal, processing, or rejection result.
        @raise Exception 目标执行临时失败时抛出，由 durable inbox 重试 / Raised for transient target-execution failures so the durable inbox retries.
        """

        transition = await self._store.begin_decision(
            command,
            lease_for=self._execution_lease,
        )
        if isinstance(transition, AssetActionDecisionResult):
            return transition
        if not isinstance(transition, AssetActionExecutionClaim):
            raise AssertionError("Unhandled asset-action decision transition")
        return await self._execute_claim(transition, decided_at=command.decided_at)

    async def recover_expired(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> int:
        """@brief 恢复已批准但执行租约过期的资产动作 / Recover approved asset actions whose execution leases expired.

        @param now 可信 UTC 当前时刻 / Trusted current UTC time.
        @param limit 每轮最多恢复的执行数 / Maximum executions recovered in one poll.
        @return 成功持久化终态的恢复数 / Number of recoveries whose terminal state was persisted.
        @raise Exception 银行或结果持久化暂时失败时抛出，租约会在下轮再次恢复 /
            Raised for transient bank or result-persistence failures; the lease will be recovered again in a later poll.
        @note 此方法绝不扫描 pending/cancelled/expired 确认，也不占用 Agent mailbox；它只
            消费已同意且过期的 ``executing`` 租约。/ This method never scans pending,
            cancelled, or expired confirmations and never occupies an Agent mailbox; it consumes
            only approved ``executing`` leases that expired.
        """

        recovered_at = ensure_utc(now)
        claims = await self._store.claim_expired_executions(
            now=recovered_at,
            lease_for=self._execution_lease,
            limit=limit,
        )
        completed = 0
        for claim in claims:
            await self._execute_claim(claim, decided_at=recovered_at)
            completed += 1
        return completed

    async def _execute_claim(
        self,
        claim: AssetActionExecutionClaim,
        *,
        decided_at: datetime,
    ) -> AssetActionDecisionResult:
        """@brief 在租约外执行并原子终结一份 claim / Execute outside the lease transaction and atomically finish a claim.

        @param claim 当前 fenced 执行权 / Current fenced execution authority.
        @param decided_at callback 的可信时刻 / Trusted callback time.
        @return 已执行的终态结果 / Executed terminal result.
        """

        executed_at = ensure_utc(decided_at)
        confirmation = claim.confirmation
        result = await self._executor.execute(
            confirmation,
            idempotency_key=_execution_idempotency_key(confirmation.confirmation_id),
            executed_at=executed_at,
        )
        completed = await self._store.finalize_execution(
            claim,
            result=result,
            completed_at=executed_at,
            outcome_text=_outcome_text(confirmation.kind.value, result),
        )
        return AssetActionDecisionResult(
            code=AssetActionDecisionCode.EXECUTED,
            confirmation=completed,
            result=result,
        )


def _execution_idempotency_key(confirmation_id: object) -> str:
    """@brief 从确认 ID 推导目标 bounded context 幂等键 / Derive the target bounded-context idempotency key from a confirmation ID.

    @param confirmation_id 已验证确认 ID / Validated confirmation identifier.
    @return 低于银行长度上限的稳定键 / Stable key below the banking length limit.
    """

    return f"asset-confirmation:{confirmation_id}"


def _outcome_text(kind: str, result: JsonObject) -> str:
    """@brief 生成不会重新解释模型文本的终态通知 / Produce a terminal notification without reinterpreting model text.

    @param kind 已持久化动作类别 / Persisted action kind.
    @param result 已持久化业务结果 / Persisted business result.
    @return 用户可见且可安全重放的中文说明 / User-visible Chinese explanation safe to replay.
    """

    code = result.get("code")
    if code == "success":
        return f"已完成已确认的资产操作：{kind}。"
    if code == "forbidden":
        return f"资产操作未执行：{kind} 的管理员授权在确认时已不满足。"
    if code == "not_registered":
        return f"资产操作未执行：相关账户尚未注册（{kind}）。"
    if code == "not_found":
        return f"资产操作未执行：目标记录不存在（{kind}）。"
    if code == "not_pending":
        return f"资产操作未执行：目标申请已不再等待审核（{kind}）。"
    if code == "insufficient_funds":
        return f"资产操作未执行：可用余额不足（{kind}）。"
    return f"已完成已确认的资产操作：{kind}；请使用 /bank 查看最新状态。"


__all__ = [
    "ASSET_ACTION_CONFIRMATION_SERVICE_DATA_KEY",
    "AssetActionConfirmationService",
]
