"""@brief PostgreSQL Turn、acceptance 与历史 adapter / PostgreSQL Turn, acceptance, and history adapter."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    InferenceActivityId,
    TurnId,
)
from fogmoe_bot.domain.conversation.temporal import ensure_utc
from fogmoe_bot.domain.conversation.turn import (
    POST_ACCEPTANCE_TURN_STATES,
    ConversationTurn,
    TurnEvent,
    TurnState,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivityDraft,
    InferenceActivityEnqueueResult,
    InferenceActivityStatus,
)
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import OutboundStatus
from fogmoe_bot.domain.conversation.workflow_results import TurnAcceptanceResult
from fogmoe_bot.domain.conversation.errors import (
    ConcurrentTurnUpdateError,
    IdempotencyConflictError,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.assistant_billing import (
    AssistantBillingTransactions,
    PostgresAssistantBilling,
)

from .common import (
    _INFERENCE_ACTIVITY_COLUMNS,
    _INFERENCE_ACTIVITY_SELECT,
    _encode_json,
    _map_inference_activity,
    _row_values,
    _text,
    _validate_inference_activity_idempotency,
)
from .turn_uow import (
    _append_message,
    _load_turn_for_mutation,
    _persist_turn,
    _require_existing_message,
    _TURN_SELECT,
    _map_message,
    _map_turn,
    _validate_message_for_turn,
)


def _validate_activity_for_turn(
    turn: ConversationTurn,
    draft: InferenceActivityDraft,
) -> None:
    """@brief 验证 acceptance 活动所有权 / Validate ownership of an acceptance activity."""

    if draft.turn_id != turn.turn_id:
        raise ValueError("Inference activity must belong to the target turn")
    if draft.conversation_id != turn.conversation_id:
        raise ValueError("Inference activity must belong to the target conversation")
    if draft.activity_id != InferenceActivityId.for_turn(turn.turn_id):
        raise ValueError(
            "Primary inference activity must use the deterministic Turn ID"
        )


class PostgresTurnRepository:
    """@brief 拥有 Turn acceptance、历史读取与取消生命周期 / Own Turn acceptance, history reads, and cancellation."""

    def __init__(
        self,
        billing: AssistantBillingTransactions | None = None,
    ) -> None:
        """@brief 注入同事务 Assistant 计费原语 / Inject the same-transaction Assistant billing primitive.

        @param billing reserve/settle/release 事务端口 / Reserve/settle/release transaction port.
        """

        self._billing = billing or PostgresAssistantBilling()
        """@brief 推理终结与取消共享的计费状态机 / Billing state machine shared by inference finalization and cancellation."""

    async def create_and_accept_turn(
        self,
        turn: ConversationTurn,
        *,
        message: MessageDraft,
        activity: InferenceActivityDraft,
        accepted_at: datetime,
    ) -> TurnAcceptanceResult:
        """@brief 在一个短事务内创建并接受完整回合 / Create and accept a complete Turn in one short transaction.

        @param turn 初始 RECEIVED 回合 / Initial RECEIVED turn.
        @param message 确定性用户消息 / Deterministic user message.
        @param activity 确定性 primary 推理意图 / Deterministic primary inference intent.
        @param accepted_at 接受时间 / Acceptance time.
        @return 原子 acceptance 回执 / Atomic acceptance receipt.
        @note 任一写入失败都会回滚 Turn 插入；不存在孤立 RECEIVED 窗口。/
        Any write failure rolls back the Turn insert; there is no orphan RECEIVED window.
        """

        async with db_connection.transaction() as connection:
            return await self.create_and_accept_turn_in_transaction(
                connection,
                turn,
                message=message,
                activity=activity,
                accepted_at=accepted_at,
            )

    async def create_and_accept_turn_in_transaction(
        self,
        connection: AsyncConnection,
        turn: ConversationTurn,
        *,
        message: MessageDraft,
        activity: InferenceActivityDraft,
        accepted_at: datetime,
    ) -> TurnAcceptanceResult:
        """@brief 在调用方 PostgreSQL 事务内创建并接受回合 / Create and accept a Turn inside the caller's PostgreSQL transaction.

        @param connection 调用方拥有的活动 AsyncConnection / Active caller-owned AsyncConnection.
        @param turn 初始 RECEIVED 回合 / Initial RECEIVED turn.
        @param message 确定性用户消息 / Deterministic user message.
        @param activity 确定性 primary 推理意图 / Deterministic primary inference intent.
        @param accepted_at 接受时间 / Acceptance time.
        @return 原子 acceptance 回执 / Atomic acceptance receipt.
        @note 本方法不 commit、不 rollback、也不打开嵌套事务；仅供 infrastructure
        组合 billing 等同库 UoW。/ This method neither commits, rolls back, nor opens a
        nested transaction; it is an infrastructure-only primitive for composing same-database
        units of work such as billing.
        """

        if (
            turn.state is not TurnState.RECEIVED
            or turn.version != 0
            or turn.inference_attempts != 0
            or turn.delivery_attempts != 0
        ):
            raise ValueError("create_and_accept_turn requires an initial received turn")
        timestamp = ensure_utc(accepted_at)
        if timestamp < turn.updated_at:
            raise ValueError("accepted_at cannot precede turn receipt")

        current = await self._insert_or_load_turn(
            turn,
            connection=connection,
        )
        _validate_message_for_turn(
            current,
            message,
            expected_role=MessageRole.USER,
        )
        _validate_activity_for_turn(current, activity)
        if message.created_at > timestamp or activity.created_at > timestamp:
            raise ValueError(
                "Acceptance effects cannot be created after turn acceptance"
            )

        if current.version == turn.version and current.state is TurnState.RECEIVED:
            updated = current.transition(
                TurnEvent.ACCEPT,
                occurred_at=timestamp,
            ).transition(
                TurnEvent.REQUEST_INFERENCE,
                occurred_at=timestamp,
            )
            message_result = await _append_message(
                message,
                connection=connection,
            )
            activity_result = await self._enqueue_inference_activity(
                activity,
                connection=connection,
            )
            await _persist_turn(
                updated,
                expected_version=turn.version,
                connection=connection,
            )
        elif (
            current.version >= turn.version + 2
            and current.state in POST_ACCEPTANCE_TURN_STATES
        ):
            updated = current
            message_result = await _require_existing_message(
                message,
                operation="turn acceptance",
                connection=connection,
            )
            activity_result = await self._require_existing_inference_activity(
                activity,
                operation="turn acceptance",
                connection=connection,
            )
        else:
            raise ConcurrentTurnUpdateError(
                f"Turn {turn.turn_id} expected initial version {turn.version}, "
                f"found {current.version}:{current.state.value}"
            )

        return TurnAcceptanceResult(
            turn=updated,
            user_message=message_result,
            inference_activity=activity_result,
        )

    async def _insert_or_load_turn(
        self,
        turn: ConversationTurn,
        *,
        connection: AsyncConnection,
    ) -> ConversationTurn:
        """@brief 插入初始 Turn 或锁定其规范重放行 / Insert an initial Turn or lock its canonical replay row.

        @param turn 初始 RECEIVED 回合 / Initial RECEIVED turn.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return 新 Turn 或已锁定规范 Turn / New Turn or locked canonical Turn.
        @raise IdempotencyConflictError ID/source 已承载不同语义时抛出 / Raised when an ID or source carries different semantics.
        """

        row = await db_connection.fetch_one(
            "INSERT INTO conversation.conversation_turns "
            "(turn_id, conversation_id, source_kind, source_key, source_update_id, state, version, "
            "inference_attempts, delivery_attempts, next_retry_at, last_error, "
            "created_at, updated_at, completed_at) "
            "VALUES (CAST(%s AS UUID), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL) "
            "ON CONFLICT DO NOTHING RETURNING turn_id",
            (
                str(turn.turn_id),
                str(turn.conversation_id),
                turn.source.kind,
                turn.source.key,
                int(turn.source.update_id)
                if turn.source.update_id is not None
                else None,
                turn.state.value,
                turn.version,
                turn.inference_attempts,
                turn.delivery_attempts,
                turn.next_retry_at,
                turn.last_error,
                turn.created_at,
                turn.updated_at,
            ),
            connection=connection,
        )
        if row is not None:
            return turn
        existing_row = await db_connection.fetch_one(
            _TURN_SELECT + " WHERE turn_id = CAST(%s AS UUID) "
            "OR (source_kind = %s AND source_key = %s) "
            "OR (%s IS NOT NULL AND source_update_id = %s) FOR UPDATE",
            (
                str(turn.turn_id),
                turn.source.kind,
                turn.source.key,
                int(turn.source.update_id)
                if turn.source.update_id is not None
                else None,
                int(turn.source.update_id)
                if turn.source.update_id is not None
                else None,
            ),
            connection=connection,
        )
        if existing_row is None:
            raise RuntimeError("Turn insert conflicted but no canonical row exists")
        existing = _map_turn(existing_row)
        if (
            existing.turn_id != turn.turn_id
            or existing.conversation_id != turn.conversation_id
            or existing.source != turn.source
        ):
            raise IdempotencyConflictError(
                f"Turn {turn.turn_id} or source {turn.source.kind}:{turn.source.key} "
                "was reused with different semantics"
            )
        return existing

    async def get_turn(self, turn_id: TurnId) -> ConversationTurn | None:
        """@brief 读取回合快照 / Load a turn snapshot.

        @param turn_id 回合 ID / Turn identifier.
        @return 回合或 None / Turn or None.
        """

        row = await db_connection.fetch_one(
            _TURN_SELECT + " WHERE turn_id = CAST(%s AS UUID)",
            (str(turn_id),),
        )
        return _map_turn(row) if row is not None else None

    async def read_conversation_messages(
        self,
        conversation_id: ConversationId,
        *,
        through_turn_id: TurnId,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """@brief 按 sequence 读取截至指定 Turn 的有界规范历史 / Read bounded canonical sequence history through a Turn.

        @param conversation_id 长期会话 ID / Long-lived conversation identifier.
        @param through_turn_id 历史截止回合 / Turn defining the history cutoff.
        @param limit 最大消息数，范围 1..512 / Maximum message count in the range 1..512.
        @return 按 sequence 升序排列的最近消息 / Most recent messages ordered by ascending sequence.
        @raise ValueError limit 越界时抛出 / Raised when limit is out of bounds.
        @note 截止点是该 Turn 已持久化消息的最大 sequence；旧 Turn 中标记
        ``exclude_from_assistant=true`` 的消息在 LIMIT 前排除，但当前 Turn 始终保留用于
        activity identity 校验。/ The cutoff is the maximum persisted sequence for the Turn;
        messages from older Turns marked ``exclude_from_assistant=true`` are removed before LIMIT,
        while the current Turn is retained for activity-identity validation.
        """

        if not 1 <= limit <= 512:
            raise ValueError("conversation history limit must be between 1 and 512")
        rows = await db_connection.fetch_all(
            "WITH turn_bounds AS ("
            "SELECT MIN(sequence) AS first_sequence, MAX(sequence) AS last_sequence "
            "FROM conversation.conversation_messages "
            "WHERE conversation_id = %s AND turn_id = CAST(%s AS UUID)"
            "), reset_boundary AS ("
            "SELECT COALESCE(MAX(history_reset.through_sequence), 0) AS sequence "
            "FROM conversation.conversation_history_resets AS history_reset "
            "CROSS JOIN turn_bounds "
            "WHERE history_reset.conversation_id = %s "
            "AND turn_bounds.first_sequence IS NOT NULL "
            "AND history_reset.through_sequence < turn_bounds.first_sequence"
            "), recent AS ("
            "SELECT message.message_id, message.conversation_id, message.sequence, "
            "message.turn_id, message.source_update_id, message.role, message.content, "
            "message.idempotency_key, message.created_at "
            "FROM conversation.conversation_messages AS message "
            "CROSS JOIN turn_bounds CROSS JOIN reset_boundary "
            "WHERE message.conversation_id = %s "
            "AND turn_bounds.last_sequence IS NOT NULL "
            "AND message.sequence > reset_boundary.sequence "
            "AND message.sequence <= turn_bounds.last_sequence "
            "AND (message.turn_id = CAST(%s AS UUID) OR NOT "
            "(message.content @> jsonb_build_object('exclude_from_assistant', TRUE))) "
            "ORDER BY message.sequence DESC LIMIT %s"
            ") SELECT message_id, conversation_id, sequence, turn_id, "
            "source_update_id, role, content, idempotency_key, created_at "
            "FROM recent ORDER BY sequence ASC",
            (
                str(conversation_id),
                str(through_turn_id),
                str(conversation_id),
                str(conversation_id),
                str(through_turn_id),
                limit,
            ),
        )
        return tuple(_map_message(row) for row in rows)

    async def _enqueue_inference_activity(
        self,
        draft: InferenceActivityDraft,
        *,
        connection: AsyncConnection,
    ) -> InferenceActivityEnqueueResult:
        """@brief 在 acceptance 事务内幂等写入活动 / Idempotently persist an activity within the acceptance transaction.

        @param draft 活动意图 / Activity intent.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return 规范活动与插入标志 / Canonical activity and insertion flag.
        """

        row = await db_connection.fetch_one(
            "INSERT INTO conversation.inference_activities "
            "(activity_id, turn_id, conversation_id, request, status, version, "
            "attempt_count, next_attempt_at, created_at, updated_at, traceparent) "
            "VALUES (CAST(%s AS UUID), CAST(%s AS UUID), %s, CAST(%s AS JSONB), "
            "'pending', 0, 0, %s, %s, %s, %s) ON CONFLICT (turn_id) DO NOTHING "
            "RETURNING " + _INFERENCE_ACTIVITY_COLUMNS,
            (
                str(draft.activity_id),
                str(draft.turn_id),
                str(draft.conversation_id),
                _encode_json(draft.request),
                draft.created_at,
                draft.trace_context.to_traceparent(),
                draft.created_at,
                draft.created_at,
            ),
            connection=connection,
        )
        if row is not None:
            return InferenceActivityEnqueueResult(
                activity=_map_inference_activity(row),
                inserted=True,
            )
        return await self._require_existing_inference_activity(
            draft,
            operation="inference activity enqueue",
            connection=connection,
        )

    async def _require_existing_inference_activity(
        self,
        draft: InferenceActivityDraft,
        *,
        operation: str,
        connection: AsyncConnection,
    ) -> InferenceActivityEnqueueResult:
        """@brief 读取并校验已提交活动意图 / Load and validate an already committed activity intent.

        @param draft 重放意图 / Replayed intent.
        @param operation 组合操作名 / Composite-operation name.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return inserted=False 的规范活动 / Canonical activity with inserted=False.
        """

        row = await db_connection.fetch_one(
            _INFERENCE_ACTIVITY_SELECT
            + " WHERE activity_id = CAST(%s AS UUID) OR turn_id = CAST(%s AS UUID) "
            "LIMIT 1",
            (str(draft.activity_id), str(draft.turn_id)),
            connection=connection,
        )
        if row is None:
            raise ConcurrentTurnUpdateError(
                f"Turn advanced through {operation} without its inference activity"
            )
        existing = _map_inference_activity(row)
        _validate_inference_activity_idempotency(existing, draft)
        return InferenceActivityEnqueueResult(existing, False)

    async def cancel_turn(
        self,
        turn_id: TurnId,
        *,
        expected_version: int,
        cancelled_at: datetime,
    ) -> ConversationTurn:
        """@brief 原子取消回合及其未领取 activity/outbox / Atomically cancel a turn and its unclaimed activity/outbox.

        @param turn_id 回合 ID / Turn identifier.
        @param expected_version 调用方观察到的版本 / Version observed by the caller.
        @param cancelled_at 取消时间 / Cancellation time.
        @return CANCELLED 回合 / Turn in CANCELLED.
        @raise ConcurrentTurnUpdateError activity/outbox 正在处理或版本冲突时抛出 / Raised when the activity/outbox is processing or the version conflicts.
        @note 两阶段 outbox 锁定在获取 turn 锁后重查 phantom，覆盖并发 inference commit 在等待期间插入 outbox 的竞态 /
        Two-pass outbox locking rechecks for a phantom after acquiring the turn lock, covering an outbox inserted by a concurrent inference commit while cancellation waited.
        """

        if expected_version < 0:
            raise ValueError("expected_version cannot be negative")
        timestamp = ensure_utc(cancelled_at)
        async with db_connection.transaction() as connection:
            await self._lock_active_inference_for_turn(
                turn_id,
                connection=connection,
            )
            # 先锁可见 outbox 以保持 outbox→turn 顺序；获取 turn 后必须重查并发新行 /
            # Lock visible outbox first to preserve outbox→turn order; recheck after the turn for concurrent inserts.
            await self._lock_active_outbound_for_turn(
                turn_id,
                connection=connection,
            )
            current = await _load_turn_for_mutation(
                turn_id,
                connection=connection,
            )
            active_inference_rows = await self._lock_active_inference_for_turn(
                turn_id,
                connection=connection,
            )
            if any(
                InferenceActivityStatus(_text(_row_values(row, 1)[0]))
                is InferenceActivityStatus.PROCESSING
                for row in active_inference_rows
            ):
                raise ConcurrentTurnUpdateError(
                    f"Turn {turn_id} cannot be cancelled while inference is processing"
                )
            active_rows = await self._lock_active_outbound_for_turn(
                turn_id,
                connection=connection,
            )
            if any(
                OutboundStatus(_text(_row_values(row, 1)[0]))
                is OutboundStatus.PROCESSING
                for row in active_rows
            ):
                raise ConcurrentTurnUpdateError(
                    f"Turn {turn_id} cannot be cancelled while delivery is processing"
                )
            if current.version != expected_version:
                raise ConcurrentTurnUpdateError(
                    f"Turn {turn_id} expected version {expected_version}, "
                    f"found {current.version}"
                )
            await db_connection.execute(
                "UPDATE conversation.inference_activities "
                "SET status = 'cancelled', version = version + 1, "
                "next_attempt_at = NULL, updated_at = %s, claim_token = NULL, "
                "lease_expires_at = NULL, completion_token = NULL "
                "WHERE turn_id = CAST(%s AS UUID) AND status IN ('pending', 'retry')",
                (timestamp, str(turn_id)),
                connection=connection,
            )
            await db_connection.execute(
                "UPDATE conversation.outbound_messages "
                "SET status = 'cancelled', version = version + 1, next_attempt_at = NULL, "
                "updated_at = %s, claim_token = NULL, lease_expires_at = NULL "
                "WHERE turn_id = CAST(%s AS UUID) "
                "AND status IN ('pending', 'retry_wait')",
                (timestamp, str(turn_id)),
                connection=connection,
            )
            cancelled = current.transition(
                TurnEvent.CANCEL,
                occurred_at=timestamp,
            )
            await _persist_turn(
                cancelled,
                expected_version=expected_version,
                connection=connection,
            )
            await self._billing.release(
                connection,
                turn_id=turn_id,
                released_at=timestamp,
            )
            return cancelled

    async def _lock_active_inference_for_turn(
        self,
        turn_id: TurnId,
        *,
        connection: AsyncConnection,
    ) -> Sequence[object]:
        """@brief 锁定回合的活动推理行 / Lock a turn's active inference row.

        @param turn_id 回合 ID / Turn identifier.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return 活动状态行 / Active-status rows.
        """

        rows: Sequence[object] = await db_connection.fetch_all(
            "SELECT status FROM conversation.inference_activities "
            "WHERE turn_id = CAST(%s AS UUID) "
            "AND status IN ('pending', 'processing', 'retry') FOR UPDATE",
            (str(turn_id),),
            connection=connection,
        )
        return rows

    async def _lock_active_outbound_for_turn(
        self,
        turn_id: TurnId,
        *,
        connection: AsyncConnection,
    ) -> Sequence[object]:
        """@brief 锁定回合的活动 outbox 行 / Lock a turn's active outbox row.

        @param turn_id 回合 ID / Turn identifier.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return 活动状态行 / Active-status rows.
        """

        rows: Sequence[object] = await db_connection.fetch_all(
            "SELECT status FROM conversation.outbound_messages "
            "WHERE turn_id = CAST(%s AS UUID) "
            "AND status IN ('pending', 'processing', 'retry_wait') FOR UPDATE",
            (str(turn_id),),
            connection=connection,
        )
        return rows


__all__ = ["PostgresTurnRepository"]
