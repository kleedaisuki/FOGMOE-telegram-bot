"""@brief PostgreSQL inference-activity adapter / PostgreSQL inference-activity adapter."""

from __future__ import annotations

from datetime import datetime, timedelta
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.conversation.identity import (
    InferenceActivityId,
    LeaseToken,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.conversation.turn import (
    POST_INFERENCE_COMPLETION_TURN_STATES,
    ConversationTurn,
    TurnEvent,
    TurnState,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityClaim,
    InferenceActivityStatus,
)
from fogmoe_bot.domain.conversation.message import (
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundDraft,
    OutboundEnqueueResult,
)
from fogmoe_bot.domain.conversation.workflow_results import InferenceCompletionResult
from fogmoe_bot.domain.conversation.errors import (
    ConcurrentTurnUpdateError,
    StaleClaimError,
    TurnNotFoundError,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import (
    _INFERENCE_ACTIVITY_COLUMNS,
    _INFERENCE_ACTIVITY_SELECT,
    _claim_window,
    _map_inference_activity,
    _require_claim_update,
    _required_error,
    _row_values,
    _text,
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
    """@brief 验证 claim 的不可变活动语义 / Validate immutable activity semantics carried by a claim."""

    _validate_inference_activity_idempotency(current, claim.activity.draft)


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

        row = await db_connection.fetch_one(
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
        async with db_connection.transaction() as connection:
            rows = await db_connection.fetch_all(
                "WITH candidates AS ("
                "SELECT candidate.activity_id, candidate.status AS previous_status "
                "FROM conversation.inference_activities AS candidate "
                "WHERE candidate.status IN ('pending', 'retry') "
                "AND candidate.next_attempt_at <= %s AND NOT EXISTS ("
                "SELECT 1 FROM conversation.inference_activities AS earlier "
                "WHERE earlier.conversation_id = candidate.conversation_id "
                "AND earlier.status IN ('pending', 'processing', 'retry') "
                "AND (earlier.created_at, earlier.activity_id) "
                "< (candidate.created_at, candidate.activity_id)"
                ") ORDER BY candidate.next_attempt_at ASC, candidate.activity_id ASC LIMIT %s "
                "FOR UPDATE SKIP LOCKED"
                ") UPDATE conversation.inference_activities AS activity "
                "SET status = 'processing', version = activity.version + 1, "
                "attempt_count = activity.attempt_count + 1, next_attempt_at = NULL, "
                "claim_token = CAST(%s AS UUID), lease_expires_at = %s, "
                "completion_token = NULL, updated_at = %s, last_error = NULL "
                "FROM candidates WHERE activity.activity_id = candidates.activity_id "
                "RETURNING activity.activity_id, activity.turn_id, "
                "activity.conversation_id, activity.request, activity.status, "
                "activity.version, activity.attempt_count, activity.next_attempt_at, "
                "activity.created_at, activity.updated_at, activity.completed_at, "
                "activity.completion_token, activity.last_error, activity.traceparent, "
                "candidates.previous_status",
                (timestamp, limit, str(token), lease_expires_at, timestamp),
                connection=connection,
            )
            claims: list[InferenceActivityClaim] = []
            for row in rows:
                values = _row_values(row, 15)
                activity = _map_inference_activity(values[:14])
                previous_status = InferenceActivityStatus(_text(values[14]))
                turn = await _load_turn_for_mutation(
                    activity.turn_id,
                    connection=connection,
                )
                if previous_status is InferenceActivityStatus.PENDING:
                    if turn.state is not TurnState.WAITING_INFERENCE:
                        raise ConcurrentTurnUpdateError(
                            f"Pending inference {activity.activity_id} requires a "
                            f"waiting_inference turn, found {turn.state.value}"
                        )
                elif previous_status is InferenceActivityStatus.RETRY:
                    if turn.state is not TurnState.INFERENCE_RETRY_WAIT:
                        raise ConcurrentTurnUpdateError(
                            f"Retry inference {activity.activity_id} requires an "
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
                        f"Unsupported inference pre-claim status {previous_status.value}"
                    )
                claims.append(
                    InferenceActivityClaim(
                        activity=activity,
                        token=token,
                        lease_expires_at=lease_expires_at,
                    )
                )
        return tuple(sorted(claims, key=lambda claim: str(claim.activity.activity_id)))

    async def complete_inference_activity(
        self,
        claim: InferenceActivityClaim,
        *,
        assistant_message: MessageDraft,
        outbounds: Sequence[OutboundDraft],
        completed_at: datetime,
    ) -> InferenceCompletionResult:
        """@brief 以 fencing token 原子提交推理、历史与 outbox / Atomically commit inference, history, and outbox with a fencing token.

        @param claim 当前活动 claim / Current activity claim.
        @param assistant_message 确定性助手消息 / Deterministic assistant message.
        @param outbounds 有序、确定性的出站副作用 / Ordered deterministic outbound effects.
        @param completed_at 完成时间 / Completion time.
        @return 原子完成回执 / Atomic completion receipt.
        @raise StaleClaimError claim 已被恢复或替代时抛出 / Raised when the claim was recovered or superseded.
        @note 相同成功 claim 的 post-commit 重放返回规范回执；更老 claim 永远不能覆盖新结果。/
        A post-commit replay of the same successful claim returns canonical receipts; an older claim can never overwrite a newer result.
        """

        timestamp = ensure_utc(completed_at)
        if timestamp < claim.activity.updated_at:
            raise ValueError("completed_at cannot precede inference claim time")
        if not outbounds:
            raise ValueError("Inference completion requires outbound effects")
        if assistant_message.created_at > timestamp or any(
            outbound.created_at > timestamp for outbound in outbounds
        ):
            raise ValueError("Inference effects cannot be created after completed_at")
        async with db_connection.transaction() as connection:
            await db_connection.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(claim.activity.conversation_id),),
                connection=connection,
            )
            (
                current,
                current_claim_token,
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
                if current.completion_token != claim.token:
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
                or current_claim_token != claim.token
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
            activity_row = await db_connection.fetch_one(
                "UPDATE conversation.inference_activities "
                "SET status = 'completed', version = version + 1, "
                "next_attempt_at = NULL, claim_token = NULL, lease_expires_at = NULL, "
                "completion_token = CAST(%s AS UUID), completed_at = %s, "
                "updated_at = %s, last_error = NULL "
                "WHERE activity_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND claim_token = CAST(%s AS UUID) RETURNING "
                + _INFERENCE_ACTIVITY_COLUMNS,
                (
                    str(claim.token),
                    timestamp,
                    timestamp,
                    str(current.activity_id),
                    str(claim.token),
                ),
                connection=connection,
            )
            if activity_row is None:
                raise StaleClaimError(
                    f"Stale inference claim for {current.activity_id}"
                )
            completed_activity = _map_inference_activity(activity_row)
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
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 原子安排活动与 Turn 重试 / Atomically schedule the activity and Turn for retry.

        @param claim 当前活动 claim / Current activity claim.
        @param failed_at 失败时间 / Failure time.
        @param retry_at 下次可领取时间 / Next claimable time.
        @param error 有界错误摘要 / Bounded error summary.
        @return None / None.
        """

        failure_time = ensure_utc(failed_at)
        retry_time = ensure_utc(retry_at)
        if failure_time < claim.activity.updated_at:
            raise ValueError("failed_at cannot precede inference claim time")
        if retry_time <= failure_time:
            raise ValueError("retry_at must be later than failed_at")
        normalized_error = _required_error(error)
        async with db_connection.transaction() as connection:
            current, current_token = await self._load_inference_activity_for_update(
                claim.activity.activity_id,
                connection=connection,
            )
            _validate_claim_identity(current, claim)
            if (
                current.status is not InferenceActivityStatus.PROCESSING
                or current_token != claim.token
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
            rowcount = await db_connection.execute(
                "UPDATE conversation.inference_activities "
                "SET status = 'retry', version = version + 1, next_attempt_at = %s, "
                "claim_token = NULL, lease_expires_at = NULL, completion_token = NULL, "
                "updated_at = %s, last_error = %s "
                "WHERE activity_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND claim_token = CAST(%s AS UUID)",
                (
                    retry_time,
                    failure_time,
                    normalized_error,
                    str(current.activity_id),
                    str(claim.token),
                ),
                connection=connection,
            )
            _require_claim_update(rowcount, "inference", str(current.activity_id))
            retrying = turn.transition(
                TurnEvent.SCHEDULE_INFERENCE_RETRY,
                occurred_at=failure_time,
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
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 原子终结活动与 Turn / Atomically fail the activity and Turn finally.

        @param claim 当前活动 claim / Current activity claim.
        @param failed_at 最终失败时间 / Final-failure time.
        @param error 有界错误摘要 / Bounded error summary.
        @return None / None.
        """

        failure_time = ensure_utc(failed_at)
        if failure_time < claim.activity.updated_at:
            raise ValueError("failed_at cannot precede inference claim time")
        normalized_error = _required_error(error)
        async with db_connection.transaction() as connection:
            current, current_token = await self._load_inference_activity_for_update(
                claim.activity.activity_id,
                connection=connection,
            )
            _validate_claim_identity(current, claim)
            if (
                current.status is not InferenceActivityStatus.PROCESSING
                or current_token != claim.token
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
            rowcount = await db_connection.execute(
                "UPDATE conversation.inference_activities "
                "SET status = 'failed', version = version + 1, next_attempt_at = NULL, "
                "claim_token = NULL, lease_expires_at = NULL, completion_token = NULL, "
                "updated_at = %s, last_error = %s "
                "WHERE activity_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND claim_token = CAST(%s AS UUID)",
                (
                    failure_time,
                    normalized_error,
                    str(current.activity_id),
                    str(claim.token),
                ),
                connection=connection,
            )
            _require_claim_update(rowcount, "inference", str(current.activity_id))
            failed = turn.transition(
                TurnEvent.FAIL_FINAL,
                occurred_at=failure_time,
                error=normalized_error,
            )
            await _persist_turn(
                failed,
                expected_version=turn.version,
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
        async with db_connection.transaction() as connection:
            rows = await db_connection.fetch_all(
                "WITH expired AS (SELECT activity_id "
                "FROM conversation.inference_activities "
                "WHERE status = 'processing' AND lease_expires_at <= %s "
                "FOR UPDATE SKIP LOCKED) "
                "UPDATE conversation.inference_activities AS activity "
                "SET status = 'retry', version = activity.version + 1, "
                "next_attempt_at = %s, claim_token = NULL, lease_expires_at = NULL, "
                "completion_token = NULL, updated_at = %s, last_error = %s "
                "FROM expired WHERE activity.activity_id = expired.activity_id RETURNING "
                "activity.activity_id, activity.turn_id, activity.conversation_id, "
                "activity.request, activity.status, activity.version, "
                "activity.attempt_count, activity.next_attempt_at, activity.created_at, "
                "activity.updated_at, activity.completed_at, activity.completion_token, "
                "activity.last_error, activity.traceparent",
                (timestamp, retry_time, timestamp, recovery_error),
                connection=connection,
            )
            for row in rows:
                activity = _map_inference_activity(row)
                turn = await _load_turn_for_mutation(
                    activity.turn_id,
                    connection=connection,
                )
                if turn.state is not TurnState.WAITING_INFERENCE:
                    raise ConcurrentTurnUpdateError(
                        f"Expired inference {activity.activity_id} requires a "
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
    ) -> tuple[InferenceActivity, LeaseToken | None]:
        """@brief 锁定活动并读取当前 claim token / Lock an activity and load its current claim token.

        @param activity_id 活动 ID / Activity identifier.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return 活动快照与可选 claim token / Activity snapshot and optional claim token.
        @raise TurnNotFoundError 活动不存在时抛出 / Raised when the activity does not exist.
        """

        row = await db_connection.fetch_one(
            "SELECT "
            + _INFERENCE_ACTIVITY_COLUMNS
            + ", claim_token FROM conversation.inference_activities "
            "WHERE activity_id = CAST(%s AS UUID) FOR UPDATE",
            (str(activity_id),),
            connection=connection,
        )
        if row is None:
            raise TurnNotFoundError(f"Inference activity {activity_id} does not exist")
        values = _row_values(row, 15)
        token = LeaseToken.parse(_uuid(values[14])) if values[14] is not None else None
        return _map_inference_activity(values[:14]), token


__all__ = ["InferenceOutboxWriter", "PostgresInferenceRepository"]
