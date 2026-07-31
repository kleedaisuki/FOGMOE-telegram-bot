"""PostgreSQL adapter for durable Admin announcement delivery."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.admin.models import (
    AnnouncementAcceptance,
    RequestAnnouncement,
)
from fogmoe_bot.domain.admin.announcement import (
    AnnouncementDeliveryCounts,
    AnnouncementDispatchContent,
    AnnouncementId,
)
from fogmoe_bot.domain.admin.recipient import (
    AnnouncementClaimToken,
    AnnouncementCompletionReleased,
    AnnouncementRecipient,
    AnnouncementRecipientClaim,
    AnnouncementRecipientDeadLettered,
    AnnouncementRecipientExpanded,
    AnnouncementRecipientKind,
    AnnouncementRecipientLeaseRecovered,
    AnnouncementRecipientRetryScheduled,
    ExpandedAnnouncementRecipient,
    FailedAnnouncementRecipient,
    RetryWaitingAnnouncementRecipient,
)
from fogmoe_bot.infrastructure.database import db

_RECIPIENT_COLUMNS = (
    "announcement_id, recipient_kind, chat_id, message_thread_id, "
    "reply_to_message_id, status, attempt_count, next_attempt_at, claim_token, "
    "lease_expires_at, outbound_message_id, last_error, created_at, updated_at, "
    "expanded_at, terminal_at"
)
"""@brief recipient 聚合的规范数据库列序 / Canonical database column order for recipient aggregates."""

type _SettlementDecision = (
    AnnouncementRecipientExpanded
    | AnnouncementRecipientRetryScheduled
    | AnnouncementRecipientDeadLettered
)
"""@brief processing claim 的穷尽结算决策 / Exhaustive settlement decisions for a processing claim."""


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
        async with db.transaction() as connection:
            inserted_row = await db.fetch_one(
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
            row = await db.fetch_one(
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
        await db.execute(
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
        await db.execute(
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
        await db.execute(
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
        await db.execute(
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
        async with db.transaction() as connection:
            candidates = await db.fetch_all(
                """
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
                """,
                (limit,),
                connection=connection,
            )
            decisions: list[AnnouncementCompletionReleased] = []
            """@brief 由锁定 blocked pre-state 产生的 release 决策 / Release decisions produced from locked blocked pre-states."""
            for candidate in candidates:
                announcement_id = AnnouncementId.parse(cast(UUID | str, candidate[0]))
                pre_row = await db.fetch_one(
                    f"SELECT {_qualified_recipient_columns('completion')} "
                    "FROM admin.announcement_recipients AS completion "
                    "WHERE completion.announcement_id = CAST(%s AS UUID) "
                    "AND completion.recipient_kind = 'completion' "
                    "AND completion.status = 'blocked' FOR UPDATE",
                    (str(announcement_id),),
                    connection=connection,
                )
                if pre_row is None:
                    raise RuntimeError(
                        "Delivering announcement has no blocked completion recipient"
                    )
                blocked = _restore_recipient(pre_row)
                decision = blocked.release_completion(released_at=timestamp)
                post_row = await db.fetch_one(
                    "WITH promoted AS ("
                    "UPDATE admin.announcements AS announcement SET "
                    "state = 'completed', completed_at = %s, updated_at = %s "
                    "WHERE announcement.announcement_id = CAST(%s AS UUID) "
                    "AND announcement.state = 'delivering' "
                    "RETURNING announcement.announcement_id) "
                    "UPDATE admin.announcement_recipients AS completion SET "
                    "status = 'pending', next_attempt_at = %s, updated_at = %s "
                    "FROM promoted WHERE completion.announcement_id = "
                    "promoted.announcement_id "
                    "AND completion.recipient_kind = 'completion' "
                    "AND completion.status = 'blocked' "
                    f"RETURNING {_qualified_recipient_columns('completion')}",
                    (
                        timestamp,
                        timestamp,
                        str(announcement_id),
                        timestamp,
                        timestamp,
                    ),
                    connection=connection,
                )
                if post_row is None:
                    raise RuntimeError(
                        "Locked announcement completion could not be released"
                    )
                _require_expected_post_state(
                    post_row,
                    decision.recipient,
                    operation="completion release",
                )
                decisions.append(decision)
            return len(decisions)

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
        async with db.transaction() as connection:
            rows = await db.fetch_all(
                """
                SELECT
                  recipient.announcement_id,
                  recipient.recipient_kind,
                  recipient.chat_id,
                  recipient.message_thread_id,
                  recipient.reply_to_message_id,
                  recipient.status,
                  recipient.attempt_count,
                  recipient.next_attempt_at,
                  recipient.claim_token,
                  recipient.lease_expires_at,
                  recipient.outbound_message_id,
                  recipient.last_error,
                  recipient.created_at,
                  recipient.updated_at,
                  recipient.expanded_at,
                  recipient.terminal_at,
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
                recipient = _restore_recipient(row)
                content = _dispatch_content(row)
                token = AnnouncementClaimToken.new()
                claim = recipient.claim(
                    token=token,
                    claimed_at=claimed_at,
                    lease_expires_at=lease_expires_at,
                    content=content,
                )
                post_row = await db.fetch_one(
                    "UPDATE admin.announcement_recipients SET "
                    "status = 'processing', attempt_count = attempt_count + 1, "
                    "next_attempt_at = NULL, claim_token = CAST(%s AS UUID), "
                    "lease_expires_at = %s, last_error = NULL, updated_at = %s "
                    "WHERE announcement_id = CAST(%s AS UUID) "
                    "AND recipient_kind = %s AND chat_id = %s "
                    "AND status = %s "
                    "RETURNING announcement_id, recipient_kind, chat_id, "
                    "message_thread_id, reply_to_message_id, status, attempt_count, "
                    "next_attempt_at, claim_token, lease_expires_at, "
                    "outbound_message_id, last_error, created_at, updated_at, "
                    "expanded_at, terminal_at",
                    (
                        str(token),
                        lease_expires_at,
                        claimed_at,
                        str(recipient.announcement_id),
                        recipient.recipient_kind.value,
                        recipient.chat_id,
                        recipient.status.value,
                    ),
                    connection=connection,
                )
                if post_row is None:
                    raise RuntimeError(
                        "Locked announcement receipt could not be claimed"
                    )
                if _restore_recipient(post_row) != claim.recipient:
                    raise RuntimeError(
                        "Claimed announcement recipient disagrees with domain transition"
                    )
                claims.append(claim)
        return tuple(claims)

    async def _lock_processing_recipient(
        self,
        connection: AsyncConnection,
        *,
        recipient: AnnouncementRecipient,
        token: AnnouncementClaimToken,
    ) -> AnnouncementRecipient | None:
        """@brief 锁定并恢复 settlement 的真实 processing pre-state / Lock and restore the real processing pre-state for settlement.

        @param connection 当前短事务连接 / Current short-transaction connection.
        @param recipient 决策携带的后态 identity / Post-state identity carried by the decision.
        @param token 原 claim fencing token / Original claim fencing token.
        @return token 仍有效时的完整 pre-state，否则 None / Complete pre-state when the token is current, otherwise None.
        """

        row = await db.fetch_one(
            f"SELECT {_RECIPIENT_COLUMNS} "
            "FROM admin.announcement_recipients "
            "WHERE announcement_id = CAST(%s AS UUID) "
            "AND recipient_kind = %s AND chat_id = %s "
            "AND status = 'processing' AND claim_token = CAST(%s AS UUID) "
            "FOR UPDATE",
            (
                str(recipient.announcement_id),
                recipient.recipient_kind.value,
                recipient.chat_id,
                str(token),
            ),
            connection=connection,
        )
        return None if row is None else _restore_recipient(row)

    async def persist_expanded(
        self,
        decision: AnnouncementRecipientExpanded,
    ) -> bool:
        """@brief 持久化 expanded 决策并推进公告 / Persist an expanded decision and advance its announcement.

        @param decision token-fenced 领域决策 / Token-fenced domain decision.
        @return token 仍有效时为 True / True when the token was current.
        """

        recipient = decision.recipient
        async with db.transaction() as connection:
            pre = await self._lock_processing_recipient(
                connection,
                recipient=recipient,
                token=decision.claim.capability.token,
            )
            if pre is None:
                return False
            expected = _apply_settlement_decision(decision, pre)
            state = cast(ExpandedAnnouncementRecipient, expected.state)
            post_row = await db.fetch_one(
                "UPDATE admin.announcement_recipients SET "
                "status = 'expanded', outbound_message_id = CAST(%s AS UUID), "
                "expanded_at = %s, claim_token = NULL, lease_expires_at = NULL, "
                "last_error = NULL, updated_at = %s "
                "WHERE announcement_id = CAST(%s AS UUID) "
                "AND recipient_kind = %s AND chat_id = %s "
                "AND status = 'processing' AND claim_token = CAST(%s AS UUID) "
                f"RETURNING {_RECIPIENT_COLUMNS}",
                (
                    str(state.outbound_message_id),
                    state.expanded_at,
                    expected.updated_at,
                    str(expected.announcement_id),
                    expected.recipient_kind.value,
                    expected.chat_id,
                    str(decision.claim.capability.token),
                ),
                connection=connection,
            )
            if post_row is None:
                raise RuntimeError("Locked announcement recipient could not be expanded")
            _require_expected_post_state(
                post_row,
                expected,
                operation="expanded settlement",
            )
            if expected.recipient_kind is not AnnouncementRecipientKind.COMPLETION:
                await self._advance_audience_expansion(
                    connection,
                    expected.announcement_id,
                    now=expected.updated_at,
                )
            return True

    async def persist_retry(
        self,
        decision: AnnouncementRecipientRetryScheduled,
    ) -> bool:
        """@brief 持久化 retry-wait 决策 / Persist a retry-wait decision.

        @param decision token-fenced 领域决策 / Token-fenced domain decision.
        @return token 仍有效时为 True / True when the token was current.
        """

        recipient = decision.recipient
        async with db.transaction() as connection:
            pre = await self._lock_processing_recipient(
                connection,
                recipient=recipient,
                token=decision.claim.capability.token,
            )
            if pre is None:
                return False
            expected = _apply_settlement_decision(decision, pre)
            state = cast(RetryWaitingAnnouncementRecipient, expected.state)
            post_row = await db.fetch_one(
                "UPDATE admin.announcement_recipients SET "
                "status = 'retry_wait', next_attempt_at = %s, claim_token = NULL, "
                "lease_expires_at = NULL, last_error = %s, updated_at = %s "
                "WHERE announcement_id = CAST(%s AS UUID) "
                "AND recipient_kind = %s AND chat_id = %s "
                "AND status = 'processing' AND claim_token = CAST(%s AS UUID) "
                f"RETURNING {_RECIPIENT_COLUMNS}",
                (
                    state.next_attempt_at,
                    state.failure.value,
                    expected.updated_at,
                    str(expected.announcement_id),
                    expected.recipient_kind.value,
                    expected.chat_id,
                    str(decision.claim.capability.token),
                ),
                connection=connection,
            )
            if post_row is None:
                raise RuntimeError("Locked announcement recipient could not be retried")
            _require_expected_post_state(
                post_row,
                expected,
                operation="retry settlement",
            )
            return True

    async def persist_dead_letter(
        self,
        decision: AnnouncementRecipientDeadLettered,
    ) -> bool:
        """@brief 持久化 failed-final 决策并推进公告 / Persist a failed-final decision and advance its announcement.

        @param decision token-fenced 领域决策 / Token-fenced domain decision.
        @return token 仍有效时为 True / True when the token was current.
        """

        recipient = decision.recipient
        async with db.transaction() as connection:
            pre = await self._lock_processing_recipient(
                connection,
                recipient=recipient,
                token=decision.claim.capability.token,
            )
            if pre is None:
                return False
            expected = _apply_settlement_decision(decision, pre)
            state = cast(FailedAnnouncementRecipient, expected.state)
            post_row = await db.fetch_one(
                "UPDATE admin.announcement_recipients SET "
                "status = 'failed_final', terminal_at = %s, claim_token = NULL, "
                "lease_expires_at = NULL, last_error = %s, updated_at = %s "
                "WHERE announcement_id = CAST(%s AS UUID) "
                "AND recipient_kind = %s AND chat_id = %s "
                "AND status = 'processing' AND claim_token = CAST(%s AS UUID) "
                f"RETURNING {_RECIPIENT_COLUMNS}",
                (
                    state.terminal_at,
                    state.failure.value,
                    expected.updated_at,
                    str(expected.announcement_id),
                    expected.recipient_kind.value,
                    expected.chat_id,
                    str(decision.claim.capability.token),
                ),
                connection=connection,
            )
            if post_row is None:
                raise RuntimeError(
                    "Locked announcement recipient could not be dead-lettered"
                )
            _require_expected_post_state(
                post_row,
                expected,
                operation="dead-letter settlement",
            )
            if expected.recipient_kind is not AnnouncementRecipientKind.COMPLETION:
                await self._advance_audience_expansion(
                    connection,
                    expected.announcement_id,
                    now=expected.updated_at,
                )
            return True

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

        await db.execute(
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
        async with db.transaction() as connection:
            rows = await db.fetch_all(
                f"SELECT {_RECIPIENT_COLUMNS} "
                "FROM admin.announcement_recipients "
                "WHERE status = 'processing' AND lease_expires_at <= %s "
                "ORDER BY lease_expires_at, announcement_id, recipient_kind, chat_id "
                "FOR UPDATE SKIP LOCKED LIMIT %s",
                (timestamp, limit),
                connection=connection,
            )
            decisions: list[AnnouncementRecipientLeaseRecovered] = []
            """@brief 从真实 processing 行产生的恢复决策 / Recovery decisions produced from real processing rows."""
            for row in rows:
                processing = _restore_recipient(row)
                decision = processing.recover_expired(recovered_at=timestamp)
                recovered = decision.recipient
                state = cast(RetryWaitingAnnouncementRecipient, recovered.state)
                post_row = await db.fetch_one(
                    "UPDATE admin.announcement_recipients SET "
                    "status = 'retry_wait', next_attempt_at = %s, "
                    "claim_token = NULL, lease_expires_at = NULL, "
                    "last_error = %s, updated_at = %s "
                    "WHERE announcement_id = CAST(%s AS UUID) "
                    "AND recipient_kind = %s AND chat_id = %s "
                    "AND status = 'processing' "
                    "AND claim_token = CAST(%s AS UUID) "
                    "AND lease_expires_at <= %s "
                    f"RETURNING {_RECIPIENT_COLUMNS}",
                    (
                        state.next_attempt_at,
                        state.failure.value,
                        recovered.updated_at,
                        str(recovered.announcement_id),
                        recovered.recipient_kind.value,
                        recovered.chat_id,
                        str(decision.capability.token),
                        timestamp,
                    ),
                    connection=connection,
                )
                if post_row is None:
                    raise RuntimeError(
                        "Locked expired announcement lease could not be recovered"
                    )
                _require_expected_post_state(
                    post_row,
                    recovered,
                    operation="lease recovery",
                )
                decisions.append(decision)
            return len(decisions)


def _qualified_recipient_columns(alias: str) -> str:
    """@brief 用内部 SQL alias 限定 recipient 列 / Qualify recipient columns with an internal SQL alias.

    @param alias adapter 内部固定 alias / Adapter-internal fixed alias.
    @return 规范限定列列表 / Canonical qualified column list.
    @note alias 不接受用户输入 / The alias never receives user input.
    """

    return ", ".join(
        f"{alias}.{column.strip()}" for column in _RECIPIENT_COLUMNS.split(",")
    )


def _apply_settlement_decision(
    decision: _SettlementDecision,
    pre_state: AnnouncementRecipient,
) -> AnnouncementRecipient:
    """@brief 在锁定的真实 pre-state 上重放并核对结算 / Replay and verify settlement on the locked real pre-state.

    @param decision claim 产生的封闭决策 / Closed decision produced by the claim.
    @param pre_state repository 锁定并恢复的 processing 聚合 / Processing aggregate locked and restored by the repository.
    @return 从真实 pre-state 计算的精确后态 / Exact post-state calculated from the real pre-state.
    @raise RuntimeError pre-state 与领域决策不一致时抛出 /
        Raised when the pre-state disagrees with the domain decision.
    """

    if pre_state != decision.claim.recipient:
        raise RuntimeError(
            "Announcement settlement decision disagrees with database pre-state"
        )
    try:
        expected = decision.apply_to(pre_state)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "Announcement settlement decision disagrees with database pre-state"
        ) from error
    if expected != decision.recipient:
        raise RuntimeError(
            "Announcement settlement decision disagrees with database pre-state"
        )
    return expected


def _require_expected_post_state(
    row: Sequence[object],
    expected: AnnouncementRecipient,
    *,
    operation: str,
) -> None:
    """@brief restore SQL 后态并与领域决策逐字段比较 / Restore the SQL post-state and compare it field-for-field with the domain decision.

    @param row UPDATE RETURNING 的完整 recipient 行 / Complete recipient row from UPDATE RETURNING.
    @param expected 领域计算后态 / Domain-calculated post-state.
    @param operation 错误上下文 / Error context.
    @return None / None.
    @raise RuntimeError SQL 后态不一致时抛出 / Raised when the SQL post-state disagrees.
    """

    actual = _restore_recipient(row)
    if actual != expected:
        raise RuntimeError(
            f"Announcement {operation} post-state disagrees with domain transition"
        )


def _restore_recipient(row: Sequence[object]) -> AnnouncementRecipient:
    """@brief 从固定 SQL 列序恢复 durable recipient / Restore a durable recipient from the fixed SQL column order.

    @param row 前十六列为完整 recipient 持久化形状 / Row whose first sixteen columns are the complete recipient shape.
    @return 已验证领域聚合 / Validated domain aggregate.
    @raise ValueError 行字段不足或状态矩阵非法时抛出 / Raised for a short row or invalid state matrix.
    """

    if len(row) < 16:
        raise ValueError("Announcement recipient row has fewer than sixteen fields")
    return AnnouncementRecipient.restore(
        announcement_id=AnnouncementId.parse(cast(UUID | str, row[0])),
        recipient_kind=AnnouncementRecipientKind(str(row[1])),
        chat_id=_integer(row[2]),
        message_thread_id=_optional_integer(row[3]),
        reply_to_message_id=_optional_integer(row[4]),
        status=str(row[5]),
        attempt_count=_integer(row[6]),
        next_attempt_at=cast(datetime | None, row[7]),
        claim_token=cast(UUID | str | None, row[8]),
        lease_expires_at=cast(datetime | None, row[9]),
        outbound_message_id=cast(UUID | str | None, row[10]),
        last_error=None if row[11] is None else str(row[11]),
        created_at=cast(datetime, row[12]),
        updated_at=cast(datetime, row[13]),
        expanded_at=cast(datetime | None, row[14]),
        terminal_at=cast(datetime | None, row[15]),
    )


def _dispatch_content(row: Sequence[object]) -> AnnouncementDispatchContent:
    """@brief 从 claim JOIN 列恢复不可变出站内容 / Restore immutable dispatch content from claim JOIN columns.

    @param row recipient 十六列后附公告正文、总数、时间和终态计数 / Recipient columns followed by body, total, time, and terminal counts.
    @return 已验证出站内容 / Validated dispatch content.
    @raise ValueError 行字段不足或计数非法时抛出 / Raised for a short row or invalid counts.
    """

    if len(row) < 21:
        raise ValueError("Announcement claim row has fewer than twenty-one fields")
    return AnnouncementDispatchContent(
        body=str(row[16]),
        counts=AnnouncementDeliveryCounts(
            recipients=_integer(row[17]),
            delivered=_integer(row[19]),
            failed=_integer(row[20]),
        ),
        announcement_created_at=cast(datetime, row[18]),
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


__all__ = [
    "AnnouncementIdempotencyConflict",
    "PostgresAdminAnnouncementOperations",
]
