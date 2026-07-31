"""@brief PostgreSQL durable inbox adapter / PostgreSQL durable-inbox adapter."""

from __future__ import annotations

from datetime import datetime, timedelta

from fogmoe_bot.domain.conversation.errors import IdempotencyConflictError
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    LeaseToken,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import (
    InboxClaim,
    InboxDeadLettered,
    InboxItem,
    InboxRetryScheduled,
    InboxStatus,
    InboxSucceeded,
    InboundUpdate,
)
from fogmoe_bot.domain.observability.trace import TraceContext
from fogmoe_bot.domain.temporal import ensure_utc
from fogmoe_bot.infrastructure.database import db

from .common import (
    _claim_window,
    _datetime,
    _encode_json,
    _integer,
    _json_object,
    _optional_datetime,
    _optional_text,
    _require_claim_update,
    _row_values,
    _text,
)


def _map_inbox_item(row: object) -> InboxItem:
    """@brief 将数据库行恢复为 inbox 聚合 / Restore an inbox aggregate from a database row."""

    values = _row_values(row, 12)
    update = InboundUpdate.pending(
        update_id=UpdateId(_integer(values[0])),
        conversation_id=ConversationId(_text(values[1])),
        payload=_json_object(values[2]),
        received_at=_datetime(values[7]),
        trace_context=TraceContext.parse(_text(values[11])),
    )
    return InboxItem.restore(
        update=update,
        status=InboxStatus(_text(values[3])),
        version=_integer(values[4]),
        attempt_count=_integer(values[5]),
        next_attempt_at=_optional_datetime(values[6]),
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
        @raise IdempotencyConflictError 相同 Update ID 的语义不同时抛出 / Raised when the same Update ID has different semantics.
        """

        item = InboxItem.receive(update)

        async with db.transaction() as connection:
            row = await db.fetch_one(
                "INSERT INTO conversation.inbound_updates "
                "(update_id, conversation_id, payload, status, version, attempt_count, "
                "next_attempt_at, received_at, updated_at, traceparent) "
                "VALUES (%s, %s, CAST(%s AS JSONB), %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
                (
                    int(update.update_id),
                    str(update.conversation_id),
                    _encode_json(update.payload),
                    item.status.value,
                    item.version,
                    item.attempt_count,
                    item.next_attempt_at,
                    update.received_at,
                    item.updated_at,
                    update.trace_context.to_traceparent(),
                ),
                connection=connection,
            )
            if row is not None:
                return True

            existing_row = await db.fetch_one(
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
            existing = _map_inbox_item(existing_row)
            if not existing.update.is_replay_of(update):
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
    ) -> tuple[InboxClaim, ...]:
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
        async with db.transaction() as connection:
            rows = await db.fetch_all(
                "WITH candidates AS ("
                "SELECT candidate.update_id, candidate.status AS previous_status, "
                "candidate.version AS previous_version, "
                "candidate.attempt_count AS previous_attempt_count, "
                "candidate.next_attempt_at AS previous_next_attempt_at, "
                "candidate.updated_at AS previous_updated_at, "
                "candidate.processed_at AS previous_processed_at, "
                "candidate.last_error AS previous_last_error "
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
                "inbound.processed_at, inbound.last_error, inbound.traceparent, "
                "candidates.previous_status, candidates.previous_version, "
                "candidates.previous_attempt_count, candidates.previous_next_attempt_at, "
                "candidates.previous_updated_at, candidates.previous_processed_at, "
                "candidates.previous_last_error",
                (timestamp, limit, str(token), lease_expires_at, timestamp),
                connection=connection,
            )

        claims: list[InboxClaim] = []
        for row in rows:
            values = _row_values(row, 19)
            persisted_processing = _map_inbox_item(values[:12])
            previous = InboxItem.restore(
                update=persisted_processing.update,
                status=InboxStatus(_text(values[12])),
                version=_integer(values[13]),
                attempt_count=_integer(values[14]),
                next_attempt_at=_optional_datetime(values[15]),
                updated_at=_datetime(values[16]),
                processed_at=_optional_datetime(values[17]),
                last_error=_optional_text(values[18]),
            )
            claim = previous.claim(
                token=token,
                claimed_at=timestamp,
                lease_expires_at=lease_expires_at,
            )
            if claim.item != persisted_processing:
                raise RuntimeError(
                    "Inbox claim SQL diverged from the domain claim transition"
                )
            claims.append(claim)
        return tuple(sorted(claims, key=lambda claim: int(claim.item.update.update_id)))

    async def complete_inbound(
        self,
        decision: InboxSucceeded,
    ) -> None:
        """@brief 原子持久化成功领域决定 / Atomically persist a successful domain decision.

        @param decision 已验证成功决定 / Validated success decision.
        @return None / None.
        @raise StaleClaimError recovery/reclaim 后 token 已非当前 owner 时抛出 /
            Raised when recovery or reclaim means the token no longer identifies the current owner.
        """

        claim = decision.claim
        target = decision.item
        rowcount = await db.execute(
            "UPDATE conversation.inbound_updates "
            "SET status = 'processed', version = %s, processed_at = %s, "
            "updated_at = %s, next_attempt_at = NULL, claim_token = NULL, "
            "lease_expires_at = NULL, last_error = NULL "
            "WHERE update_id = %s AND status = 'processing' "
            "AND version = %s AND claim_token = CAST(%s AS UUID)",
            (
                target.version,
                target.processed_at,
                target.updated_at,
                int(claim.item.update.update_id),
                claim.expected_version,
                str(claim.token),
            ),
        )
        _require_claim_update(
            rowcount,
            "inbound",
            str(int(claim.item.update.update_id)),
        )

    async def schedule_inbound_retry(
        self,
        decision: InboxRetryScheduled,
    ) -> None:
        """@brief 原子持久化重试领域决定 / Atomically persist a retry domain decision.

        @param decision 已验证重试决定 / Validated retry decision.
        @return None / None.
        @raise StaleClaimError recovery/reclaim 后 token 已非当前 owner 时抛出 /
            Raised when recovery or reclaim means the token no longer identifies the current owner.
        """

        claim = decision.claim
        target = decision.item
        rowcount = await db.execute(
            "UPDATE conversation.inbound_updates "
            "SET status = 'retry_wait', version = %s, next_attempt_at = %s, "
            "updated_at = %s, claim_token = NULL, lease_expires_at = NULL, "
            "last_error = %s "
            "WHERE update_id = %s AND status = 'processing' "
            "AND version = %s AND claim_token = CAST(%s AS UUID)",
            (
                target.version,
                target.next_attempt_at,
                target.updated_at,
                target.last_error,
                int(claim.item.update.update_id),
                claim.expected_version,
                str(claim.token),
            ),
        )
        _require_claim_update(
            rowcount,
            "inbound",
            str(int(claim.item.update.update_id)),
        )

    async def dead_letter_inbound(
        self,
        decision: InboxDeadLettered,
    ) -> None:
        """@brief 原子持久化 dead-letter 领域决定 / Atomically persist a dead-letter domain decision.

        @param decision 已验证最终失败决定 / Validated final-failure decision.
        @return None / None.
        @raise StaleClaimError recovery/reclaim 后 token 已非当前 owner 时抛出 /
            Raised when recovery or reclaim means the token no longer identifies the current owner.
        """

        claim = decision.claim
        target = decision.item
        rowcount = await db.execute(
            "UPDATE conversation.inbound_updates "
            "SET status = 'failed_final', version = %s, next_attempt_at = NULL, "
            "updated_at = %s, claim_token = NULL, lease_expires_at = NULL, "
            "last_error = %s WHERE update_id = %s AND status = 'processing' "
            "AND version = %s AND claim_token = CAST(%s AS UUID)",
            (
                target.version,
                target.updated_at,
                target.last_error,
                int(claim.item.update.update_id),
                claim.expected_version,
                str(claim.token),
            ),
        )
        _require_claim_update(
            rowcount,
            "inbound",
            str(int(claim.item.update.update_id)),
        )

    async def recover_expired_inbound_leases(self, *, now: datetime) -> int:
        """@brief 回收崩溃 worker 遗留的 inbox 租约 / Recover inbox leases stranded by crashed workers.

        @param now 当前 UTC 时间 / Current UTC time.
        @return inbox 回收数量 / Recovered inbox count.
        """

        timestamp = ensure_utc(now)
        rowcount = await db.execute(
            "UPDATE conversation.inbound_updates "
            "SET status = 'retry_wait', version = version + 1, next_attempt_at = %s, "
            "updated_at = %s, claim_token = NULL, lease_expires_at = NULL, "
            "last_error = COALESCE(last_error, 'recovered expired worker lease') "
            "WHERE status = 'processing' AND lease_expires_at <= %s",
            (timestamp, timestamp, timestamp),
        )
        return _integer(rowcount)


__all__ = ["PostgresInboxRepository"]
