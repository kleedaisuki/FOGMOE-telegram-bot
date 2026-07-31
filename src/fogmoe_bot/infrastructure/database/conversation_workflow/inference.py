"""@brief PostgreSQL inference-activity adapter / PostgreSQL inference-activity adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.conversation.errors import (
    ConcurrentTurnUpdateError,
    StaleClaimError,
    TurnNotFoundError,
)
from fogmoe_bot.domain.conversation.identity import (
    InferenceActivityId,
    LeaseToken,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityClaim,
    InferenceActivityLease,
    InferenceActivityStatus,
    InferenceFailedFinal,
    InferenceFailure,
    InferenceRetryScheduled,
    InferenceSucceeded,
)
from fogmoe_bot.domain.conversation.message import (
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundDraft,
    OutboundEnqueueResult,
)
from fogmoe_bot.domain.conversation.turn import (
    POST_INFERENCE_COMPLETION_TURN_STATES,
    ConversationTurn,
    TurnEvent,
    TurnState,
)
from fogmoe_bot.domain.conversation.workflow_results import (
    InferenceCompletionResult,
    InferenceFailureDeliveryResult,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.infrastructure.database import db

from .common import (
    _INFERENCE_ACTIVITY_COLUMNS,
    _INFERENCE_ACTIVITY_SELECT,
    _claim_window,
    _datetime,
    _map_inference_activity,
    _optional_datetime,
    _row_values,
    _uuid,
    _validate_inference_activity_idempotency,
)
from .outbox import PostgresOutboxRepository
from .turn_uow import (
    _append_message,
    _load_turn_for_mutation,
    _persist_turn,
    _require_existing_message,
    _validate_message_for_turn,
)


def _validate_claim_identity(
    current: InferenceActivity,
    claim: InferenceActivityClaim,
) -> None:
    """@brief 验证 claim 的不可变活动语义与 input revision / Validate immutable activity semantics and input revision carried by a claim.

    @param current 数据库当前活动 / Current database activity.
    @param claim worker 持有的 claim / Claim held by the worker.
    @return None / None.
    @raise StaleClaimError steer 已提升 revision 时抛出 / Raised when a steer has advanced the revision.
    """

    _validate_inference_activity_idempotency(current, claim.activity)
    if current.input_revision != claim.activity.input_revision:
        raise StaleClaimError(f"Stale inference revision for {current.activity_id}")
    if current.retry_budget_used != claim.activity.retry_budget_used:
        raise StaleClaimError(f"Stale inference retry budget for {current.activity_id}")


def _validate_outbound_for_turn(
    turn: ConversationTurn,
    draft: OutboundDraft,
) -> None:
    """@brief 验证 completion 出站副作用所有权 / Validate completion outbound-effect ownership."""

    if draft.turn_id != turn.turn_id:
        raise ValueError("Composite outbound effect must belong to the target turn")
    if draft.conversation_id != turn.conversation_id:
        raise ValueError(
            "Composite outbound effect must belong to the target conversation"
        )


class InferenceOutboxWriter(Protocol):
    """@brief inference completion 所需的同事务 outbox writer / Same-transaction outbox writer required by inference completion."""

    async def enqueue_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 原子写入 Turn-owned outbound / Atomically enqueue a Turn-owned outbound."""

        ...

    async def require_existing_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
        *,
        operation: str,
    ) -> OutboundEnqueueResult:
        """@brief 读取已提交组合操作的 canonical outbound / Load a committed composite operation's canonical outbound."""

        ...


class PostgresInferenceRepository:
    """@brief 拥有 inference claim、重试与原子 completion / Own inference claims, retries, and atomic completion."""

    def __init__(
        self,
        outbox: InferenceOutboxWriter | None = None,
    ) -> None:
        """@brief 注入同事务 outbox writer / Inject the same-transaction outbox writer.

        @param outbox completion 所需的同事务 outbox writer / Same-transaction outbox writer required by completion.
        """

        self._outbox = outbox or PostgresOutboxRepository()

    async def get_inference_activity(
        self,
        activity_id: InferenceActivityId,
    ) -> InferenceActivity | None:
        """@brief 读取推理活动快照 / Load an inference-activity snapshot.

        @param activity_id 活动 ID / Activity identifier.
        @return 活动或 None / Activity or None.
        """

        row = await db.fetch_one(
            _INFERENCE_ACTIVITY_SELECT + " WHERE activity_id = CAST(%s AS UUID)",
            (str(activity_id),),
        )
        return _map_inference_activity(row) if row is not None else None

    async def claim_inference_activities(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[InferenceActivityClaim, ...]:
        """@brief 以 SKIP LOCKED 领取推理活动 / Claim inference activities with SKIP LOCKED.

        @param now 当前 UTC 时间 / Current UTC time.
        @param limit 最大领取数 / Maximum number of claims.
        @param lease_for fencing 租约时长 / Fencing lease duration.
        @return 带 token 的 claims / Claims carrying tokens.
        @note retry claim 与 Turn 的 RETRY_INFERENCE 转移位于同一事务。/
        Claiming a retry and applying the Turn RETRY_INFERENCE transition share one transaction.
        """

        timestamp, lease_expires_at = _claim_window(now, limit, lease_for)
        if limit < 1:
            return ()
        token = LeaseToken.new()
        async with db.transaction() as connection:
            rows = await db.fetch_all(
                "SELECT " + _INFERENCE_ACTIVITY_COLUMNS + " "
                "FROM conversation.inference_activities AS candidate "
                "WHERE candidate.status IN ('pending', 'steer_pending', 'retry') "
                "AND candidate.next_attempt_at <= %s AND NOT EXISTS ("
                "SELECT 1 FROM conversation.inference_activities AS earlier "
                "WHERE earlier.conversation_id = candidate.conversation_id "
                "AND earlier.status IN ('pending', 'processing', 'steer_pending', 'retry') "
                "AND (earlier.created_at, earlier.activity_id) "
                "< (candidate.created_at, candidate.activity_id)"
                ") ORDER BY candidate.next_attempt_at ASC, candidate.activity_id ASC LIMIT %s "
                "FOR UPDATE OF candidate SKIP LOCKED",
                (timestamp, limit),
                connection=connection,
            )
            claims: list[InferenceActivityClaim] = []
            for row in rows:
                previous = _map_inference_activity(row)
                claim = previous.claim(
                    token=token,
                    claimed_at=timestamp,
                    lease_expires_at=lease_expires_at,
                )
                target = claim.activity
                activity_row = await db.fetch_one(
                    "UPDATE conversation.inference_activities "
                    "SET status = %s, version = %s, attempt_count = %s, "
                    "retry_budget_used = %s, next_attempt_at = %s, "
                    "claim_token = CAST(%s AS UUID), lease_expires_at = %s, "
                    "completion_token = NULL, completed_at = NULL, updated_at = %s, "
                    "last_error = %s, input_revision = %s "
                    "WHERE activity_id = CAST(%s AS UUID) AND status = %s "
                    "AND version = %s AND attempt_count = %s "
                    "AND retry_budget_used = %s AND input_revision = %s "
                    "AND next_attempt_at IS NOT DISTINCT FROM %s "
                    "AND updated_at = %s AND claim_token IS NULL "
                    "AND lease_expires_at IS NULL RETURNING "
                    + _INFERENCE_ACTIVITY_COLUMNS,
                    (
                        target.status.value,
                        target.version,
                        target.attempt_count,
                        target.retry_budget_used,
                        target.next_attempt_at,
                        str(token),
                        lease_expires_at,
                        target.updated_at,
                        target.last_error,
                        int(target.input_revision),
                        str(previous.activity_id),
                        previous.status.value,
                        previous.version,
                        previous.attempt_count,
                        previous.retry_budget_used,
                        int(previous.input_revision),
                        previous.next_attempt_at,
                        previous.updated_at,
                    ),
                    connection=connection,
                )
                if activity_row is None:
                    raise ConcurrentTurnUpdateError(
                        f"Inference activity {previous.activity_id} changed while row-locked"
                    )
                persisted_processing = _map_inference_activity(activity_row)
                if persisted_processing != target:
                    raise RuntimeError(
                        "Inference claim SQL diverged from the domain transition"
                    )
                turn = await _load_turn_for_mutation(
                    persisted_processing.turn_id,
                    connection=connection,
                )
                if previous.status in {
                    InferenceActivityStatus.PENDING,
                    InferenceActivityStatus.STEER_PENDING,
                }:
                    if turn.state is not TurnState.WAITING_INFERENCE:
                        raise ConcurrentTurnUpdateError(
                            f"Pending or steered inference {persisted_processing.activity_id} requires a "
                            f"waiting_inference turn, found {turn.state.value}"
                        )
                elif previous.status is InferenceActivityStatus.RETRY:
                    if turn.state is not TurnState.INFERENCE_RETRY_WAIT:
                        raise ConcurrentTurnUpdateError(
                            f"Retry inference {persisted_processing.activity_id} requires an "
                            f"inference_retry_wait turn, found {turn.state.value}"
                        )
                    resumed = turn.transition(
                        TurnEvent.RETRY_INFERENCE,
                        occurred_at=timestamp,
                    )
                    await _persist_turn(
                        resumed,
                        expected_version=turn.version,
                        connection=connection,
                    )
                else:
                    raise RuntimeError(
                        f"Unsupported inference pre-claim status {previous.status.value}"
                    )
                claims.append(claim)
        return tuple(sorted(claims, key=lambda claim: str(claim.activity.activity_id)))

    async def complete_inference_activity(
        self,
        decision: InferenceSucceeded,
        *,
        assistant_message: MessageDraft,
        outbounds: Sequence[OutboundDraft],
    ) -> InferenceCompletionResult:
        """@brief 以 fencing token 原子提交推理、历史与 outbox / Atomically commit inference, history, and outbox with a fencing token.

        @param decision 已验证成功 settlement / Validated successful settlement.
        @param assistant_message 确定性助手消息 / Deterministic assistant message.
        @param outbounds 有序、确定性的出站副作用 / Ordered deterministic outbound effects.
        @return 原子完成回执 / Atomic completion receipt.
        @raise StaleClaimError claim 已被恢复或替代时抛出 / Raised when the claim was recovered or superseded.
        @note 相同成功 claim 的 post-commit 重放返回规范回执；更老 claim 永远不能覆盖新结果。/
        A post-commit replay of the same successful claim returns canonical receipts; an older claim can never overwrite a newer result.
        """

        claim = decision.claim
        target = decision.activity
        timestamp = target.completed_at
        if timestamp is None:  # pragma: no cover - decision proves completion.
            raise AssertionError("Inference success lost its completion time")
        if not outbounds:
            raise ValueError("Inference completion requires outbound effects")
        if assistant_message.created_at > timestamp or any(
            outbound.created_at > timestamp for outbound in outbounds
        ):
            raise ValueError("Inference effects cannot be created after completed_at")
        async with db.transaction() as connection:
            await db.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(claim.activity.conversation_id),),
                connection=connection,
            )
            (
                current,
                current_claim_token,
                current_lease_expires_at,
            ) = await self._load_inference_activity_for_update(
                claim.activity.activity_id,
                connection=connection,
            )
            _validate_claim_identity(current, claim)
            turn = await _load_turn_for_mutation(
                current.turn_id,
                connection=connection,
            )
            _validate_message_for_turn(
                turn,
                assistant_message,
                expected_role=MessageRole.ASSISTANT,
            )
            for outbound in outbounds:
                _validate_outbound_for_turn(turn, outbound)

            if current.status is InferenceActivityStatus.COMPLETED:
                if (
                    current.completion_token != claim.token
                    or current.version != claim.expected_version + 1
                ):
                    raise StaleClaimError(
                        f"Stale inference claim for {current.activity_id}"
                    )
                if turn.state not in POST_INFERENCE_COMPLETION_TURN_STATES:
                    raise ConcurrentTurnUpdateError(
                        f"Completed inference {current.activity_id} has turn "
                        f"state {turn.state.value}"
                    )
                message_result = await _require_existing_message(
                    assistant_message,
                    operation="inference completion replay",
                    connection=connection,
                )
                outbound_results = tuple(
                    [
                        await self._outbox.require_existing_outbound_in_transaction(
                            connection,
                            outbound,
                            operation="inference completion replay",
                        )
                        for outbound in outbounds
                    ]
                )
                return InferenceCompletionResult(
                    turn=turn,
                    activity=current,
                    assistant_message=message_result,
                    outbounds=outbound_results,
                )

            if (
                current.status is not InferenceActivityStatus.PROCESSING
                or current != claim.activity
                or current_claim_token != claim.token
                or current_lease_expires_at != claim.lease_expires_at
            ):
                raise StaleClaimError(
                    f"Stale inference claim for {current.activity_id}"
                )
            if turn.state is not TurnState.WAITING_INFERENCE:
                raise ConcurrentTurnUpdateError(
                    f"Inference {current.activity_id} requires a waiting_inference "
                    f"turn, found {turn.state.value}"
                )

            message_result = await _append_message(
                assistant_message,
                connection=connection,
            )
            outbound_results = tuple(
                [
                    await self._outbox.enqueue_outbound_in_transaction(
                        connection, outbound
                    )
                    for outbound in outbounds
                ]
            )
            activity_row = await db.fetch_one(
                "UPDATE conversation.inference_activities "
                "SET status = 'completed', version = %s, "
                "next_attempt_at = NULL, claim_token = NULL, lease_expires_at = NULL, "
                "completion_token = CAST(%s AS UUID), completed_at = %s, "
                "updated_at = %s, last_error = NULL "
                "WHERE activity_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND version = %s AND claim_token = CAST(%s AS UUID) "
                "AND input_revision = %s AND retry_budget_used = %s RETURNING "
                + _INFERENCE_ACTIVITY_COLUMNS,
                (
                    target.version,
                    str(claim.token),
                    timestamp,
                    timestamp,
                    str(current.activity_id),
                    claim.expected_version,
                    str(claim.token),
                    int(claim.activity.input_revision),
                    claim.activity.retry_budget_used,
                ),
                connection=connection,
            )
            if activity_row is None:
                raise StaleClaimError(
                    f"Stale inference claim for {current.activity_id}"
                )
            completed_activity = _map_inference_activity(activity_row)
            if completed_activity != target:
                raise RuntimeError(
                    "Inference completion SQL diverged from the domain transition"
                )
            updated_turn = turn.transition(
                TurnEvent.INFERENCE_SUCCEEDED,
                occurred_at=timestamp,
            ).transition(
                TurnEvent.REQUEST_DELIVERY,
                occurred_at=timestamp,
            )
            await _persist_turn(
                updated_turn,
                expected_version=turn.version,
                connection=connection,
            )
            return InferenceCompletionResult(
                turn=updated_turn,
                activity=completed_activity,
                assistant_message=message_result,
                outbounds=outbound_results,
            )

    async def retry_inference_activity(
        self,
        decision: InferenceRetryScheduled,
    ) -> None:
        """@brief 原子安排活动与 Turn 重试 / Atomically schedule the activity and Turn for retry.

        @param decision 已验证重试 settlement / Validated retry settlement.
        @return None / None.
        """

        claim = decision.claim
        target = decision.activity
        retry_time = target.next_attempt_at
        normalized_error = target.last_error
        if retry_time is None or normalized_error is None:
            raise AssertionError("Inference retry settlement lost schedule or failure")
        async with db.transaction() as connection:
            (
                current,
                current_token,
                current_lease,
            ) = await self._load_inference_activity_for_update(
                claim.activity.activity_id,
                connection=connection,
            )
            _validate_claim_identity(current, claim)
            if (
                current.status is not InferenceActivityStatus.PROCESSING
                or current != claim.activity
                or current_token != claim.token
                or current_lease != claim.lease_expires_at
            ):
                raise StaleClaimError(
                    f"Stale inference claim for {current.activity_id}"
                )
            turn = await _load_turn_for_mutation(
                current.turn_id,
                connection=connection,
            )
            if turn.state is not TurnState.WAITING_INFERENCE:
                raise ConcurrentTurnUpdateError(
                    f"Inference retry requires waiting_inference, found {turn.state.value}"
                )
            activity_row = await db.fetch_one(
                "UPDATE conversation.inference_activities "
                "SET status = 'retry', version = %s, next_attempt_at = %s, "
                "claim_token = NULL, lease_expires_at = NULL, completion_token = NULL, "
                "updated_at = %s, last_error = %s, retry_budget_used = %s "
                "WHERE activity_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND version = %s AND claim_token = CAST(%s AS UUID) "
                "AND input_revision = %s AND retry_budget_used = %s RETURNING "
                + _INFERENCE_ACTIVITY_COLUMNS,
                (
                    target.version,
                    retry_time,
                    target.updated_at,
                    normalized_error,
                    target.retry_budget_used,
                    str(current.activity_id),
                    claim.expected_version,
                    str(claim.token),
                    int(claim.activity.input_revision),
                    claim.activity.retry_budget_used,
                ),
                connection=connection,
            )
            if activity_row is None:
                raise StaleClaimError(
                    f"Stale inference claim for {current.activity_id}"
                )
            if _map_inference_activity(activity_row) != target:
                raise RuntimeError(
                    "Inference retry SQL diverged from the domain transition"
                )
            retrying = turn.transition(
                TurnEvent.SCHEDULE_INFERENCE_RETRY,
                occurred_at=target.updated_at,
                retry_at=retry_time,
                error=normalized_error,
            )
            await _persist_turn(
                retrying,
                expected_version=turn.version,
                connection=connection,
            )

    async def fail_inference_activity(
        self,
        decision: InferenceFailedFinal,
        *,
        assistant_message: MessageDraft,
        outbounds: Sequence[OutboundDraft],
    ) -> InferenceFailureDeliveryResult:
        """@brief 原子终结活动并持久化安全失败上下文/outbox / Atomically fail the activity and persist safe failure context/outbox.

        @param decision 已验证最终失败 settlement / Validated final-failure settlement.
        @param assistant_message 不含内部诊断的 canonical 用户反馈 / Canonical user feedback without internal diagnostics.
        @param outbounds 安全失败反馈出站 / Safe failure-feedback outbounds.
        @return 原子失败反馈回执 / Atomic failure-feedback receipt.
        @note activity fencing 成功后、Turn 终态转移前，当前附件的 strict pending marker
            会在同一事务中转为 unavailable。receipt 竞争若先完成 imported，条件更新返回
            零行而保留 receipt；finalizer 若先胜出，后续 receipt publish 会因 unavailable
            冲突而回滚。/ After activity fencing succeeds and before the Turn enters its terminal
            state, a current attachment's strict pending marker becomes unavailable in the same
            transaction. If a receipt race already completed imported, the conditional update
            affects zero rows and preserves it; if finalization wins, later receipt publication
            conflicts on unavailable and rolls back.
        """

        claim = decision.claim
        target = decision.activity
        failure_time = target.updated_at
        normalized_error = target.last_error
        if normalized_error is None:  # pragma: no cover - decision proves failure.
            raise AssertionError("Inference final failure lost its summary")
        if not outbounds:
            raise ValueError("Final inference failure requires outbound feedback")
        if assistant_message.created_at > failure_time or any(
            outbound.created_at > failure_time for outbound in outbounds
        ):
            raise ValueError(
                "Inference failure effects cannot be created after failed_at"
            )
        async with db.transaction() as connection:
            await db.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(claim.activity.conversation_id),),
                connection=connection,
            )
            (
                current,
                current_token,
                current_lease,
            ) = await self._load_inference_activity_for_update(
                claim.activity.activity_id,
                connection=connection,
            )
            _validate_claim_identity(current, claim)
            if (
                current.status is not InferenceActivityStatus.PROCESSING
                or current != claim.activity
                or current_token != claim.token
                or current_lease != claim.lease_expires_at
            ):
                raise StaleClaimError(
                    f"Stale inference claim for {current.activity_id}"
                )
            turn = await _load_turn_for_mutation(
                current.turn_id,
                connection=connection,
            )
            if turn.state is not TurnState.WAITING_INFERENCE:
                raise ConcurrentTurnUpdateError(
                    f"Inference failure requires waiting_inference, found {turn.state.value}"
                )
            _validate_message_for_turn(
                turn,
                assistant_message,
                expected_role=MessageRole.ASSISTANT,
            )
            for outbound in outbounds:
                _validate_outbound_for_turn(turn, outbound)
            activity_row = await db.fetch_one(
                "UPDATE conversation.inference_activities "
                "SET status = 'failed', version = %s, next_attempt_at = NULL, "
                "claim_token = NULL, lease_expires_at = NULL, completion_token = NULL, "
                "updated_at = %s, last_error = %s, retry_budget_used = %s "
                "WHERE activity_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND version = %s AND claim_token = CAST(%s AS UUID) AND input_revision = %s "
                "AND retry_budget_used = %s "
                "RETURNING " + _INFERENCE_ACTIVITY_COLUMNS,
                (
                    target.version,
                    failure_time,
                    normalized_error,
                    target.retry_budget_used,
                    str(current.activity_id),
                    claim.expected_version,
                    str(claim.token),
                    int(claim.activity.input_revision),
                    claim.activity.retry_budget_used,
                ),
                connection=connection,
            )
            if activity_row is None:
                raise StaleClaimError(
                    f"Stale inference claim for {current.activity_id}"
                )
            failed_activity = _map_inference_activity(activity_row)
            if failed_activity != target:
                raise RuntimeError(
                    "Inference final-failure SQL diverged from the domain transition"
                )
            await self._terminalize_pending_current_attachment(
                failed_activity,
                connection=connection,
            )
            message_result = await _append_message(
                assistant_message,
                connection=connection,
            )
            outbound_results = tuple(
                [
                    await self._outbox.enqueue_outbound_in_transaction(
                        connection,
                        outbound,
                    )
                    for outbound in outbounds
                ]
            )
            waiting_delivery = turn.transition(
                TurnEvent.REQUEST_FAILURE_DELIVERY,
                occurred_at=failure_time,
                error=normalized_error,
            )
            await _persist_turn(
                waiting_delivery,
                expected_version=turn.version,
                connection=connection,
            )
            return InferenceFailureDeliveryResult(
                turn=waiting_delivery,
                activity=failed_activity,
                assistant_message=message_result,
                outbounds=outbound_results,
            )

    @staticmethod
    async def _terminalize_pending_current_attachment(
        activity: InferenceActivity,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 将最终失败当前附件的 pending marker 终结为 unavailable / Terminalize a finally failed current attachment's pending marker as unavailable.

        @param activity 已 fencing 为 failed 的 durable activity / Durable activity already fenced as failed.
        @param connection 当前 failure transaction 连接 / Connection of the current failure transaction.
        @return None / None.
        @note 只匹配严格 marker；畸形数据保持 fail-closed，不在这里“修复”或伪造 receipt。
            数据库 transition trigger 还会验证同 Turn activity 已为 failed 且没有 receipt。
            / Only a strict marker matches; malformed data stays fail-closed and is neither
            repaired nor given a fabricated receipt here. The database transition trigger also
            verifies that the same Turn activity is failed and has no receipt.
        """

        if not _has_matching_current_turn_upload(activity.request):
            return
        await db.execute(
            "UPDATE conversation.conversation_messages SET content = jsonb_set("
            "content, '{workspace_attachment,state}', '\"unavailable\"'::JSONB, false) "
            "WHERE turn_id = CAST(%s AS UUID) AND conversation_id = %s AND role = 'user' "
            "AND jsonb_typeof(content -> 'workspace_attachment') = 'object' "
            "AND jsonb_typeof(content #> '{workspace_attachment,version}') = 'number' "
            "AND content #>> '{workspace_attachment,version}' = '1' "
            "AND jsonb_typeof(content #> '{workspace_attachment,state}') = 'string' "
            "AND content #>> '{workspace_attachment,state}' = 'pending'",
            (str(activity.turn_id), str(activity.conversation_id)),
            connection=connection,
        )

    async def recover_expired_inference_leases(self, *, now: datetime) -> int:
        """@brief 原子回收过期活动租约并同步 Turn / Atomically recover expired activity leases and synchronize Turns.

        @param now 当前 UTC 时间 / Current UTC time.
        @return 回收数量 / Number of recovered activities.
        """

        timestamp = ensure_utc(now)
        retry_time = timestamp + timedelta.resolution
        recovery_error = "inference worker lease expired before finalization"
        async with db.transaction() as connection:
            rows = await db.fetch_all(
                "SELECT "
                + _INFERENCE_ACTIVITY_COLUMNS
                + ", candidate.claim_token, candidate.lease_expires_at "
                "FROM conversation.inference_activities AS candidate "
                "WHERE candidate.status = 'processing' "
                "AND candidate.lease_expires_at <= %s "
                "FOR UPDATE OF candidate SKIP LOCKED",
                (timestamp,),
                connection=connection,
            )
            for row in rows:
                values = _row_values(row, 18)
                previous = _map_inference_activity(values[:16])
                if values[16] is None or values[17] is None:
                    raise RuntimeError(
                        "Expired processing inference lost its lease ownership"
                    )
                lease = InferenceActivityLease.restore(
                    previous,
                    token=LeaseToken.parse(_uuid(values[16])),
                    lease_expires_at=_datetime(values[17]),
                )
                recovery = previous.recover_expired_lease(
                    lease,
                    recovered_at=timestamp,
                    retry_at=retry_time,
                    failure=InferenceFailure(recovery_error),
                )
                target = recovery.activity
                activity_row = await db.fetch_one(
                    "UPDATE conversation.inference_activities "
                    "SET status = %s, version = %s, attempt_count = %s, "
                    "retry_budget_used = %s, next_attempt_at = %s, "
                    "claim_token = NULL, lease_expires_at = NULL, "
                    "completion_token = NULL, completed_at = NULL, updated_at = %s, "
                    "last_error = %s, input_revision = %s "
                    "WHERE activity_id = CAST(%s AS UUID) AND status = 'processing' "
                    "AND version = %s AND attempt_count = %s "
                    "AND retry_budget_used = %s AND input_revision = %s "
                    "AND updated_at = %s AND claim_token = CAST(%s AS UUID) "
                    "AND lease_expires_at = %s RETURNING "
                    + _INFERENCE_ACTIVITY_COLUMNS,
                    (
                        target.status.value,
                        target.version,
                        target.attempt_count,
                        target.retry_budget_used,
                        target.next_attempt_at,
                        target.updated_at,
                        target.last_error,
                        int(target.input_revision),
                        str(previous.activity_id),
                        previous.version,
                        previous.attempt_count,
                        previous.retry_budget_used,
                        int(previous.input_revision),
                        previous.updated_at,
                        str(lease.token),
                        lease.lease_expires_at,
                    ),
                    connection=connection,
                )
                if activity_row is None:
                    raise ConcurrentTurnUpdateError(
                        f"Expired inference {previous.activity_id} changed while row-locked"
                    )
                persisted_retry = _map_inference_activity(activity_row)
                if recovery.activity != persisted_retry:
                    raise RuntimeError(
                        "Inference lease-recovery SQL diverged from the domain transition"
                    )
                turn = await _load_turn_for_mutation(
                    persisted_retry.turn_id,
                    connection=connection,
                )
                if turn.state is not TurnState.WAITING_INFERENCE:
                    raise ConcurrentTurnUpdateError(
                        f"Expired inference {persisted_retry.activity_id} requires a "
                        f"waiting_inference turn, found {turn.state.value}"
                    )
                retrying = turn.transition(
                    TurnEvent.SCHEDULE_INFERENCE_RETRY,
                    occurred_at=timestamp,
                    retry_at=retry_time,
                    error=recovery_error,
                )
                await _persist_turn(
                    retrying,
                    expected_version=turn.version,
                    connection=connection,
                )
            return len(rows)

    async def _load_inference_activity_for_update(
        self,
        activity_id: InferenceActivityId,
        *,
        connection: AsyncConnection,
    ) -> tuple[InferenceActivity, LeaseToken | None, datetime | None]:
        """@brief 锁定活动并读取当前 token/lease / Lock an activity and load its current token and lease.

        @param activity_id 活动 ID / Activity identifier.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return 活动聚合、可选 token 与可选 lease / Aggregate, optional token, and optional lease.
        @raise TurnNotFoundError 活动不存在时抛出 / Raised when the activity does not exist.
        """

        row = await db.fetch_one(
            "SELECT "
            + _INFERENCE_ACTIVITY_COLUMNS
            + ", claim_token, lease_expires_at FROM conversation.inference_activities "
            "WHERE activity_id = CAST(%s AS UUID) FOR UPDATE",
            (str(activity_id),),
            connection=connection,
        )
        if row is None:
            raise TurnNotFoundError(f"Inference activity {activity_id} does not exist")
        values = _row_values(row, 18)
        token = LeaseToken.parse(_uuid(values[16])) if values[16] is not None else None
        return (
            _map_inference_activity(values[:16]),
            token,
            _optional_datetime(values[17]),
        )


def _has_matching_current_turn_upload(request: Mapping[str, object]) -> bool:
    """@brief 判断 request 是否含可授权 unavailable 转移的当前上传引用 / Determine whether a request carries a current-upload reference that may authorize an unavailable transition.

    @param request durable inference request JSON / Durable inference request JSON.
    @return 当 ``current_turn_upload.source_message_id`` 与 ``scope.message_id`` 均为 JSON
        整数且精确相同、scope 的个人/群 grammar 有效时为 True / ``True`` only when
        ``current_turn_upload.source_message_id`` and ``scope.message_id`` are equal JSON
        integers and the personal/group scope grammar is valid.
    @note 此 predicate 与 0071 PostgreSQL trigger 的最小授权条件对齐。畸形 request 不得
        借 ``failed`` 状态把无关 pending marker 终结为 ``unavailable``；它会保持
        fail-closed。/ This predicate aligns with the minimal authorization condition in the
        0071 PostgreSQL trigger. A malformed request must not use a ``failed`` state to
        terminalize an unrelated pending marker as ``unavailable``; it remains fail-closed.
    """

    upload = request.get("current_turn_upload")
    scope = request.get("scope")
    if not isinstance(upload, Mapping) or not isinstance(scope, Mapping):
        return False
    source_message_id = upload.get("source_message_id")
    scope_message_id = scope.get("message_id")
    is_group = scope.get("is_group")
    if (
        not isinstance(is_group, bool)
        or not isinstance(source_message_id, int)
        or isinstance(source_message_id, bool)
        or not isinstance(scope_message_id, int)
        or isinstance(scope_message_id, bool)
        or source_message_id != scope_message_id
    ):
        return False
    if is_group:
        group_id = scope.get("group_id")
        return (
            isinstance(group_id, int)
            and not isinstance(group_id, bool)
            and group_id != 0
        )
    user = request.get("user")
    if not isinstance(user, Mapping):
        return False
    user_id = user.get("user_id")
    return isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0


__all__ = ["InferenceOutboxWriter", "PostgresInferenceRepository"]
