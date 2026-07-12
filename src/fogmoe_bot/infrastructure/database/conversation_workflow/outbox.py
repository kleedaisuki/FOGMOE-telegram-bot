"""@brief PostgreSQL transactional outbox adapter / PostgreSQL transactional-outbox adapter."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    LeaseToken,
    MessageSequence,
    OutboundMessageId,
    TurnId,
)
from fogmoe_bot.domain.conversation.temporal import ensure_utc
from fogmoe_bot.domain.conversation.turn import (
    ConversationTurn,
    TurnEvent,
    TurnState,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundClaim,
    OutboundDraft,
    OutboundEnqueueResult,
    OutboundKind,
    OutboundMessage,
    OutboundStatus,
)
from fogmoe_bot.domain.conversation.errors import (
    ConcurrentTurnUpdateError,
    IdempotencyConflictError,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import (
    _claim_window,
    _datetime,
    _encode_json,
    _integer,
    _json_object,
    _optional_datetime,
    _optional_text,
    _require_claim_update,
    _required_error,
    _row_values,
    _text,
    _uuid,
)
from .turn_uow import (
    _load_turn_for_mutation,
    _persist_turn,
)


def _map_outbound(row: object) -> OutboundMessage:
    """@brief 将数据库行映射为 outbox 消息 / Map a database row to an outbox message."""

    values = _row_values(row, 17)
    draft = OutboundDraft(
        message_id=OutboundMessageId.parse(_uuid(values[0])),
        conversation_id=ConversationId(_text(values[1])),
        turn_id=(TurnId.parse(_uuid(values[2])) if values[2] is not None else None),
        delivery_stream_id=DeliveryStreamId(_text(values[3])),
        kind=OutboundKind(_text(values[5])),
        payload=_json_object(values[6]),
        idempotency_key=_text(values[7]),
        created_at=_datetime(values[12]),
    )
    return OutboundMessage(
        draft=draft,
        stream_sequence=MessageSequence(_integer(values[4])),
        status=OutboundStatus(_text(values[8])),
        version=_integer(values[9]),
        attempt_count=_integer(values[10]),
        next_attempt_at=_optional_datetime(values[11]),
        updated_at=_datetime(values[13]),
        delivered_at=_optional_datetime(values[14]),
        external_message_id=_optional_text(values[15]),
        last_error=_optional_text(values[16]),
    )


def _validate_outbound_idempotency(
    existing: OutboundMessage,
    requested: OutboundDraft,
) -> None:
    """@brief 验证重复 outbox 副作用语义一致 / Verify duplicate outbox-effect semantics."""

    if (
        existing.message_id != requested.message_id
        or existing.conversation_id != requested.conversation_id
        or existing.turn_id != requested.turn_id
        or existing.delivery_stream_id != requested.delivery_stream_id
        or existing.kind != requested.kind
        or existing.payload != requested.payload
        or existing.idempotency_key != requested.idempotency_key
    ):
        raise IdempotencyConflictError(
            f"Outbound {requested.message_id} or idempotency key was reused with different semantics"
        )


class StandaloneOutboxWriter(Protocol):
    """@brief 跨 bounded context 同事务出站所需最窄端口 / Narrow same-transaction outbound port for cross-context callers."""

    async def enqueue_standalone_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 在调用方事务写入 standalone outbound / Enqueue a standalone outbound in the caller transaction."""

        ...


class PostgresOutboxRepository:
    """@brief 拥有 outbox 排序、领取、fencing 与终态 / Own outbox ordering, claims, fencing, and terminal states."""

    async def enqueue_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 在调用方事务写入 Turn-owned outbound / Enqueue a Turn-owned outbound in the caller transaction."""

        if draft.turn_id is None:
            raise ValueError("Turn outbound draft must reference a Turn")
        return await self._enqueue_outbound(draft, connection=connection)

    async def require_existing_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
        *,
        operation: str,
    ) -> OutboundEnqueueResult:
        """@brief 在调用方事务读取组合操作的规范 outbound / Load a composite operation's canonical outbound in the caller transaction."""

        return await self._require_existing_outbound(
            draft,
            operation=operation,
            connection=connection,
        )

    async def enqueue_standalone_outbound(
        self,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 以短事务幂等写入无 Turn 副作用 / Idempotently enqueue a standalone effect in a short transaction.

        @param draft ``turn_id=None`` 且 ID 确定的出站草稿 / Outbound draft with ``turn_id=None`` and a deterministic ID.
        @return 规范 outbox 消息与插入标志 / Canonical outbox message and insertion flag.
        @raise ValueError 草稿不是规范 standalone 副作用时抛出 / Raised when the draft is not a canonical standalone effect.
        """

        async with db_connection.transaction() as connection:
            return await self.enqueue_standalone_outbound_in_transaction(
                connection,
                draft,
            )

    async def enqueue_standalone_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 在调用方事务内写入无 Turn 副作用 / Enqueue a standalone effect inside the caller's transaction.

        @param connection 调用方拥有的活动事务连接 / Active caller-owned transaction connection.
        @param draft 无 Turn 的确定性出站草稿 / Deterministic outbound draft without a Turn.
        @return 规范 outbox 消息与插入标志 / Canonical outbox message and insertion flag.
        @raise ValueError 草稿携带 Turn 或非确定性 ID 时抛出 / Raised when the draft carries a Turn or a non-canonical ID.
        @note 本方法不打开、提交或回滚事务 / This method does not open, commit, or roll back a transaction.
        """

        if draft.turn_id is not None:
            raise ValueError("Standalone outbound draft cannot reference a Turn")
        expected_message_id = OutboundMessageId.for_conversation(
            draft.conversation_id,
            draft.idempotency_key,
        )
        if draft.message_id != expected_message_id:
            raise ValueError(
                "Standalone outbound draft must use its deterministic conversation ID"
            )
        return await self._enqueue_outbound(draft, connection=connection)

    async def _enqueue_outbound(
        self,
        draft: OutboundDraft,
        *,
        connection: AsyncConnection,
    ) -> OutboundEnqueueResult:
        """@brief 在调用方事务内幂等写入 outbox / Idempotently enqueue outbox data in the caller transaction.

        @param draft 尚未分配投递流序号的副作用 / Effect awaiting a delivery-stream sequence.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return 规范消息与插入标志 / Canonical message and insertion flag.
        @raise IdempotencyConflictError ID 已指向其他副作用时抛出 / Raised when an ID already denotes another effect.
        """

        existing_row = await self._find_outbound(draft, connection=connection)
        if existing_row is not None:
            existing = _map_outbound(existing_row)
            _validate_outbound_idempotency(existing, draft)
            return OutboundEnqueueResult(existing, False)

        await db_connection.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"outbox-idempotency:{draft.conversation_id}",),
            connection=connection,
        )
        await db_connection.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (str(draft.delivery_stream_id),),
            connection=connection,
        )
        existing_row = await self._find_outbound(draft, connection=connection)
        if existing_row is not None:
            existing = _map_outbound(existing_row)
            _validate_outbound_idempotency(existing, draft)
            return OutboundEnqueueResult(existing, False)

        sequence_row = await db_connection.fetch_one(
            "SELECT COALESCE(MAX(stream_sequence), 0) + 1 "
            "FROM conversation.outbound_messages WHERE delivery_stream_id = %s",
            (str(draft.delivery_stream_id),),
            connection=connection,
        )
        if sequence_row is None:
            raise RuntimeError(
                "Could not allocate an outbound delivery-stream sequence"
            )
        stream_sequence = _integer(sequence_row[0])
        row = await db_connection.fetch_one(
            "INSERT INTO conversation.outbound_messages "
            "(message_id, conversation_id, turn_id, delivery_stream_id, stream_sequence, "
            "kind, payload, idempotency_key, status, version, attempt_count, "
            "next_attempt_at, created_at, updated_at) "
            "VALUES (CAST(%s AS UUID), %s, CAST(%s AS UUID), %s, %s, %s, "
            "CAST(%s AS JSONB), %s, 'pending', 0, 0, %s, %s, %s) "
            "RETURNING message_id, conversation_id, turn_id, delivery_stream_id, "
            "stream_sequence, kind, payload, idempotency_key, status, version, "
            "attempt_count, next_attempt_at, created_at, updated_at, delivered_at, "
            "external_message_id, last_error",
            (
                str(draft.message_id),
                str(draft.conversation_id),
                str(draft.turn_id) if draft.turn_id is not None else None,
                str(draft.delivery_stream_id),
                stream_sequence,
                draft.kind.value,
                _encode_json(draft.payload),
                draft.idempotency_key,
                draft.created_at,
                draft.created_at,
                draft.created_at,
            ),
            connection=connection,
        )
        if row is None:
            raise RuntimeError("Outbound insert returned no canonical row")
        return OutboundEnqueueResult(_map_outbound(row), True)

    async def _require_existing_outbound(
        self,
        draft: OutboundDraft,
        *,
        operation: str,
        connection: AsyncConnection,
    ) -> OutboundEnqueueResult:
        """@brief 为已提交组合操作读取规范 outbox 消息 / Load the canonical outbox message for an already committed composite operation.

        @param draft 重放的 outbox 草稿 / Replayed outbox draft.
        @param operation 组合操作名 / Composite-operation name.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return inserted=False 的规范结果 / Canonical result with inserted=False.
        @raise ConcurrentTurnUpdateError 回合已推进但 outbox 缺失时抛出 / Raised when the turn advanced but its outbox row is missing.
        """

        row = await self._find_outbound(draft, connection=connection)
        if row is None:
            raise ConcurrentTurnUpdateError(
                f"Turn advanced through {operation} without its atomic outbox message"
            )
        existing = _map_outbound(row)
        _validate_outbound_idempotency(existing, draft)
        return OutboundEnqueueResult(existing, False)

    async def _find_outbound(
        self, draft: OutboundDraft, *, connection: AsyncConnection
    ) -> object | None:
        """@brief 通过实体 ID 或幂等键寻找 outbox 消息 / Find an outbox message by entity ID or idempotency key.

        @param draft 待匹配副作用 / Effect to match.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return 数据库行或 None / Database row or None.
        """

        row: object | None = await db_connection.fetch_one(
            "SELECT message_id, conversation_id, turn_id, delivery_stream_id, "
            "stream_sequence, kind, payload, idempotency_key, status, version, "
            "attempt_count, next_attempt_at, created_at, updated_at, delivered_at, "
            "external_message_id, last_error FROM conversation.outbound_messages "
            "WHERE message_id = CAST(%s AS UUID) "
            "OR (conversation_id = %s AND idempotency_key = %s) LIMIT 1",
            (str(draft.message_id), str(draft.conversation_id), draft.idempotency_key),
            connection=connection,
        )
        return row

    async def get_outbound(
        self, message_id: OutboundMessageId
    ) -> OutboundMessage | None:
        """@brief 读取 outbox 消息 / Load an outbox message.

        @param message_id 出站消息 ID / Outbound-message identifier.
        @return 消息或 None / Message or None.
        """

        row = await db_connection.fetch_one(
            "SELECT message_id, conversation_id, turn_id, delivery_stream_id, "
            "stream_sequence, kind, payload, idempotency_key, status, version, "
            "attempt_count, next_attempt_at, created_at, updated_at, delivered_at, "
            "external_message_id, last_error "
            "FROM conversation.outbound_messages WHERE message_id = CAST(%s AS UUID)",
            (str(message_id),),
        )
        return _map_outbound(row) if row is not None else None

    async def claim_outbound(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[OutboundClaim, ...]:
        """@brief 以有界批次和 SKIP LOCKED 领取 outbox / Claim a bounded outbox batch with SKIP LOCKED.

        @param now 当前 UTC 时间 / Current UTC time.
        @param limit 最大领取数 / Maximum number of claims.
        @param lease_for 租约时长 / Lease duration.
        @return 领取凭证元组 / Tuple of claim receipts.
        @note NOT EXISTS 头部谓词确保同一外部投递流不会越过更早的未决消息 /
        The NOT EXISTS head predicate prevents a delivery stream from overtaking an earlier unsettled message.
        """

        timestamp, lease_expires_at = _claim_window(now, limit, lease_for)
        if limit < 1:
            return ()
        token = LeaseToken.new()
        async with db_connection.transaction() as connection:
            rows = await db_connection.fetch_all(
                "WITH candidates AS ("
                "SELECT candidate.message_id, candidate.status AS previous_status "
                "FROM conversation.outbound_messages AS candidate "
                "WHERE candidate.status IN ('pending', 'retry_wait') "
                "AND candidate.next_attempt_at <= %s "
                "AND NOT EXISTS ("
                "SELECT 1 FROM conversation.outbound_messages AS earlier "
                "WHERE earlier.delivery_stream_id = candidate.delivery_stream_id "
                "AND earlier.status IN ('pending', 'processing', 'retry_wait') "
                "AND earlier.stream_sequence < candidate.stream_sequence"
                ") "
                "ORDER BY candidate.next_attempt_at ASC, candidate.delivery_stream_id ASC, "
                "candidate.stream_sequence ASC LIMIT %s "
                "FOR UPDATE OF candidate SKIP LOCKED"
                ") "
                "UPDATE conversation.outbound_messages AS outbound "
                "SET status = 'processing', version = outbound.version + 1, "
                "attempt_count = outbound.attempt_count + 1, next_attempt_at = NULL, "
                "claim_token = CAST(%s AS UUID), lease_expires_at = %s, "
                "updated_at = %s, last_error = NULL "
                "FROM candidates WHERE outbound.message_id = candidates.message_id "
                "RETURNING outbound.message_id, outbound.conversation_id, outbound.turn_id, "
                "outbound.delivery_stream_id, outbound.stream_sequence, outbound.kind, "
                "outbound.payload, outbound.idempotency_key, outbound.status, outbound.version, "
                "outbound.attempt_count, outbound.next_attempt_at, outbound.created_at, "
                "outbound.updated_at, outbound.delivered_at, outbound.external_message_id, "
                "outbound.last_error, candidates.previous_status",
                (timestamp, limit, str(token), lease_expires_at, timestamp),
                connection=connection,
            )
            claims: list[OutboundClaim] = []
            for row in rows:
                values = _row_values(row, 18)
                message = _map_outbound(values[:17])
                previous_status = OutboundStatus(_text(values[17]))
                if message.turn_id is not None:
                    turn = await _load_turn_for_mutation(
                        message.turn_id,
                        connection=connection,
                    )
                    if previous_status is OutboundStatus.PENDING:
                        if turn.state is not TurnState.WAITING_DELIVERY:
                            raise ConcurrentTurnUpdateError(
                                f"Pending outbound {message.message_id} requires a "
                                f"waiting_delivery turn, found {turn.state.value}"
                            )
                    elif previous_status is OutboundStatus.RETRY_WAIT:
                        if turn.state is not TurnState.DELIVERY_RETRY_WAIT:
                            raise ConcurrentTurnUpdateError(
                                f"Retry outbound {message.message_id} requires a "
                                f"delivery_retry_wait turn, found {turn.state.value}"
                            )
                        resumed = turn.transition(
                            TurnEvent.RETRY_DELIVERY,
                            occurred_at=timestamp,
                        )
                        await _persist_turn(
                            resumed,
                            expected_version=turn.version,
                            connection=connection,
                        )
                    else:
                        raise RuntimeError(
                            "Claim query returned unsupported previous status "
                            f"{previous_status.value}"
                        )
                claims.append(
                    OutboundClaim(
                        message=message,
                        token=token,
                        lease_expires_at=lease_expires_at,
                    )
                )

        return tuple(
            sorted(
                claims,
                key=lambda claim: (
                    str(claim.message.delivery_stream_id),
                    int(claim.message.stream_sequence),
                ),
            )
        )

    async def mark_outbound_delivered(
        self,
        claim: OutboundClaim,
        *,
        delivered_at: datetime,
        external_message_id: str | None,
    ) -> None:
        """@brief 用 fencing token 标记 outbox 投递成功 / Mark outbox delivery successful with its fencing token.

        @param claim 领取凭证 / Claim receipt.
        @param delivered_at 成功时间 / Success time.
        @param external_message_id 外部消息 ID / External message identifier.
        @return None / None.
        @raise StaleClaimError token 已失效时抛出 / Raised when the token is stale.
        """

        timestamp = ensure_utc(delivered_at)
        if timestamp < claim.message.updated_at:
            raise ValueError("delivered_at cannot precede claim time")
        async with db_connection.transaction() as connection:
            rowcount = await db_connection.execute(
                "UPDATE conversation.outbound_messages "
                "SET status = 'delivered', version = version + 1, delivered_at = %s, "
                "external_message_id = %s, updated_at = %s, next_attempt_at = NULL, "
                "claim_token = NULL, lease_expires_at = NULL, last_error = NULL "
                "WHERE message_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND claim_token = CAST(%s AS UUID)",
                (
                    timestamp,
                    external_message_id,
                    timestamp,
                    str(claim.message.message_id),
                    str(claim.token),
                ),
                connection=connection,
            )
            _require_claim_update(
                rowcount,
                "outbound",
                str(claim.message.message_id),
            )
            await self._transition_delivery_turn(
                claim,
                event=TurnEvent.DELIVERY_SUCCEEDED,
                occurred_at=timestamp,
                connection=connection,
            )

    async def retry_outbound(
        self,
        claim: OutboundClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 安排 outbox 重试 / Schedule an outbox retry.

        @param claim 领取凭证 / Claim receipt.
        @param failed_at 本次失败时间 / Failure time for this attempt.
        @param retry_at 下次尝试时间 / Next attempt time.
        @param error 失败原因 / Failure reason.
        @return None / None.
        @raise StaleClaimError token 已失效时抛出 / Raised when the token is stale.
        """

        failure_time = ensure_utc(failed_at)
        retry_time = ensure_utc(retry_at)
        if failure_time < claim.message.updated_at:
            raise ValueError("failed_at cannot precede claim time")
        if retry_time <= failure_time:
            raise ValueError("retry_at must be later than failed_at")
        normalized_error = _required_error(error)
        async with db_connection.transaction() as connection:
            rowcount = await db_connection.execute(
                "UPDATE conversation.outbound_messages "
                "SET status = 'retry_wait', version = version + 1, next_attempt_at = %s, "
                "updated_at = %s, claim_token = NULL, lease_expires_at = NULL, "
                "last_error = %s WHERE message_id = CAST(%s AS UUID) "
                "AND status = 'processing' AND claim_token = CAST(%s AS UUID)",
                (
                    retry_time,
                    failure_time,
                    normalized_error,
                    str(claim.message.message_id),
                    str(claim.token),
                ),
                connection=connection,
            )
            _require_claim_update(
                rowcount,
                "outbound",
                str(claim.message.message_id),
            )
            await self._transition_delivery_turn(
                claim,
                event=TurnEvent.SCHEDULE_DELIVERY_RETRY,
                occurred_at=failure_time,
                retry_at=retry_time,
                error=normalized_error,
                connection=connection,
            )

    async def fail_outbound(
        self,
        claim: OutboundClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 将 outbox 消息标记最终失败 / Mark an outbox message finally failed.

        @param claim 领取凭证 / Claim receipt.
        @param failed_at 最终失败时间 / Final failure time.
        @param error 最终失败原因 / Final failure reason.
        @return None / None.
        @raise StaleClaimError token 已失效时抛出 / Raised when the token is stale.
        """

        failure_time = ensure_utc(failed_at)
        if failure_time < claim.message.updated_at:
            raise ValueError("failed_at cannot precede claim time")
        normalized_error = _required_error(error)
        async with db_connection.transaction() as connection:
            rowcount = await db_connection.execute(
                "UPDATE conversation.outbound_messages "
                "SET status = 'failed_final', version = version + 1, next_attempt_at = NULL, "
                "updated_at = %s, claim_token = NULL, lease_expires_at = NULL, "
                "last_error = %s WHERE message_id = CAST(%s AS UUID) "
                "AND status = 'processing' AND claim_token = CAST(%s AS UUID)",
                (
                    failure_time,
                    normalized_error,
                    str(claim.message.message_id),
                    str(claim.token),
                ),
                connection=connection,
            )
            _require_claim_update(
                rowcount,
                "outbound",
                str(claim.message.message_id),
            )
            await self._transition_delivery_turn(
                claim,
                event=TurnEvent.FAIL_FINAL,
                occurred_at=failure_time,
                error=normalized_error,
                connection=connection,
            )

    async def _transition_delivery_turn(
        self,
        claim: OutboundClaim,
        *,
        event: TurnEvent,
        occurred_at: datetime,
        retry_at: datetime | None = None,
        error: str | None = None,
        connection: AsyncConnection,
    ) -> ConversationTurn | None:
        """@brief 在 outbox 事务内推进可选关联回合 / Advance the optional associated Turn in the outbox transaction.

        @param claim outbox 领取凭证 / Outbox claim receipt.
        @param event 投递领域事件 / Delivery domain event.
        @param occurred_at 事件时间 / Event time.
        @param retry_at 可选重试时间 / Optional retry time.
        @param error 可选错误 / Optional error.
        @param connection 当前短事务连接 / Current short-transaction connection.
        @return 更新后的回合；standalone 副作用为 None / Updated Turn, or None for a standalone effect.
        @raise ConcurrentTurnUpdateError 回合不在 WAITING_DELIVERY 时抛出 / Raised when the turn is not WAITING_DELIVERY.
        """

        turn_id = claim.message.turn_id
        if turn_id is None:
            return None
        turn = await _load_turn_for_mutation(
            turn_id,
            connection=connection,
        )
        if turn.state is not TurnState.WAITING_DELIVERY:
            raise ConcurrentTurnUpdateError(
                f"Outbound {claim.message.message_id} requires a waiting_delivery turn, "
                f"found {turn.state.value}"
            )
        updated = turn.transition(
            event,
            occurred_at=occurred_at,
            retry_at=retry_at,
            error=error,
        )
        await _persist_turn(
            updated,
            expected_version=turn.version,
            connection=connection,
        )
        return updated

    async def recover_expired_outbound_leases(self, *, now: datetime) -> int:
        """@brief 回收崩溃 worker 遗留的 outbox 租约 / Recover outbox leases stranded by crashed workers.

        @param now 当前 UTC 时间 / Current UTC time.
        @return outbox 回收数量 / Recovered outbox count.
        """

        timestamp = ensure_utc(now)
        retry_time = timestamp + timedelta(microseconds=1)
        recovery_error = "recovered expired worker lease"
        async with db_connection.transaction() as connection:
            rows = await db_connection.fetch_all(
                "WITH expired AS ("
                "SELECT message_id FROM conversation.outbound_messages "
                "WHERE status = 'processing' AND lease_expires_at <= %s "
                "FOR UPDATE SKIP LOCKED"
                ") UPDATE conversation.outbound_messages AS outbound "
                "SET status = 'retry_wait', version = outbound.version + 1, "
                "next_attempt_at = %s, updated_at = %s, claim_token = NULL, "
                "lease_expires_at = NULL, last_error = %s FROM expired "
                "WHERE outbound.message_id = expired.message_id "
                "RETURNING outbound.message_id, outbound.conversation_id, outbound.turn_id, "
                "outbound.delivery_stream_id, outbound.stream_sequence, outbound.kind, "
                "outbound.payload, outbound.idempotency_key, outbound.status, outbound.version, "
                "outbound.attempt_count, outbound.next_attempt_at, outbound.created_at, "
                "outbound.updated_at, outbound.delivered_at, outbound.external_message_id, "
                "outbound.last_error",
                (
                    timestamp,
                    retry_time,
                    timestamp,
                    recovery_error,
                ),
                connection=connection,
            )
            for row in rows:
                message = _map_outbound(row)
                if message.turn_id is None:
                    continue
                turn = await _load_turn_for_mutation(
                    message.turn_id,
                    connection=connection,
                )
                if turn.state is not TurnState.WAITING_DELIVERY:
                    raise ConcurrentTurnUpdateError(
                        f"Expired outbound {message.message_id} requires a "
                        f"waiting_delivery turn, found {turn.state.value}"
                    )
                retrying = turn.transition(
                    TurnEvent.SCHEDULE_DELIVERY_RETRY,
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


__all__ = ["PostgresOutboxRepository", "StandaloneOutboxWriter"]
