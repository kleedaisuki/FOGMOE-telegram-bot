"""PostgreSQL adapter for durable Admin announcement delivery."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.admin.models import (
    AnnouncementAcceptance,
    RequestAnnouncement,
)
from fogmoe_bot.domain.admin.models import (
    AnnouncementId,
    AnnouncementRecipientClaim,
    AnnouncementRecipientKind,
)
from fogmoe_bot.domain.conversation.identity import OutboundMessageId
from fogmoe_bot.infrastructure.database import connection as db_connection


class AnnouncementIdempotencyConflict(RuntimeError):
    """@brief 同一公告幂等键被用于不同意图 / The same announcement idempotency key denotes a different intent."""


class PostgresAdminAnnouncementOperations:
    """@brief 持久化公告意图、受众快照、租约与 fencing 回执 / Persist announcement intents, audience snapshots, leases, and fenced receipts."""

    async def accept(self, command: RequestAnnouncement) -> AnnouncementAcceptance:
        """@brief 原子创建意图、受众快照和终态报告回执 / Atomically create the intent, audience snapshot, and terminal-report receipt.

        @param command 已授权公告命令 / Authorized announcement command.
        @return 规范接收回执 / Canonical acceptance receipt.
        @raise AnnouncementIdempotencyConflict 同键语义不同 / The same key denotes different semantics.
        """

        announcement_id = AnnouncementId.for_idempotency_key(command.idempotency_key)
        async with db_connection.transaction() as connection:
            inserted_row = await db_connection.fetch_one(
                "INSERT INTO admin.announcements "
                "(announcement_id, idempotency_key, requested_by, source_update_id, "
                "body, recipient_count, state, created_at, updated_at) "
                "VALUES (CAST(%s AS UUID), %s, %s, %s, %s, 0, 'expanding', %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING announcement_id",
                (
                    str(announcement_id),
                    command.idempotency_key,
                    command.actor_id,
                    command.source_update_id,
                    command.body,
                    command.requested_at,
                    command.requested_at,
                ),
                connection=connection,
            )
            inserted = inserted_row is not None
            if inserted:
                await self._snapshot_audience(
                    connection,
                    announcement_id=announcement_id,
                    command=command,
                )
            row = await db_connection.fetch_one(
                "SELECT announcement.announcement_id, announcement.idempotency_key, "
                "announcement.requested_by, announcement.source_update_id, "
                "announcement.body, announcement.recipient_count, announcement.created_at, "
                "completion.chat_id, completion.message_thread_id, "
                "completion.reply_to_message_id "
                "FROM admin.announcements AS announcement "
                "JOIN admin.announcement_recipients AS completion "
                "ON completion.announcement_id = announcement.announcement_id "
                "AND completion.recipient_kind = 'completion' "
                "WHERE announcement.idempotency_key = %s "
                "FOR UPDATE OF announcement, completion",
                (command.idempotency_key,),
                connection=connection,
            )
            if row is None:
                raise RuntimeError(
                    "Announcement acceptance returned no canonical intent"
                )
            self._validate_replay(row, command, expected_id=announcement_id)
            return AnnouncementAcceptance(
                announcement_id=AnnouncementId.parse(cast(UUID | str, row[0])),
                recipient_count=_integer(row[5]),
                inserted=inserted,
            )

    async def _snapshot_audience(
        self,
        connection: AsyncConnection,
        *,
        announcement_id: AnnouncementId,
        command: RequestAnnouncement,
    ) -> None:
        """@brief 在意图事务内固化用户和群组受众 / Materialize user and group audiences inside the intent transaction.

        @param connection 意图事务连接 / Intent-transaction connection.
        @param announcement_id 公告 ID / Announcement ID.
        @param command 公告命令 / Announcement command.
        @return None / None.
        """

        parameters = (
            str(announcement_id),
            command.requested_at,
            command.requested_at,
        )
        await db_connection.execute(
            """
            INSERT INTO admin.announcement_recipients
              (announcement_id, recipient_kind, chat_id, status,
               next_attempt_at, created_at, updated_at)
            SELECT CAST(%s AS UUID), 'user', audience.chat_id, 'pending', %s, %s, %s
            FROM (
              SELECT DISTINCT COALESCE(tg_uid, id) AS chat_id
              FROM identity.users
              WHERE provider = 'telegram' AND COALESCE(tg_uid, id) <> 0
            ) AS audience
            ON CONFLICT (announcement_id, recipient_kind, chat_id) DO NOTHING
            """,
            (*parameters, command.requested_at),
            connection=connection,
        )
        await db_connection.execute(
            """
            INSERT INTO admin.announcement_recipients
              (announcement_id, recipient_kind, chat_id, status,
               next_attempt_at, created_at, updated_at)
            SELECT CAST(%s AS UUID), 'group', audience.group_id, 'pending', %s, %s, %s
            FROM (
              SELECT group_id FROM moderation.group_keywords
              UNION
              SELECT group_id FROM moderation.group_verification
              UNION
              SELECT group_id FROM moderation.group_spam_control
              UNION
              SELECT group_id FROM crypto.group_chart_tokens
              UNION
              SELECT group_id FROM conversation.group_message_projection
              WHERE is_canonical
            ) AS audience
            WHERE audience.group_id <> 0
            ON CONFLICT (announcement_id, recipient_kind, chat_id) DO NOTHING
            """,
            (*parameters, command.requested_at),
            connection=connection,
        )
        await db_connection.execute(
            "INSERT INTO admin.announcement_recipients "
            "(announcement_id, recipient_kind, chat_id, message_thread_id, "
            "reply_to_message_id, status, next_attempt_at, created_at, updated_at) "
            "VALUES (CAST(%s AS UUID), 'completion', %s, %s, %s, "
            "'blocked', NULL, %s, %s)",
            (
                str(announcement_id),
                command.reply_chat_id,
                command.reply_message_thread_id,
                command.reply_message_id,
                command.requested_at,
                command.requested_at,
            ),
            connection=connection,
        )
        await db_connection.execute(
            "UPDATE admin.announcements SET "
            "recipient_count = (SELECT COUNT(*) FROM admin.announcement_recipients "
            "WHERE announcement_id = CAST(%s AS UUID) "
            "AND recipient_kind IN ('user', 'group')), "
            "state = CASE WHEN EXISTS (SELECT 1 FROM admin.announcement_recipients "
            "WHERE announcement_id = CAST(%s AS UUID) "
            "AND recipient_kind IN ('user', 'group')) "
            "THEN 'expanding' ELSE 'delivering' END "
            "WHERE announcement_id = CAST(%s AS UUID)",
            (str(announcement_id), str(announcement_id), str(announcement_id)),
            connection=connection,
        )

    def _validate_replay(
        self,
        row: Sequence[object],
        command: RequestAnnouncement,
        *,
        expected_id: AnnouncementId,
    ) -> None:
        """@brief 拒绝同键不同义的公告重放 / Reject an announcement replay with different semantics.

        @param row 已持久化意图行 / Persisted intent row.
        @param command 重放命令 / Replayed command.
        @param expected_id 幂等键推导 ID / ID derived from the idempotency key.
        @return None / None.
        @raise AnnouncementIdempotencyConflict 语义不同 / Semantics differ.
        """

        same = (
            AnnouncementId.parse(cast(UUID | str, row[0])) == expected_id
            and str(row[1]) == command.idempotency_key
            and _integer(row[2]) == command.actor_id
            and _integer(row[3]) == command.source_update_id
            and str(row[4]) == command.body
            and _utc(cast(datetime, row[6])) == command.requested_at
            and _integer(row[7]) == command.reply_chat_id
            and _optional_integer(row[8]) == command.reply_message_thread_id
            and _integer(row[9]) == command.reply_message_id
        )
        if not same:
            raise AnnouncementIdempotencyConflict(
                "Announcement idempotency key already denotes another intent"
            )

    async def promote_delivery_completions(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> int:
        """@brief 在受众 outbox 全部终态后释放完成报告 / Release completion reporting after every audience outbox is terminal.

        @param now 当前 UTC 时间 / Current UTC instant.
        @param limit 最大公告数 / Maximum announcement count.
        @return 推进数 / Promotion count.
        """

        _require_positive_limit(limit)
        timestamp = _utc(now)
        async with db_connection.transaction() as connection:
            return await db_connection.execute(
                """
                WITH candidates AS (
                  SELECT announcement.announcement_id
                  FROM admin.announcements AS announcement
                  WHERE announcement.state = 'delivering'
                    AND NOT EXISTS (
                      SELECT 1
                      FROM admin.announcement_recipients AS recipient
                      JOIN conversation.outbound_messages AS outbound
                        ON outbound.message_id = recipient.outbound_message_id
                      WHERE recipient.announcement_id = announcement.announcement_id
                        AND recipient.recipient_kind IN ('user', 'group')
                        AND recipient.status = 'expanded'
                        AND outbound.status NOT IN ('delivered', 'failed_final', 'cancelled')
                    )
                  ORDER BY announcement.created_at, announcement.announcement_id
                  FOR UPDATE OF announcement SKIP LOCKED
                  LIMIT %s
                ), promoted AS (
                  UPDATE admin.announcements AS announcement
                  SET state = 'completed', completed_at = %s, updated_at = %s
                  FROM candidates
                  WHERE announcement.announcement_id = candidates.announcement_id
                    AND announcement.state = 'delivering'
                  RETURNING announcement.announcement_id
                )
                UPDATE admin.announcement_recipients AS completion
                SET status = 'pending', next_attempt_at = %s, updated_at = %s
                FROM promoted
                WHERE completion.announcement_id = promoted.announcement_id
                  AND completion.recipient_kind = 'completion'
                  AND completion.status = 'blocked'
                """,
                (limit, timestamp, timestamp, timestamp, timestamp),
                connection=connection,
            )

    async def claim_ready(
        self,
        *,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> Sequence[AnnouncementRecipientClaim]:
        """@brief 领取有界的可执行回执 / Claim a bounded ready-receipt batch.

        @param now 当前 UTC 时间 / Current UTC instant.
        @param lease_for 租约时长 / Lease duration.
        @param limit 最大回执数 / Maximum receipt count.
        @return 带独立 token 的领取 / Claims with independent tokens.
        """

        _require_positive_limit(limit)
        if lease_for <= timedelta(0):
            raise ValueError("Announcement claim lease must be positive")
        claimed_at = _utc(now)
        lease_expires_at = claimed_at + lease_for
        claims: list[AnnouncementRecipientClaim] = []
        """@brief 本事务领取列表 / Claims acquired by this transaction."""
        async with db_connection.transaction() as connection:
            rows = await db_connection.fetch_all(
                """
                SELECT
                  recipient.announcement_id,
                  recipient.recipient_kind,
                  recipient.chat_id,
                  recipient.message_thread_id,
                  recipient.reply_to_message_id,
                  recipient.attempt_count,
                  announcement.body,
                  announcement.recipient_count,
                  announcement.created_at,
                  COALESCE((
                    SELECT COUNT(*)
                    FROM admin.announcement_recipients AS audience
                    JOIN conversation.outbound_messages AS outbound
                      ON outbound.message_id = audience.outbound_message_id
                    WHERE audience.announcement_id = announcement.announcement_id
                      AND audience.recipient_kind IN ('user', 'group')
                      AND audience.status = 'expanded'
                      AND outbound.status = 'delivered'
                  ), 0),
                  COALESCE((
                    SELECT COUNT(*)
                    FROM admin.announcement_recipients AS audience
                    LEFT JOIN conversation.outbound_messages AS outbound
                      ON outbound.message_id = audience.outbound_message_id
                    WHERE audience.announcement_id = announcement.announcement_id
                      AND audience.recipient_kind IN ('user', 'group')
                      AND (
                        audience.status = 'failed_final'
                        OR (
                          audience.status = 'expanded'
                          AND outbound.status IN ('failed_final', 'cancelled')
                        )
                      )
                  ), 0)
                FROM admin.announcement_recipients AS recipient
                JOIN admin.announcements AS announcement
                  ON announcement.announcement_id = recipient.announcement_id
                WHERE recipient.status IN ('pending', 'retry_wait')
                  AND recipient.next_attempt_at <= %s
                ORDER BY
                  CASE recipient.recipient_kind WHEN 'completion' THEN 1 ELSE 0 END,
                  announcement.created_at,
                  recipient.announcement_id,
                  recipient.recipient_kind,
                  recipient.chat_id
                FOR UPDATE OF recipient SKIP LOCKED
                LIMIT %s
                """,
                (claimed_at, limit),
                connection=connection,
            )
            for row in rows:
                token = uuid4()
                rowcount = await db_connection.execute(
                    "UPDATE admin.announcement_recipients SET "
                    "status = 'processing', attempt_count = attempt_count + 1, "
                    "next_attempt_at = NULL, claim_token = CAST(%s AS UUID), "
                    "lease_expires_at = %s, last_error = NULL, updated_at = %s "
                    "WHERE announcement_id = CAST(%s AS UUID) "
                    "AND recipient_kind = %s AND chat_id = %s "
                    "AND status IN ('pending', 'retry_wait')",
                    (
                        str(token),
                        lease_expires_at,
                        claimed_at,
                        str(row[0]),
                        str(row[1]),
                        _integer(row[2]),
                    ),
                    connection=connection,
                )
                if rowcount != 1:
                    raise RuntimeError(
                        "Locked announcement receipt could not be claimed"
                    )
                claims.append(
                    AnnouncementRecipientClaim(
                        announcement_id=AnnouncementId.parse(cast(UUID | str, row[0])),
                        recipient_kind=AnnouncementRecipientKind(str(row[1])),
                        chat_id=_integer(row[2]),
                        message_thread_id=_optional_integer(row[3]),
                        reply_to_message_id=_optional_integer(row[4]),
                        body=str(row[6]),
                        recipient_count=_integer(row[7]),
                        delivered_count=_integer(row[9]),
                        failed_count=_integer(row[10]),
                        claim_token=token,
                        attempt_count=_integer(row[5]) + 1,
                        announcement_created_at=_utc(cast(datetime, row[8])),
                        claimed_at=claimed_at,
                        lease_expires_at=lease_expires_at,
                    )
                )
        return tuple(claims)

    async def mark_expanded(
        self,
        claim: AnnouncementRecipientClaim,
        *,
        outbound_message_id: OutboundMessageId,
        completed_at: datetime,
    ) -> bool:
        """@brief 用 fencing token 终结回执并推进公告 / Finalize a receipt and advance its announcement with a fencing token.

        @param claim 领取凭证 / Claim receipt.
        @param outbound_message_id 已入队的 outbox ID / Enqueued outbox ID.
        @param completed_at 终结时间 / Completion instant.
        @return token 仍有效时为 True / True when the token was current.
        """

        timestamp = _utc(completed_at)
        async with db_connection.transaction() as connection:
            rowcount = await db_connection.execute(
                "UPDATE admin.announcement_recipients SET "
                "status = 'expanded', outbound_message_id = CAST(%s AS UUID), "
                "expanded_at = %s, claim_token = NULL, lease_expires_at = NULL, "
                "last_error = NULL, updated_at = %s "
                "WHERE announcement_id = CAST(%s AS UUID) "
                "AND recipient_kind = %s AND chat_id = %s "
                "AND status = 'processing' AND claim_token = CAST(%s AS UUID)",
                (
                    str(outbound_message_id),
                    timestamp,
                    timestamp,
                    str(claim.announcement_id),
                    claim.recipient_kind.value,
                    claim.chat_id,
                    str(claim.claim_token),
                ),
                connection=connection,
            )
            if (
                rowcount == 1
                and claim.recipient_kind is not AnnouncementRecipientKind.COMPLETION
            ):
                await self._advance_audience_expansion(
                    connection,
                    claim.announcement_id,
                    now=timestamp,
                )
            return rowcount == 1

    async def schedule_retry(
        self,
        claim: AnnouncementRecipientClaim,
        *,
        retry_at: datetime,
        error_category: str,
    ) -> bool:
        """@brief 用 fencing token 安排重试 / Schedule a retry with a fencing token.

        @param claim 领取凭证 / Claim receipt.
        @param retry_at 下次尝试时间 / Next-attempt instant.
        @param error_category 错误分类 / Error category.
        @return token 仍有效时为 True / True when the token was current.
        """

        timestamp = _utc(retry_at)
        error = _error_category(error_category)
        rowcount = await db_connection.execute(
            "UPDATE admin.announcement_recipients SET "
            "status = 'retry_wait', next_attempt_at = %s, claim_token = NULL, "
            "lease_expires_at = NULL, last_error = %s, updated_at = %s "
            "WHERE announcement_id = CAST(%s AS UUID) "
            "AND recipient_kind = %s AND chat_id = %s "
            "AND status = 'processing' AND claim_token = CAST(%s AS UUID)",
            (
                timestamp,
                error,
                timestamp,
                str(claim.announcement_id),
                claim.recipient_kind.value,
                claim.chat_id,
                str(claim.claim_token),
            ),
        )
        return rowcount == 1

    async def mark_failed_final(
        self,
        claim: AnnouncementRecipientClaim,
        *,
        failed_at: datetime,
        error_category: str,
    ) -> bool:
        """@brief 用 fencing token 记录最终失败并推进公告 / Record final failure and advance the announcement with a fencing token.

        @param claim 领取凭证 / Claim receipt.
        @param failed_at 失败时间 / Failure instant.
        @param error_category 错误分类 / Error category.
        @return token 仍有效时为 True / True when the token was current.
        """

        timestamp = _utc(failed_at)
        error = _error_category(error_category)
        async with db_connection.transaction() as connection:
            rowcount = await db_connection.execute(
                "UPDATE admin.announcement_recipients SET "
                "status = 'failed_final', terminal_at = %s, claim_token = NULL, "
                "lease_expires_at = NULL, last_error = %s, updated_at = %s "
                "WHERE announcement_id = CAST(%s AS UUID) "
                "AND recipient_kind = %s AND chat_id = %s "
                "AND status = 'processing' AND claim_token = CAST(%s AS UUID)",
                (
                    timestamp,
                    error,
                    timestamp,
                    str(claim.announcement_id),
                    claim.recipient_kind.value,
                    claim.chat_id,
                    str(claim.claim_token),
                ),
                connection=connection,
            )
            if (
                rowcount == 1
                and claim.recipient_kind is not AnnouncementRecipientKind.COMPLETION
            ):
                await self._advance_audience_expansion(
                    connection,
                    claim.announcement_id,
                    now=timestamp,
                )
            return rowcount == 1

    async def _advance_audience_expansion(
        self,
        connection: AsyncConnection,
        announcement_id: AnnouncementId,
        *,
        now: datetime,
    ) -> None:
        """@brief 最后一个受众回执终态时进入投递等待 / Enter delivery waiting when the final audience receipt becomes terminal.

        @param connection 当前回执事务 / Current receipt transaction.
        @param announcement_id 公告 ID / Announcement ID.
        @param now 状态转移时间 / Transition instant.
        @return None / None.
        """

        await db_connection.execute(
            "UPDATE admin.announcements AS announcement SET "
            "state = 'delivering', updated_at = %s "
            "WHERE announcement.announcement_id = CAST(%s AS UUID) "
            "AND announcement.state = 'expanding' "
            "AND NOT EXISTS (SELECT 1 FROM admin.announcement_recipients AS recipient "
            "WHERE recipient.announcement_id = announcement.announcement_id "
            "AND recipient.recipient_kind IN ('user', 'group') "
            "AND recipient.status NOT IN ('expanded', 'failed_final'))",
            (now, str(announcement_id)),
            connection=connection,
        )

    async def recover_expired(self, *, now: datetime, limit: int) -> int:
        """@brief 回收过期回执租约 / Recover expired receipt leases.

        @param now 当前 UTC 时间 / Current UTC instant.
        @param limit 最大回收数 / Maximum recovery count.
        @return 回收数 / Recovery count.
        """

        _require_positive_limit(limit)
        timestamp = _utc(now)
        async with db_connection.transaction() as connection:
            return await db_connection.execute(
                """
                WITH expired AS (
                  SELECT announcement_id, recipient_kind, chat_id
                  FROM admin.announcement_recipients
                  WHERE status = 'processing' AND lease_expires_at <= %s
                  ORDER BY lease_expires_at, announcement_id, recipient_kind, chat_id
                  FOR UPDATE SKIP LOCKED
                  LIMIT %s
                )
                UPDATE admin.announcement_recipients AS recipient
                SET status = 'retry_wait', next_attempt_at = %s,
                    claim_token = NULL, lease_expires_at = NULL,
                    last_error = 'lease_expired', updated_at = %s
                FROM expired
                WHERE recipient.announcement_id = expired.announcement_id
                  AND recipient.recipient_kind = expired.recipient_kind
                  AND recipient.chat_id = expired.chat_id
                  AND recipient.status = 'processing'
                """,
                (timestamp, limit, timestamp, timestamp),
                connection=connection,
            )


def _integer(value: object) -> int:
    """@brief 严格转换数据库整数 / Strictly convert a database integer.

    @param value 数据库值 / Database value.
    @return Python 整数 / Python integer.
    @raise ValueError 值不是整数 / The value is not an integer.
    """

    if isinstance(value, bool):
        raise ValueError("Boolean is not an Admin integer")
    return int(str(value))


def _optional_integer(value: object) -> int | None:
    """@brief 转换可空整数 / Convert an optional database integer.

    @param value 数据库值 / Database value.
    @return 整数或 None / Integer or None.
    """

    return None if value is None else _integer(value)


def _utc(value: datetime) -> datetime:
    """@brief 将 aware 时间规范为 UTC / Normalize an aware instant to UTC.

    @param value 输入时间 / Input instant.
    @return UTC aware 时间 / UTC-aware instant.
    @raise ValueError 输入为 naive datetime / The input is naive.
    """

    if value.tzinfo is None:
        raise ValueError("Admin persistence timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _require_positive_limit(limit: int) -> None:
    """@brief 校验有界批量 / Validate a bounded batch limit.

    @param limit 批量上限 / Batch bound.
    @return None / None.
    @raise ValueError limit 非正 / The limit is not positive.
    """

    if isinstance(limit, bool) or limit < 1:
        raise ValueError("Admin batch limit must be positive")


def _error_category(value: str) -> str:
    """@brief 约束持久化错误分类 / Bound a persisted error category.

    @param value 错误类别 / Error category.
    @return 1-100 字符类别 / Category containing 1-100 characters.
    """

    normalized = value.strip()[:100]
    return normalized or "unknown"


__all__ = [
    "AnnouncementIdempotencyConflict",
    "PostgresAdminAnnouncementOperations",
]
