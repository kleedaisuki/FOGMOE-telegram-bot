"""@brief PostgreSQL durable inbox adapter / PostgreSQL durable-inbox adapter."""

from __future__ import annotations

from datetime import datetime, timedelta


from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    LeaseToken,
    UpdateId,
)
from fogmoe_bot.domain.conversation.temporal import ensure_utc
from fogmoe_bot.domain.conversation.inbox import (
    InboundClaim,
    InboundStatus,
    InboundUpdate,
)
from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.domain.observability.trace import TraceContext

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
)


def _map_inbound(row: object) -> InboundUpdate:
    """@brief 将数据库行映射为入口实体 / Map a database row to an inbound entity."""

    values = _row_values(row, 12)
    return InboundUpdate(
        update_id=UpdateId(_integer(values[0])),
        conversation_id=ConversationId(_text(values[1])),
        payload=_json_object(values[2]),
        trace_context=TraceContext.parse(_text(values[11])),
        status=InboundStatus(_text(values[3])),
        version=_integer(values[4]),
        attempt_count=_integer(values[5]),
        next_attempt_at=_optional_datetime(values[6]),
        received_at=_datetime(values[7]),
        updated_at=_datetime(values[8]),
        processed_at=_optional_datetime(values[9]),
        last_error=_optional_text(values[10]),
    )


class PostgresInboxRepository:
    """@brief 拥有 inbound Update 生命周期与 fencing / Own the inbound-Update lifecycle and fencing."""

    async def add_inbound(self, update: InboundUpdate) -> bool:
        """@brief 幂等写入入口 Update / Idempotently persist an inbound Update.

        @param update 待写入 Update / Update to persist.
        @return 新插入返回 True，重复返回 False / True when inserted, False for a duplicate.
        @raise ValueError 只允许写入初始 PENDING 实体 / Only an initial PENDING entity may be inserted.
        @raise IdempotencyConflictError 相同 Update ID 的语义不同时抛出 / Raised when the same Update ID has different semantics.
        """

        if (
            update.status is not InboundStatus.PENDING
            or update.version != 0
            or update.attempt_count != 0
        ):
            raise ValueError("add_inbound requires an initial pending Update")

        async with db_connection.transaction() as connection:
            row = await db_connection.fetch_one(
                "INSERT INTO conversation.inbound_updates "
                "(update_id, conversation_id, payload, status, version, attempt_count, "
                "next_attempt_at, received_at, updated_at, traceparent) "
                "VALUES (%s, %s, CAST(%s AS JSONB), %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
                (
                    int(update.update_id),
                    str(update.conversation_id),
                    _encode_json(update.payload),
                    update.status.value,
                    update.version,
                    update.attempt_count,
                    update.next_attempt_at,
                    update.received_at,
                    update.updated_at,
                    update.trace_context.to_traceparent(),
                ),
                connection=connection,
            )
            if row is not None:
                return True

            existing_row = await db_connection.fetch_one(
                "SELECT update_id, conversation_id, payload, status, version, attempt_count, "
                "next_attempt_at, received_at, updated_at, processed_at, last_error, traceparent "
                "FROM conversation.inbound_updates WHERE update_id = %s",
                (int(update.update_id),),
                connection=connection,
            )
            if existing_row is None:
                raise RuntimeError(
                    "Inbound insert conflicted but no canonical row exists"
                )
            existing = _map_inbound(existing_row)
            if (
                existing.conversation_id != update.conversation_id
                or existing.payload != update.payload
            ):
                raise IdempotencyConflictError(
                    f"Update {int(update.update_id)} was reused with different semantics"
                )
            return False

    async def claim_inbound(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[InboundClaim, ...]:
        """@brief 原子领取可处理入口 Update / Atomically claim runnable inbound Updates.

        @param now 当前 UTC 时间 / Current UTC time.
        @param limit 最大领取数 / Maximum number of claims.
        @param lease_for 租约时长 / Lease duration.
        @return 领取凭证元组 / Tuple of claim receipts.
        @note 会话头部谓词禁止多实例越过同一会话的更早 Update /
        A conversation-head predicate prevents multiple instances from overtaking an earlier Update in the same conversation.
        """

        timestamp, lease_expires_at = _claim_window(now, limit, lease_for)
        if limit < 1:
            return ()
        token = LeaseToken.new()
        async with db_connection.transaction() as connection:
            rows = await db_connection.fetch_all(
                "WITH candidates AS ("
                "SELECT candidate.update_id "
                "FROM conversation.inbound_updates AS candidate "
                "WHERE candidate.status IN ('pending', 'retry_wait') "
                "AND candidate.next_attempt_at <= %s "
                "AND NOT EXISTS ("
                "SELECT 1 FROM conversation.inbound_updates AS earlier "
                "WHERE earlier.conversation_id = candidate.conversation_id "
                "AND earlier.status IN ('pending', 'processing', 'retry_wait') "
                "AND earlier.update_id < candidate.update_id"
                ") "
                "ORDER BY candidate.next_attempt_at ASC, candidate.update_id ASC "
                "LIMIT %s FOR UPDATE OF candidate SKIP LOCKED"
                ") "
                "UPDATE conversation.inbound_updates AS inbound "
                "SET status = 'processing', version = inbound.version + 1, "
                "attempt_count = inbound.attempt_count + 1, next_attempt_at = NULL, "
                "claim_token = CAST(%s AS UUID), lease_expires_at = %s, "
                "updated_at = %s, last_error = NULL "
                "FROM candidates WHERE inbound.update_id = candidates.update_id "
                "RETURNING inbound.update_id, inbound.conversation_id, inbound.payload, "
                "inbound.status, inbound.version, inbound.attempt_count, "
                "inbound.next_attempt_at, inbound.received_at, inbound.updated_at, "
                "inbound.processed_at, inbound.last_error, inbound.traceparent",
                (timestamp, limit, str(token), lease_expires_at, timestamp),
                connection=connection,
            )

        claims = tuple(
            InboundClaim(
                update=_map_inbound(row),
                token=token,
                lease_expires_at=lease_expires_at,
            )
            for row in rows
        )
        return tuple(sorted(claims, key=lambda claim: int(claim.update.update_id)))

    async def mark_inbound_processed(
        self,
        claim: InboundClaim,
        *,
        processed_at: datetime,
    ) -> None:
        """@brief 用 fencing token 完成入口 Update / Complete an inbound Update with its fencing token.

        @param claim 领取凭证 / Claim receipt.
        @param processed_at 完成时间 / Completion time.
        @return None / None.
        @raise StaleClaimError token 已过期或被替换时抛出 / Raised when the token expired or was superseded.
        """

        timestamp = ensure_utc(processed_at)
        if timestamp < claim.update.updated_at:
            raise ValueError("processed_at cannot precede claim time")
        rowcount = await db_connection.execute(
            "UPDATE conversation.inbound_updates "
            "SET status = 'processed', version = version + 1, processed_at = %s, "
            "updated_at = %s, next_attempt_at = NULL, claim_token = NULL, "
            "lease_expires_at = NULL, last_error = NULL "
            "WHERE update_id = %s AND status = 'processing' "
            "AND claim_token = CAST(%s AS UUID)",
            (timestamp, timestamp, int(claim.update.update_id), str(claim.token)),
        )
        _require_claim_update(rowcount, "inbound", str(int(claim.update.update_id)))

    async def retry_inbound(
        self,
        claim: InboundClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 安排入口 Update 重试 / Schedule an inbound Update retry.

        @param claim 领取凭证 / Claim receipt.
        @param failed_at 本次失败时间 / Failure time for this attempt.
        @param retry_at 下次尝试时间 / Next attempt time.
        @param error 失败原因 / Failure reason.
        @return None / None.
        @raise StaleClaimError token 已过期或被替换时抛出 / Raised when the token expired or was superseded.
        """

        failure_time = ensure_utc(failed_at)
        retry_time = ensure_utc(retry_at)
        if failure_time < claim.update.updated_at:
            raise ValueError("failed_at cannot precede claim time")
        if retry_time <= failure_time:
            raise ValueError("retry_at must be later than failed_at")
        normalized_error = _required_error(error)
        rowcount = await db_connection.execute(
            "UPDATE conversation.inbound_updates "
            "SET status = 'retry_wait', version = version + 1, next_attempt_at = %s, "
            "updated_at = %s, claim_token = NULL, lease_expires_at = NULL, "
            "last_error = %s "
            "WHERE update_id = %s AND status = 'processing' "
            "AND claim_token = CAST(%s AS UUID)",
            (
                retry_time,
                failure_time,
                normalized_error,
                int(claim.update.update_id),
                str(claim.token),
            ),
        )
        _require_claim_update(rowcount, "inbound", str(int(claim.update.update_id)))

    async def fail_inbound(
        self,
        claim: InboundClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 将入口 Update 标记最终失败 / Mark an inbound Update finally failed.

        @param claim 领取凭证 / Claim receipt.
        @param failed_at 最终失败时间 / Final failure time.
        @param error 最终失败原因 / Final failure reason.
        @return None / None.
        @raise StaleClaimError token 已过期或被替换时抛出 / Raised when the token expired or was superseded.
        """

        failure_time = ensure_utc(failed_at)
        if failure_time < claim.update.updated_at:
            raise ValueError("failed_at cannot precede claim time")
        normalized_error = _required_error(error)
        rowcount = await db_connection.execute(
            "UPDATE conversation.inbound_updates "
            "SET status = 'failed_final', version = version + 1, next_attempt_at = NULL, "
            "updated_at = %s, claim_token = NULL, lease_expires_at = NULL, "
            "last_error = %s WHERE update_id = %s AND status = 'processing' "
            "AND claim_token = CAST(%s AS UUID)",
            (
                failure_time,
                normalized_error,
                int(claim.update.update_id),
                str(claim.token),
            ),
        )
        _require_claim_update(rowcount, "inbound", str(int(claim.update.update_id)))

    async def recover_expired_inbound_leases(self, *, now: datetime) -> int:
        """@brief 回收崩溃 worker 遗留的 inbox 租约 / Recover inbox leases stranded by crashed workers.

        @param now 当前 UTC 时间 / Current UTC time.
        @return inbox 回收数量 / Recovered inbox count.
        """

        timestamp = ensure_utc(now)
        rowcount = await db_connection.execute(
            "UPDATE conversation.inbound_updates "
            "SET status = 'retry_wait', version = version + 1, next_attempt_at = %s, "
            "updated_at = %s, claim_token = NULL, lease_expires_at = NULL, "
            "last_error = COALESCE(last_error, 'recovered expired worker lease') "
            "WHERE status = 'processing' AND lease_expires_at <= %s",
            (timestamp, timestamp, timestamp),
        )
        return _integer(rowcount)


__all__ = ["PostgresInboxRepository"]
