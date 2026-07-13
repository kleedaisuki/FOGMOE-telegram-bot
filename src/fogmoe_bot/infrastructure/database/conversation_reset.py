"""@brief PostgreSQL Conversation reset 与确认 outbox UoW / PostgreSQL Conversation-reset and confirmation-outbox unit of work."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.conversation.reset import (
    ConversationResetResult,
    ResetConversation,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    TurnId,
    TurnSource,
    UpdateId,
)
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.domain.conversation.errors import (
    ConcurrentTurnUpdateError,
    IdempotencyConflictError,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.command_source import (
    validate_telegram_command_source,
)
from fogmoe_bot.infrastructure.database.assistant_billing import (
    AssistantBillingTransactions,
    PostgresAssistantBilling,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
    StandaloneOutboxWriter,
)


class PostgresConversationResetUoW:
    """@brief 用会话 advisory lock 原子建立 reset 边界与 outbox / Atomically establish a reset boundary and outbox under a conversation advisory lock."""

    def __init__(
        self,
        outbox: StandaloneOutboxWriter | None = None,
        billing: AssistantBillingTransactions | None = None,
    ) -> None:
        """@brief 注入 connection-bound outbox 与计费原语 / Inject connection-bound outbox and billing primitives.

        @param outbox Conversation workflow repository / Conversation workflow repository.
        @param billing reserve/settle/release 计费原语 / Reserve/settle/release billing primitive.
        """

        self._outbox = outbox or PostgresOutboxRepository()
        """@brief 同事务 standalone outbox primitive / Same-transaction standalone-outbox primitive."""
        self._billing = billing or PostgresAssistantBilling()
        """@brief reset 释放未结算 Turn 的计费原语 / Billing primitive releasing unsettled Turns during reset."""

    async def reset(self, command: ResetConversation) -> ConversationResetResult:
        """@brief 幂等写入 reset 与确认副作用 / Idempotently write the reset and confirmation effect.

        @param command 已校验 reset 命令 / Validated reset command.
        @return 原子结果 / Atomic result.
        @note advisory lock 与消息追加使用相同 conversation key；因此边界一定落在两个
            完整 sequence 之间。/ The advisory lock uses the same conversation key as message
            appends, so the boundary always falls between two complete sequences.
        """

        async with db_connection.transaction() as connection:
            await validate_telegram_command_source(
                command.source,
                command.conversation_id,
                operation="Conversation reset",
                connection=connection,
            )
            await db_connection.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(command.conversation_id),),
                connection=connection,
            )
            existing = await self._find(command.source, connection=connection)
            if existing is not None:
                through_sequence = self._validate_existing(command, existing)
                confirmation = (
                    await self._outbox.enqueue_standalone_outbound_in_transaction(
                        connection,
                        command.confirmation,
                    )
                )
                return ConversationResetResult(
                    through_sequence=through_sequence,
                    inserted=False,
                    confirmation=confirmation,
                )

            await self._cancel_active_inference_turns(
                command.conversation_id,
                cancelled_at=command.requested_at,
                connection=connection,
            )

            sequence_row = await db_connection.fetch_one(
                "SELECT COALESCE(MAX(sequence), 0) "
                "FROM conversation.conversation_messages WHERE conversation_id = %s",
                (str(command.conversation_id),),
                connection=connection,
            )
            if sequence_row is None:
                raise RuntimeError("Could not calculate Conversation reset sequence")
            through_sequence = int(str(sequence_row[0]))
            inserted_row = await db_connection.fetch_one(
                "INSERT INTO conversation.conversation_history_resets "
                "(conversation_id, source_kind, source_key, source_update_id, "
                "through_sequence, created_at) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (source_kind, source_key) DO NOTHING "
                "RETURNING through_sequence",
                (
                    str(command.conversation_id),
                    command.source.kind,
                    command.source.key,
                    (
                        int(command.source.update_id)
                        if command.source.update_id is not None
                        else None
                    ),
                    through_sequence,
                    command.requested_at,
                ),
                connection=connection,
            )
            inserted = inserted_row is not None
            if not inserted:
                raced = await self._find(command.source, connection=connection)
                if raced is None:
                    raise RuntimeError("Conversation reset conflict returned no row")
                through_sequence = self._validate_existing(command, raced)

            confirmation = (
                await self._outbox.enqueue_standalone_outbound_in_transaction(
                    connection,
                    command.confirmation,
                )
            )
            return ConversationResetResult(
                through_sequence=through_sequence,
                inserted=inserted,
                confirmation=confirmation,
            )

    async def _cancel_active_inference_turns(
        self,
        conversation_id: ConversationId,
        *,
        cancelled_at: datetime,
        connection: AsyncConnection,
    ) -> None:
        """@brief fence 并取消会话内活动推理，再精确释放可选预留 / Fence and cancel active inference in a conversation, then release optional reservations.

        @param conversation_id 长期会话 identity / Long-lived conversation identity.
        @param cancelled_at reset 请求时刻 / Reset request time.
        @param connection 调用方事务连接 / Caller-owned transaction connection.
        @return None / None.
        @raise ConcurrentTurnUpdateError 活动、可选预留与 Turn 状态不一致 / Activity, optional reservation, and Turn states disagree.
        @note 零费用 Turn 没有 reservation，但仍必须被 reset fence；正费用 Turn 的原桶退款
            由同事务 billing 状态机完成。/ Zero-cost Turns own no reservation but must still be
            fenced by reset; the same-transaction billing state machine refunds positive-cost
            Turns to their exact buckets.
        @note 调用方已经持有 conversation advisory lock；活动 claim token 在同事务失效，
            因而任何已在网络 I/O 中的 worker 都只能得到 stale fencing 结果。/ The caller
            already holds the conversation advisory lock; activity claim tokens are invalidated
            in the same transaction, so a worker already doing network I/O can only observe stale
            fencing when it returns.
        """

        timestamp = ensure_utc(cancelled_at)
        activity_rows = cast(
            Sequence[object],
            await db_connection.fetch_all(
                "SELECT activity.activity_id, activity.turn_id, activity.status "
                "FROM conversation.inference_activities AS activity "
                "WHERE activity.conversation_id = %s "
                "AND activity.status IN ('pending', 'processing', 'retry') "
                "ORDER BY activity.activity_id FOR UPDATE OF activity",
                (str(conversation_id),),
                connection=connection,
            ),
        )
        activity_by_turn = {
            TurnId.parse(str(cast(Sequence[object], row)[1])): cast(
                Sequence[object], row
            )
            for row in activity_rows
        }
        if len(activity_by_turn) != len(activity_rows):
            raise ConcurrentTurnUpdateError(
                "A reset found multiple active inference activities for one Turn"
            )
        turn_ids = sorted(activity_by_turn, key=str)

        turn_rows: dict[TurnId, Sequence[object]] = {}
        for turn_id in turn_ids:
            row = await db_connection.fetch_one(
                "SELECT turn_id, state, updated_at "
                "FROM conversation.conversation_turns "
                "WHERE turn_id = CAST(%s AS UUID) FOR UPDATE",
                (str(turn_id),),
                connection=connection,
            )
            if row is None:
                raise ConcurrentTurnUpdateError(
                    f"Active inference Turn {turn_id} disappeared during reset"
                )
            values = cast(Sequence[object], row)
            if len(values) != 3:
                raise RuntimeError(
                    f"Expected 3 reset Turn columns, received {len(values)}"
                )
            if str(values[1]) not in {"waiting_inference", "inference_retry_wait"}:
                raise ConcurrentTurnUpdateError(
                    f"Active inference Turn {turn_id} has state {values[1]}"
                )
            if not isinstance(values[2], datetime):
                raise TypeError("Reset Turn updated_at must be a datetime")
            turn_rows[turn_id] = values

        reservation_rows = cast(
            Sequence[object],
            await db_connection.fetch_all(
                "SELECT billing.turn_id FROM assistant.billing_reservations AS billing "
                "JOIN conversation.conversation_turns AS turn "
                "ON turn.turn_id = billing.turn_id "
                "WHERE turn.conversation_id = %s AND billing.status = 'reserved' "
                "ORDER BY billing.turn_id FOR UPDATE OF billing",
                (str(conversation_id),),
                connection=connection,
            ),
        )
        reserved_turn_ids = {
            TurnId.parse(str(cast(Sequence[object], row)[0]))
            for row in reservation_rows
        }
        if not reserved_turn_ids.issubset(turn_ids):
            raise ConcurrentTurnUpdateError(
                "A reset found a reserved billing row without an active inference activity"
            )

        for turn_id in turn_ids:
            activity_values = activity_by_turn[turn_id]
            activity_id = str(activity_values[0])
            turn_updated_at = cast(datetime, turn_rows[turn_id][2])
            transition_at = max(timestamp, ensure_utc(turn_updated_at))
            activity_count = await db_connection.execute(
                "UPDATE conversation.inference_activities "
                "SET status = 'cancelled', version = version + 1, "
                "next_attempt_at = NULL, claim_token = NULL, lease_expires_at = NULL, "
                "completion_token = NULL, updated_at = %s "
                "WHERE activity_id = CAST(%s AS UUID) "
                "AND status IN ('pending', 'processing', 'retry')",
                (transition_at, activity_id),
                connection=connection,
            )
            if activity_count != 1:
                raise ConcurrentTurnUpdateError(
                    f"Inference activity {activity_id} changed while row-locked"
                )
            turn_count = await db_connection.execute(
                "UPDATE conversation.conversation_turns "
                "SET state = 'cancelled', version = version + 1, "
                "next_retry_at = NULL, last_error = NULL, updated_at = %s, "
                "completed_at = %s WHERE turn_id = CAST(%s AS UUID) "
                "AND state IN ('waiting_inference', 'inference_retry_wait')",
                (transition_at, transition_at, str(turn_id)),
                connection=connection,
            )
            if turn_count != 1:
                raise ConcurrentTurnUpdateError(
                    f"Turn {turn_id} changed while row-locked during reset"
                )
            await self._billing.release(
                connection,
                turn_id=turn_id,
                released_at=transition_at,
            )

    @staticmethod
    async def _find(
        source: TurnSource,
        *,
        connection: AsyncConnection,
    ) -> Sequence[object] | None:
        """@brief 按 namespaced source 读取 reset / Read a reset by namespaced source.

        @param source reset 来源 / Reset source.
        @param connection 当前事务 / Current transaction.
        @return reset 行或 None / Reset row or None.
        """

        return cast(
            Sequence[object] | None,
            await db_connection.fetch_one(
                "SELECT conversation_id, source_update_id, through_sequence, created_at "
                "FROM conversation.conversation_history_resets "
                "WHERE source_kind = %s AND source_key = %s FOR UPDATE",
                (source.kind, source.key),
                connection=connection,
            ),
        )

    @staticmethod
    def _validate_existing(
        command: ResetConversation,
        row: Sequence[object],
    ) -> int:
        """@brief 验证 replay 指向同一 reset 事实 / Validate that a replay denotes the same reset fact.

        @param command replay 命令 / Replay command.
        @param row 规范数据库行 / Canonical database row.
        @return 已存 through sequence / Stored through sequence.
        @raise IdempotencyConflictError source 被不同语义占用 / The source is occupied by different semantics.
        """

        if len(row) != 4:
            raise RuntimeError(f"Expected 4 reset columns, received {len(row)}")
        source_update_id = None if row[1] is None else UpdateId(int(str(row[1])))
        created_at = row[3]
        if not isinstance(created_at, datetime):
            raise TypeError("Conversation reset created_at must be a datetime")
        if (
            str(row[0]) != str(command.conversation_id)
            or source_update_id != command.source.update_id
            or ensure_utc(created_at) != command.requested_at
        ):
            raise IdempotencyConflictError(
                "Conversation reset source already denotes a different command"
            )
        return int(str(row[2]))


__all__ = ["PostgresConversationResetUoW"]
