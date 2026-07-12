"""@brief PostgreSQL 会话 retention、compaction queue 与永久记忆投影 / PostgreSQL conversation retention, compaction queue, and permanent-memory projection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.conversation.history_projection import HistoryBounds
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    LeaseToken,
    MessageSequence,
    TurnId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.temporal import ensure_utc
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.retention import (
    RetentionEnqueueResult,
    RetentionIdempotencyConflictError,
    RetentionKind,
    RetentionSegment,
    RetentionSegmentDraft,
    RetentionSegmentId,
    RetentionStatus,
    RetentionSummary,
    StaleRetentionClaimError,
    TokenCount,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


_SEGMENT_COLUMNS = (
    "segment_id, kind, conversation_id, owner_user_id, epoch_floor_sequence, "
    "from_sequence, through_sequence, anchor_turn_id, predecessor_segment_id, "
    "projection_version, source_digest, source_snapshot, source_row_count, "
    "source_token_count, legacy_record_id, status, version, attempt_count, "
    "next_attempt_at, claim_token, lease_expires_at, completion_token, "
    "summary_text, summary_token_count, summary_route_key, last_error, "
    "created_at, updated_at, completed_at"
)
"""@brief Retention Segment 规范 SELECT 列 / Canonical retention-segment SELECT columns."""

_SEGMENT_SELECT = "SELECT " + _SEGMENT_COLUMNS + " FROM conversation.retention_segments"
"""@brief Retention Segment SELECT 前缀 / Retention-segment SELECT prefix."""


class PostgresConversationRetention:
    """@brief 单表实现 history projection、compaction lifecycle 与 quota view / Single-table history projection, compaction lifecycle, and quota view."""

    async def history_bounds(
        self,
        conversation_id: ConversationId,
        *,
        through_turn_id: TurnId,
    ) -> HistoryBounds | None:
        """@brief 读取 anchor Turn 边界及其稳定 reset epoch / Load anchor-Turn bounds and its stable reset epoch.

        @param conversation_id 会话 ID / Conversation identifier.
        @param through_turn_id anchor Turn / Anchor Turn.
        @return bounds；Turn 无消息时为 None / Bounds, or None when the Turn has no messages.
        @note reset 必须严格早于 Turn first sequence；later reset 不改变已接受 Turn。/
        A reset must be strictly earlier than the Turn's first sequence; later resets cannot alter an accepted Turn.
        """

        row = await db_connection.fetch_one(
            "WITH turn_bounds AS ("
            "SELECT MIN(sequence) AS first_sequence, MAX(sequence) AS last_sequence "
            "FROM conversation.conversation_messages "
            "WHERE conversation_id = %s AND turn_id = CAST(%s AS UUID)"
            ") SELECT first_sequence, last_sequence, ("
            "SELECT COALESCE(MAX(history_reset.through_sequence), 0) "
            "FROM conversation.conversation_history_resets AS history_reset "
            "WHERE history_reset.conversation_id = %s "
            "AND turn_bounds.first_sequence IS NOT NULL "
            "AND history_reset.through_sequence < turn_bounds.first_sequence"
            ") AS epoch_floor_sequence FROM turn_bounds",
            (
                str(conversation_id),
                str(through_turn_id),
                str(conversation_id),
            ),
        )
        if row is None or row[0] is None or row[1] is None:
            return None
        return HistoryBounds(
            conversation_id=conversation_id,
            through_turn_id=through_turn_id,
            first_sequence=_integer(row[0]),
            last_sequence=_integer(row[1]),
            epoch_floor_sequence=_integer(row[2]),
        )

    async def latest_completed_compaction(
        self,
        conversation_id: ConversationId,
        *,
        epoch_floor_sequence: int,
        before_sequence: int,
    ) -> RetentionSegment | None:
        """@brief 读取 anchor 前最新累计 checkpoint / Load the latest cumulative checkpoint before an anchor.

        @return completed compaction 或 None / Completed compaction or None.
        """

        if epoch_floor_sequence < 0 or before_sequence <= epoch_floor_sequence:
            raise ValueError("Compaction projection bounds are invalid")
        row = await db_connection.fetch_one(
            _SEGMENT_SELECT + " WHERE kind = 'compaction' AND status = 'completed' "
            "AND conversation_id = %s AND epoch_floor_sequence = %s "
            "AND through_sequence < %s "
            "ORDER BY through_sequence DESC, completed_at DESC, segment_id DESC LIMIT 1",
            (str(conversation_id), epoch_floor_sequence, before_sequence),
        )
        return _map_segment(row) if row is not None else None

    async def active_compaction(
        self,
        conversation_id: ConversationId,
        *,
        epoch_floor_sequence: int,
    ) -> RetentionSegment | None:
        """@brief 读取同 epoch 唯一在途 compaction / Load the sole in-flight compaction for an epoch."""

        if epoch_floor_sequence < 0:
            raise ValueError("Compaction epoch floor cannot be negative")
        row = await db_connection.fetch_one(
            _SEGMENT_SELECT + " WHERE kind = 'compaction' AND conversation_id = %s "
            "AND epoch_floor_sequence = %s "
            "AND status IN ('pending', 'processing', 'retry_wait') LIMIT 1",
            (str(conversation_id), epoch_floor_sequence),
        )
        return _map_segment(row) if row is not None else None

    async def read_messages_page(
        self,
        conversation_id: ConversationId,
        *,
        after_sequence: int,
        through_sequence: int,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        """@brief keyset 分页读取完整 append-only stream / Read the append-only stream using keyset pagination.

        @return sequence 升序 page / Page ordered by ascending sequence.
        """

        if after_sequence < 0 or through_sequence < after_sequence:
            raise ValueError("Message page sequence bounds are invalid")
        if not 1 <= limit <= 1024:
            raise ValueError("Message page limit must be between 1 and 1024")
        rows = await db_connection.fetch_all(
            "SELECT message_id, conversation_id, sequence, turn_id, source_update_id, "
            "role, content, idempotency_key, created_at "
            "FROM conversation.conversation_messages "
            "WHERE conversation_id = %s AND sequence > %s AND sequence <= %s "
            "ORDER BY sequence ASC LIMIT %s",
            (str(conversation_id), after_sequence, through_sequence, limit),
        )
        return tuple(_map_message(row) for row in rows)

    async def enqueue_compaction(
        self,
        draft: RetentionSegmentDraft,
    ) -> RetentionEnqueueResult:
        """@brief 在 epoch advisory lock 下幂等入队 / Idempotently enqueue under an epoch advisory lock.

        @param draft 不可变 compaction source / Immutable compaction source.
        @return 新 Segment 或已存在同 epoch work / New segment or existing work for the epoch.
        @raise RetentionIdempotencyConflictError anchor、predecessor 或同 ID 语义漂移 / Anchor, predecessor, or same-ID semantics drifted.
        """

        if draft.kind is not RetentionKind.COMPACTION:
            raise ValueError("enqueue_compaction requires a compaction draft")
        floor = cast(int, draft.epoch_floor_sequence)
        async with db_connection.transaction() as connection:
            await db_connection.fetch_one(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"conversation-retention:{draft.conversation_id}:{floor}",),
                connection=connection,
            )
            await self._validate_draft_source(draft, connection=connection)
            active_row = await db_connection.fetch_one(
                _SEGMENT_SELECT + " WHERE kind = 'compaction' AND conversation_id = %s "
                "AND epoch_floor_sequence = %s "
                "AND status IN ('pending', 'processing', 'retry_wait') "
                "FOR UPDATE",
                (str(draft.conversation_id), floor),
                connection=connection,
            )
            if active_row is not None:
                active = _map_segment(active_row)
                if active.segment_id == draft.segment_id:
                    _validate_same_draft(active.draft, draft)
                return RetentionEnqueueResult(active, False)

            row = await db_connection.fetch_one(
                "INSERT INTO conversation.retention_segments ("
                "segment_id, kind, conversation_id, owner_user_id, epoch_floor_sequence, "
                "from_sequence, through_sequence, anchor_turn_id, predecessor_segment_id, "
                "projection_version, source_digest, source_snapshot, source_row_count, "
                "source_token_count, legacy_record_id, status, version, attempt_count, "
                "next_attempt_at, created_at, updated_at) VALUES ("
                "CAST(%s AS UUID), %s, %s, %s, %s, %s, %s, CAST(%s AS UUID), "
                "CAST(%s AS UUID), %s, %s, CAST(%s AS JSON), %s, %s, NULL, "
                "'pending', 0, 0, %s, %s, %s) "
                "ON CONFLICT (segment_id) DO NOTHING RETURNING " + _SEGMENT_COLUMNS,
                (
                    str(draft.segment_id),
                    draft.kind.value,
                    str(draft.conversation_id),
                    draft.owner_user_id,
                    draft.epoch_floor_sequence,
                    draft.from_sequence,
                    draft.through_sequence,
                    str(draft.anchor_turn_id),
                    (
                        str(draft.predecessor_segment_id)
                        if draft.predecessor_segment_id is not None
                        else None
                    ),
                    draft.projection_version,
                    draft.source_digest,
                    _encode_snapshot(draft.source_snapshot),
                    draft.source_row_count,
                    int(draft.source_token_count),
                    draft.created_at,
                    draft.created_at,
                    draft.created_at,
                ),
                connection=connection,
            )
            if row is not None:
                return RetentionEnqueueResult(_map_segment(row), True)
            existing_row = await db_connection.fetch_one(
                _SEGMENT_SELECT + " WHERE segment_id = CAST(%s AS UUID) FOR UPDATE",
                (str(draft.segment_id),),
                connection=connection,
            )
            if existing_row is None:
                raise RuntimeError(
                    "Retention segment insert conflicted without a canonical row"
                )
            existing = _map_segment(existing_row)
            _validate_same_draft(existing.draft, draft)
            return RetentionEnqueueResult(existing, False)

    async def _validate_draft_source(
        self,
        draft: RetentionSegmentDraft,
        *,
        connection: AsyncConnection,
    ) -> None:
        """@brief 在 enqueue transaction 验证 anchor epoch、range 与 predecessor / Validate anchor epoch, range, and predecessor in the enqueue transaction.

        @return None / None.
        @raise RetentionIdempotencyConflictError durable source 与 draft 不一致 / Durable source differs from the draft.
        """

        anchor = cast(TurnId, draft.anchor_turn_id)
        floor = cast(int, draft.epoch_floor_sequence)
        start = cast(int, draft.from_sequence)
        end = cast(int, draft.through_sequence)
        row = await db_connection.fetch_one(
            "WITH turn_bounds AS ("
            "SELECT MIN(sequence) AS first_sequence, MAX(sequence) AS last_sequence "
            "FROM conversation.conversation_messages "
            "WHERE conversation_id = %s AND turn_id = CAST(%s AS UUID)"
            ") SELECT first_sequence, last_sequence, ("
            "SELECT COALESCE(MAX(history_reset.through_sequence), 0) "
            "FROM conversation.conversation_history_resets AS history_reset "
            "WHERE history_reset.conversation_id = %s "
            "AND turn_bounds.first_sequence IS NOT NULL "
            "AND history_reset.through_sequence < turn_bounds.first_sequence"
            ") FROM turn_bounds",
            (
                str(draft.conversation_id),
                str(anchor),
                str(draft.conversation_id),
            ),
            connection=connection,
        )
        if (
            row is None
            or row[0] is None
            or _integer(row[2]) != floor
            or end >= _integer(row[0])
        ):
            raise RetentionIdempotencyConflictError(
                "Compaction anchor Turn or reset epoch changed semantics"
            )
        count_row = await db_connection.fetch_one(
            "SELECT COUNT(*) FROM conversation.conversation_messages "
            "WHERE conversation_id = %s AND sequence BETWEEN %s AND %s",
            (str(draft.conversation_id), start, end),
            connection=connection,
        )
        if count_row is None or _integer(count_row[0]) != draft.source_row_count:
            raise RetentionIdempotencyConflictError(
                "Compaction source row count changed semantics"
            )
        if draft.predecessor_segment_id is None:
            if start != floor + 1:
                raise RetentionIdempotencyConflictError(
                    "First compaction segment must begin at the reset epoch floor"
                )
            return
        predecessor_row = await db_connection.fetch_one(
            _SEGMENT_SELECT
            + " WHERE segment_id = CAST(%s AS UUID) AND status = 'completed' "
            "FOR UPDATE",
            (str(draft.predecessor_segment_id),),
            connection=connection,
        )
        if predecessor_row is None:
            raise RetentionIdempotencyConflictError(
                "Compaction predecessor is missing or incomplete"
            )
        predecessor = _map_segment(predecessor_row)
        if (
            predecessor.draft.conversation_id != draft.conversation_id
            or predecessor.draft.epoch_floor_sequence != floor
            or predecessor.draft.through_sequence != start - 1
        ):
            raise RetentionIdempotencyConflictError(
                "Compaction predecessor changed range semantics"
            )

    async def claim_compactions(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[RetentionSegment, ...]:
        """@brief 以 SKIP LOCKED 领取 ready Segments / Claim ready segments using SKIP LOCKED.

        @return 每行带独立 fencing token 的 claims / Claims carrying an independent fencing token per row.
        """

        timestamp = ensure_utc(now)
        if limit < 1:
            return ()
        if lease_for <= timedelta():
            raise ValueError("Compaction lease_for must be positive")
        lease_expires_at = timestamp + lease_for
        claims: list[RetentionSegment] = []
        async with db_connection.transaction() as connection:
            await db_connection.execute(
                "UPDATE conversation.retention_segments SET "
                "status = 'retry_wait', version = version + 1, "
                "next_attempt_at = %s, claim_token = NULL, lease_expires_at = NULL, "
                "updated_at = %s, last_error = COALESCE("
                "last_error, 'recovered expired compaction lease') "
                "WHERE status = 'processing' AND lease_expires_at <= %s",
                (
                    timestamp + timedelta(microseconds=1),
                    timestamp,
                    timestamp,
                ),
                connection=connection,
            )
            candidates = await db_connection.fetch_all(
                "SELECT segment_id FROM conversation.retention_segments "
                "WHERE kind = 'compaction' AND status IN ('pending', 'retry_wait') "
                "AND next_attempt_at <= %s "
                "ORDER BY next_attempt_at ASC, segment_id ASC "
                "LIMIT %s FOR UPDATE SKIP LOCKED",
                (timestamp, limit),
                connection=connection,
            )
            for candidate in candidates:
                token = LeaseToken.new()
                row = await db_connection.fetch_one(
                    "UPDATE conversation.retention_segments "
                    "SET status = 'processing', version = version + 1, "
                    "attempt_count = attempt_count + 1, next_attempt_at = NULL, "
                    "claim_token = CAST(%s AS UUID), lease_expires_at = %s, "
                    "updated_at = %s, last_error = NULL "
                    "WHERE segment_id = CAST(%s AS UUID) "
                    "AND status IN ('pending', 'retry_wait') RETURNING "
                    + _SEGMENT_COLUMNS,
                    (
                        str(token),
                        lease_expires_at,
                        timestamp,
                        str(candidate[0]),
                    ),
                    connection=connection,
                )
                if row is None:
                    raise RuntimeError("Locked compaction candidate was not claimable")
                claims.append(_map_segment(row))
        return tuple(claims)

    async def complete_compaction(
        self,
        claim: RetentionSegment,
        *,
        summary: RetentionSummary,
        completed_at: datetime,
    ) -> RetentionSegment:
        """@brief 以 fencing token 提交 canonical summary / Commit the canonical summary using a fencing token.

        @return completed segment / Completed segment.
        @raise StaleRetentionClaimError token 已替换 / Claim token was superseded.
        """

        token = _claim_token(claim)
        timestamp = ensure_utc(completed_at)
        async with db_connection.transaction() as connection:
            current = await self._load_for_update(
                claim.segment_id,
                connection=connection,
            )
            if current is None:
                raise StaleRetentionClaimError(
                    f"Retention segment {claim.segment_id} no longer exists"
                )
            _validate_same_draft(current.draft, claim.draft)
            if current.status is RetentionStatus.COMPLETED:
                if current.completion_token != token or current.summary != summary:
                    raise StaleRetentionClaimError(
                        f"Stale retention completion for {claim.segment_id}"
                    )
                return current
            if (
                current.status is not RetentionStatus.PROCESSING
                or current.claim_token != token
            ):
                raise StaleRetentionClaimError(
                    f"Stale retention completion for {claim.segment_id}"
                )
            row = await db_connection.fetch_one(
                "UPDATE conversation.retention_segments SET "
                "status = 'completed', version = version + 1, claim_token = NULL, "
                "lease_expires_at = NULL, completion_token = CAST(%s AS UUID), "
                "summary_text = %s, summary_token_count = %s, summary_route_key = %s, "
                "last_error = NULL, updated_at = %s, completed_at = %s "
                "WHERE segment_id = CAST(%s AS UUID) AND status = 'processing' "
                "AND claim_token = CAST(%s AS UUID) RETURNING " + _SEGMENT_COLUMNS,
                (
                    str(token),
                    summary.text,
                    int(summary.token_count),
                    summary.route_key,
                    timestamp,
                    timestamp,
                    str(claim.segment_id),
                    str(token),
                ),
                connection=connection,
            )
            if row is None:
                raise StaleRetentionClaimError(
                    f"Stale retention completion for {claim.segment_id}"
                )
            return _map_segment(row)

    async def retry_compaction(
        self,
        claim: RetentionSegment,
        *,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
    ) -> None:
        """@brief 以 fencing token 安排 retry / Schedule retry using a fencing token."""

        token = _claim_token(claim)
        failure_time = ensure_utc(failed_at)
        retry_time = ensure_utc(retry_at)
        if retry_time <= failure_time:
            raise ValueError("Compaction retry_at must follow failed_at")
        rowcount = await db_connection.execute(
            "UPDATE conversation.retention_segments SET "
            "status = 'retry_wait', version = version + 1, next_attempt_at = %s, "
            "claim_token = NULL, lease_expires_at = NULL, updated_at = %s, "
            "last_error = %s WHERE segment_id = CAST(%s AS UUID) "
            "AND status = 'processing' AND claim_token = CAST(%s AS UUID)",
            (
                retry_time,
                failure_time,
                _required_error(error),
                str(claim.segment_id),
                str(token),
            ),
        )
        _require_fenced_update(rowcount, claim.segment_id)

    async def fail_compaction(
        self,
        claim: RetentionSegment,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 以 fencing token 终结损坏 source / Finally fail a corrupt source using a fencing token."""

        token = _claim_token(claim)
        timestamp = ensure_utc(failed_at)
        rowcount = await db_connection.execute(
            "UPDATE conversation.retention_segments SET "
            "status = 'failed_final', version = version + 1, claim_token = NULL, "
            "lease_expires_at = NULL, updated_at = %s, completed_at = %s, "
            "last_error = %s WHERE segment_id = CAST(%s AS UUID) "
            "AND status = 'processing' AND claim_token = CAST(%s AS UUID)",
            (
                timestamp,
                timestamp,
                _required_error(error),
                str(claim.segment_id),
                str(token),
            ),
        )
        _require_fenced_update(rowcount, claim.segment_id)

    async def recover_expired_compaction_leases(self, *, now: datetime) -> int:
        """@brief 回收过期 lease 并使旧 token 失效 / Recover expired leases and invalidate stale tokens."""

        timestamp = ensure_utc(now)
        retry_at = timestamp + timedelta(microseconds=1)
        return await db_connection.execute(
            "UPDATE conversation.retention_segments SET "
            "status = 'retry_wait', version = version + 1, next_attempt_at = %s, "
            "claim_token = NULL, lease_expires_at = NULL, updated_at = %s, "
            "last_error = COALESCE(last_error, 'recovered expired compaction lease') "
            "WHERE status = 'processing' AND lease_expires_at <= %s",
            (retry_at, timestamp, timestamp),
        )

    async def count_visible_summaries(self, owner_user_id: int) -> int:
        """@brief 按付费 quota 统计可见永久摘要 / Count visible permanent summaries under the paid quota.

        @return 可见 summary 数 / Visible summary count.
        """

        _validate_owner(owner_user_id)
        row = await db_connection.fetch_one(
            "WITH ranked AS ("
            "SELECT segment_id, summary_text, ROW_NUMBER() OVER ("
            "ORDER BY completed_at DESC, segment_id DESC) AS memory_rank, "
            "GREATEST(account.permanent_records_limit, 0) AS memory_limit "
            "FROM conversation.retention_segments AS segment "
            "JOIN identity.users AS account ON account.id = segment.owner_user_id "
            "WHERE segment.owner_user_id = %s AND segment.status = 'completed'"
            ") SELECT COUNT(*) FROM ranked WHERE memory_rank <= memory_limit "
            "AND summary_text IS NOT NULL AND summary_text <> ''",
            (owner_user_id,),
        )
        return _integer(row[0]) if row is not None else 0

    async def fetch_visible_summaries(
        self,
        owner_user_id: int,
        *,
        limit: int,
        offset: int,
    ) -> tuple[RetentionSegment, ...]:
        """@brief 按 quota 读取 newest-first summaries / Read newest-first summaries under the paid quota."""

        return await self._fetch_visible_segments(
            owner_user_id,
            summaries_only=True,
            newest_first=True,
            limit=limit,
            offset=offset,
        )

    async def fetch_visible_segments(
        self,
        owner_user_id: int,
        *,
        newest_first: bool,
        limit: int,
        offset: int,
    ) -> tuple[RetentionSegment, ...]:
        """@brief 按 quota 读取可搜索永久 snapshots / Read searchable permanent snapshots under the paid quota."""

        return await self._fetch_visible_segments(
            owner_user_id,
            summaries_only=False,
            newest_first=newest_first,
            limit=limit,
            offset=offset,
        )

    async def _fetch_visible_segments(
        self,
        owner_user_id: int,
        *,
        summaries_only: bool,
        newest_first: bool,
        limit: int,
        offset: int,
    ) -> tuple[RetentionSegment, ...]:
        """@brief 执行共享 quota-window query / Execute the shared quota-window query."""

        _validate_owner(owner_user_id)
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("Permanent-memory pagination is outside its bounds")
        summary_filter = (
            "AND segment.summary_text IS NOT NULL AND segment.summary_text <> '' "
            if summaries_only
            else ""
        )
        direction = "DESC" if newest_first else "ASC"
        rows = await db_connection.fetch_all(
            "WITH ranked AS ("
            "SELECT segment.segment_id, ROW_NUMBER() OVER ("
            "ORDER BY segment.completed_at DESC, segment.segment_id DESC) AS memory_rank, "
            "GREATEST(account.permanent_records_limit, 0) AS memory_limit "
            "FROM conversation.retention_segments AS segment "
            "JOIN identity.users AS account ON account.id = segment.owner_user_id "
            "WHERE segment.owner_user_id = %s AND segment.status = 'completed'"
            ") SELECT "
            + ", ".join(
                f"segment.{column.strip()}" for column in _SEGMENT_COLUMNS.split(",")
            )
            + " FROM conversation.retention_segments AS segment "
            "JOIN ranked ON ranked.segment_id = segment.segment_id "
            "WHERE ranked.memory_rank <= ranked.memory_limit "
            + summary_filter
            + f"ORDER BY segment.completed_at {direction}, segment.segment_id {direction} "
            "LIMIT %s OFFSET %s",
            (owner_user_id, limit, offset),
        )
        return tuple(_map_segment(row) for row in rows)

    @staticmethod
    async def _load_for_update(
        segment_id: RetentionSegmentId,
        *,
        connection: AsyncConnection,
    ) -> RetentionSegment | None:
        """@brief 锁定一个 Segment / Lock one segment for mutation."""

        row = await db_connection.fetch_one(
            _SEGMENT_SELECT + " WHERE segment_id = CAST(%s AS UUID) FOR UPDATE",
            (str(segment_id),),
            connection=connection,
        )
        return _map_segment(row) if row is not None else None


def _map_segment(row: object) -> RetentionSegment:
    """@brief 将数据库行映射为严格 Segment aggregate / Map a database row to a strict segment aggregate.

    @return RetentionSegment / Retention segment.
    """

    values = _row_values(row, 29)
    kind = RetentionKind(_text(values[1]))
    draft = RetentionSegmentDraft(
        segment_id=RetentionSegmentId.parse(_uuid(values[0])),
        kind=kind,
        conversation_id=ConversationId(_text(values[2])),
        owner_user_id=_integer(values[3]),
        epoch_floor_sequence=_optional_integer(values[4]),
        from_sequence=_optional_integer(values[5]),
        through_sequence=_optional_integer(values[6]),
        anchor_turn_id=(
            TurnId.parse(_uuid(values[7])) if values[7] is not None else None
        ),
        predecessor_segment_id=(
            RetentionSegmentId.parse(_uuid(values[8]))
            if values[8] is not None
            else None
        ),
        projection_version=_integer(values[9]),
        source_digest=_text(values[10]),
        source_snapshot=_snapshot(values[11]),
        source_row_count=_integer(values[12]),
        source_token_count=TokenCount(_integer(values[13])),
        legacy_record_id=_optional_integer(values[14]),
        created_at=_datetime(values[26]),
    )
    summary = None
    if values[22] is not None:
        if values[23] is None or values[24] is None:
            raise RuntimeError("Stored retention summary is missing metadata")
        summary = RetentionSummary(
            _text(values[22]),
            TokenCount(_integer(values[23])),
            _text(values[24]),
        )
    return RetentionSegment(
        draft=draft,
        status=RetentionStatus(_text(values[15])),
        version=_integer(values[16]),
        attempt_count=_integer(values[17]),
        next_attempt_at=_optional_datetime(values[18]),
        claim_token=(
            LeaseToken.parse(_uuid(values[19])) if values[19] is not None else None
        ),
        lease_expires_at=_optional_datetime(values[20]),
        completion_token=(
            LeaseToken.parse(_uuid(values[21])) if values[21] is not None else None
        ),
        summary=summary,
        last_error=_optional_text(values[25]),
        updated_at=_datetime(values[27]),
        completed_at=_optional_datetime(values[28]),
    )


def _map_message(row: object) -> ConversationMessage:
    """@brief 映射 append-only conversation message / Map an append-only conversation message."""

    values = _row_values(row, 9)
    draft = MessageDraft(
        message_id=ConversationMessageId.parse(_uuid(values[0])),
        conversation_id=ConversationId(_text(values[1])),
        turn_id=TurnId.parse(_uuid(values[3])) if values[3] is not None else None,
        source_update_id=(
            UpdateId(_integer(values[4])) if values[4] is not None else None
        ),
        role=MessageRole(_text(values[5])),
        content=_json_object(values[6]),
        idempotency_key=_text(values[7]),
        created_at=_datetime(values[8]),
    )
    return ConversationMessage(draft, MessageSequence(_integer(values[2])))


def _validate_same_draft(
    actual: RetentionSegmentDraft,
    expected: RetentionSegmentDraft,
) -> None:
    """@brief 验证重放没有改变不可变 Segment 语义 / Validate replay has not changed immutable segment semantics."""

    if actual != expected:
        raise RetentionIdempotencyConflictError(
            f"Retention segment {expected.segment_id} changed immutable semantics"
        )


def _claim_token(claim: RetentionSegment) -> LeaseToken:
    """@brief 要求 PROCESSING claim token / Require a processing claim token."""

    if claim.status is not RetentionStatus.PROCESSING or claim.claim_token is None:
        raise ValueError("Retention operation requires a processing claim")
    return claim.claim_token


def _require_fenced_update(
    rowcount: int,
    segment_id: RetentionSegmentId,
) -> None:
    """@brief 拒绝影响行数为零的 stale claim / Reject a stale claim whose update affected no row."""

    if rowcount != 1:
        raise StaleRetentionClaimError(f"Stale retention claim for {segment_id}")


def _validate_owner(owner_user_id: int) -> None:
    """@brief 校验永久记忆用户 ID / Validate a permanent-memory user ID."""

    if isinstance(owner_user_id, bool) or owner_user_id <= 0:
        raise ValueError("Permanent-memory owner_user_id must be positive")


def _required_error(error: str) -> str:
    """@brief 规范化有界持久化错误 / Normalize a bounded persisted error."""

    normalized = error.strip()
    if not normalized:
        raise ValueError("Compaction error cannot be blank")
    return normalized[:2000]


def _encode_snapshot(snapshot: tuple[JsonObject, ...]) -> str:
    """@brief 编码 snapshot JSON / Encode snapshot JSON."""

    return json.dumps(
        snapshot,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _snapshot(value: object) -> tuple[JsonObject, ...]:
    """@brief 解码并验证 source snapshot / Decode and validate a source snapshot."""

    decoded: object = value
    if isinstance(decoded, bytes):
        decoded = decoded.decode()
    if isinstance(decoded, str):
        decoded = json.loads(decoded)
    if not isinstance(decoded, list) or not all(
        isinstance(item, dict) for item in decoded
    ):
        raise TypeError("Retention source_snapshot must be a JSON array of objects")
    return tuple(cast(JsonObject, item) for item in decoded)


def _json_object(value: object) -> JsonObject:
    """@brief 解码 JSON object / Decode a JSON object."""

    decoded: object = value
    if isinstance(decoded, bytes):
        decoded = decoded.decode()
    if isinstance(decoded, str):
        decoded = json.loads(decoded)
    if not isinstance(decoded, dict):
        raise TypeError("Expected a JSON object")
    return cast(JsonObject, decoded)


def _row_values(row: object, expected: int) -> Sequence[object]:
    """@brief 验证数据库 row shape / Validate a database-row shape."""

    values = cast(Sequence[object], row)
    if len(values) != expected:
        raise RuntimeError(f"Expected {expected} columns, received {len(values)}")
    return values


def _uuid(value: object) -> UUID:
    """@brief 解析 UUID / Parse a UUID."""

    return value if isinstance(value, UUID) else UUID(str(value))


def _integer(value: object) -> int:
    """@brief 解析非 bool 整数 / Parse a non-Boolean integer."""

    if isinstance(value, bool):
        raise TypeError("Boolean cannot represent an integer column")
    return int(str(value))


def _optional_integer(value: object) -> int | None:
    """@brief 解析可选整数 / Parse an optional integer."""

    return None if value is None else _integer(value)


def _text(value: object) -> str:
    """@brief 解析必需文本 / Parse required text."""

    if value is None:
        raise TypeError("Expected non-null text")
    return str(value)


def _optional_text(value: object) -> str | None:
    """@brief 解析可选文本 / Parse optional text."""

    return None if value is None else str(value)


def _datetime(value: object) -> datetime:
    """@brief 解析必需 datetime / Parse required datetime."""

    if not isinstance(value, datetime):
        raise TypeError("Expected a datetime")
    return ensure_utc(value)


def _optional_datetime(value: object) -> datetime | None:
    """@brief 解析可选 datetime / Parse optional datetime."""

    return None if value is None else _datetime(value)


__all__ = ["PostgresConversationRetention"]
