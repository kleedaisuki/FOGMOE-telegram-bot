"""@brief Durable Admin 公告投递 PostgreSQL adapter / PostgreSQL adapter for durable Admin announcement delivery."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.admin.models import (
    AnnouncementAcceptance,
    RequestAnnouncement,
)
from fogmoe_bot.domain.admin.announcement import (
    Announcement,
    AnnouncementAudienceProgress,
    AnnouncementAudienceSnapshot,
    AnnouncementAudienceSnapshotted,
    AnnouncementDeliveryCompleted,
    AnnouncementDeliveryCounts,
    AnnouncementId,
    AnnouncementIntent,
    AnnouncementIntentMismatch,
    CompletedAnnouncement,
    ExpandingAnnouncement,
)
from fogmoe_bot.domain.admin.recipient import (
    AnnouncementClaimToken,
    AnnouncementRecipient,
    AnnouncementRecipientClaim,
    AnnouncementRecipientDeadLettered,
    AnnouncementRecipientExpanded,
    AnnouncementRecipientKind,
    AnnouncementRecipientLeaseRecovered,
    AnnouncementRecipientRetryScheduled,
    ExpandedAnnouncementRecipient,
    FailedAnnouncementRecipient,
    PendingAnnouncementRecipient,
    RetryWaitingAnnouncementRecipient,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.admin.announcement_persistence import (
    _ANNOUNCEMENT_COLUMNS,
    _RECIPIENT_COLUMNS,
    _announcement_intent,
    _completion_address,
    _dispatch_content,
    _integer,
    _qualified_announcement_columns,
    _qualified_recipient_columns,
    _require_expected_announcement_post_state,
    _require_expected_post_state,
    _require_positive_limit,
    _restore_announcement,
    _restore_joined_announcement,
    _restore_recipient,
    _utc,
)

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

        intent = _announcement_intent(command)
        initial = Announcement.start(intent)
        async with db.transaction() as connection:
            inserted_row = await db.fetch_one(
                "INSERT INTO admin.announcements "
                "(announcement_id, idempotency_key, requested_by, source_update_id, "
                "body, recipient_count, state, created_at, updated_at) "
                "VALUES (CAST(%s AS UUID), %s, %s, %s, %s, 0, 'expanding', %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING "
                f"RETURNING {_ANNOUNCEMENT_COLUMNS}",
                (
                    str(initial.announcement_id),
                    intent.idempotency_key,
                    intent.requested_by,
                    intent.source_update_id,
                    intent.body,
                    intent.requested_at,
                    intent.requested_at,
                ),
                connection=connection,
            )
            inserted = inserted_row is not None
            if inserted_row is not None:
                persisted_initial = _restore_announcement(
                    inserted_row,
                    completion_address=intent.completion_address,
                )
                if persisted_initial != initial:
                    raise RuntimeError(
                        "Inserted announcement disagrees with domain initial state"
                    )
                await self._snapshot_audience(
                    connection,
                    announcement=persisted_initial,
                )
            row = await db.fetch_one(
                f"SELECT {_qualified_announcement_columns('announcement')}, "
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
            announcement = _restore_joined_announcement(row)
            self._validate_replay(announcement, intent)
            return AnnouncementAcceptance(
                announcement_id=announcement.announcement_id,
                recipient_count=announcement.recipient_count,
                inserted=inserted,
            )

    async def _snapshot_audience(
        self,
        connection: AsyncConnection,
        *,
        announcement: Announcement,
    ) -> AnnouncementAudienceSnapshotted:
        """@brief 在意图事务内固化用户和群组受众 / Materialize user and group audiences inside the intent transaction.

        @param connection 意图事务连接 / Intent-transaction connection.
        @param announcement INSERT RETURNING 恢复的初态 / Initial state restored from INSERT RETURNING.
        @return sealed 受众快照决策 / Sealed audience-snapshot decision.
        """

        intent = announcement.intent
        address = intent.completion_address
        parameters = (
            str(announcement.announcement_id),
            intent.requested_at,
            intent.requested_at,
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
            (*parameters, intent.requested_at),
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
            (*parameters, intent.requested_at),
            connection=connection,
        )
        await db.execute(
            "INSERT INTO admin.announcement_recipients "
            "(announcement_id, recipient_kind, chat_id, message_thread_id, "
            "reply_to_message_id, status, next_attempt_at, created_at, updated_at) "
            "VALUES (CAST(%s AS UUID), 'completion', %s, %s, %s, "
            "'blocked', NULL, %s, %s)",
            (
                str(announcement.announcement_id),
                address.chat_id,
                address.message_thread_id,
                address.reply_to_message_id,
                intent.requested_at,
                intent.requested_at,
            ),
            connection=connection,
        )
        count_row = await db.fetch_one(
            "SELECT COUNT(*) FROM admin.announcement_recipients "
            "WHERE announcement_id = CAST(%s AS UUID) "
            "AND recipient_kind IN ('user', 'group')",
            (str(announcement.announcement_id),),
            connection=connection,
        )
        if count_row is None:
            raise RuntimeError("Announcement audience snapshot returned no count")
        decision = announcement.record_audience_snapshot(
            AnnouncementAudienceSnapshot(_integer(count_row[0])),
            recorded_at=intent.requested_at,
        )
        expected = decision.announcement
        post_row = await db.fetch_one(
            "UPDATE admin.announcements SET recipient_count = %s, state = %s, "
            "updated_at = %s WHERE announcement_id = CAST(%s AS UUID) "
            "AND state = %s AND recipient_count = %s "
            f"RETURNING {_ANNOUNCEMENT_COLUMNS}",
            (
                expected.recipient_count,
                expected.status.value,
                expected.updated_at,
                str(expected.announcement_id),
                announcement.status.value,
                announcement.recipient_count,
            ),
            connection=connection,
        )
        if post_row is None:
            raise RuntimeError("Inserted announcement audience could not be recorded")
        _require_expected_announcement_post_state(
            post_row,
            expected,
            completion_address=announcement.intent.completion_address,
            operation="audience snapshot",
        )
        return decision

    def _validate_replay(
        self,
        announcement: Announcement,
        intent: AnnouncementIntent,
    ) -> None:
        """@brief 拒绝同键不同义的公告重放 / Reject an announcement replay with different semantics.

        @param announcement 已恢复的真实主聚合 / Restored real main aggregate.
        @param intent 重放的领域意图 / Replayed domain intent.
        @return None / None.
        @raise AnnouncementIdempotencyConflict 语义不同 / Semantics differ.
        """

        try:
            announcement.require_same_intent(intent)
        except AnnouncementIntentMismatch as error:
            raise AnnouncementIdempotencyConflict(
                "Announcement idempotency key already denotes another intent"
            ) from error

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
                f"""
                SELECT {_qualified_announcement_columns("announcement")},
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
            decisions: list[AnnouncementDeliveryCompleted] = []
            """@brief 由锁定公告与 blocked completion 产生的复合决策 / Compound decisions produced from locked announcements and blocked completions."""
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
                announcement = _restore_announcement(
                    candidate,
                    completion_address=_completion_address(blocked),
                )
                counts = AnnouncementDeliveryCounts(
                    recipients=announcement.recipient_count,
                    delivered=_integer(candidate[10]),
                    failed=_integer(candidate[11]),
                )
                decision = announcement.complete_delivery(
                    counts,
                    completion_recipient=blocked,
                    completed_at=timestamp,
                )
                expected_announcement = decision.announcement
                completed_state = cast(
                    CompletedAnnouncement,
                    expected_announcement.state,
                )
                announcement_row = await db.fetch_one(
                    "UPDATE admin.announcements AS announcement SET "
                    "state = 'completed', completed_at = %s, updated_at = %s "
                    "WHERE announcement.announcement_id = CAST(%s AS UUID) "
                    "AND announcement.state = 'delivering' "
                    f"RETURNING {_ANNOUNCEMENT_COLUMNS}",
                    (
                        completed_state.completed_at,
                        expected_announcement.updated_at,
                        str(expected_announcement.announcement_id),
                    ),
                    connection=connection,
                )
                if announcement_row is None:
                    raise RuntimeError(
                        "Locked delivering announcement could not be completed"
                    )
                _require_expected_announcement_post_state(
                    announcement_row,
                    expected_announcement,
                    completion_address=announcement.intent.completion_address,
                    operation="delivery completion",
                )
                release = decision.completion_release
                expected_completion = release.recipient
                pending_state = cast(
                    PendingAnnouncementRecipient,
                    expected_completion.state,
                )
                completion_row = await db.fetch_one(
                    "UPDATE admin.announcement_recipients AS completion SET "
                    "status = 'pending', next_attempt_at = %s, updated_at = %s "
                    "WHERE completion.announcement_id = CAST(%s AS UUID) "
                    "AND completion.recipient_kind = 'completion' "
                    "AND completion.chat_id = %s "
                    "AND completion.status = 'blocked' "
                    f"RETURNING {_qualified_recipient_columns('completion')}",
                    (
                        pending_state.next_attempt_at,
                        expected_completion.updated_at,
                        str(expected_completion.announcement_id),
                        expected_completion.chat_id,
                    ),
                    connection=connection,
                )
                if completion_row is None:
                    raise RuntimeError(
                        "Locked announcement completion could not be released"
                    )
                _require_expected_post_state(
                    completion_row,
                    expected_completion,
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
                raise RuntimeError(
                    "Locked announcement recipient could not be expanded"
                )
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

        row = await db.fetch_one(
            f"SELECT {_qualified_announcement_columns('announcement')}, "
            "completion.chat_id, completion.message_thread_id, "
            "completion.reply_to_message_id "
            "FROM admin.announcements AS announcement "
            "JOIN admin.announcement_recipients AS completion "
            "ON completion.announcement_id = announcement.announcement_id "
            "AND completion.recipient_kind = 'completion' "
            "WHERE announcement.announcement_id = CAST(%s AS UUID) "
            "FOR UPDATE OF announcement",
            (str(announcement_id),),
            connection=connection,
        )
        if row is None:
            raise RuntimeError("Announcement expansion returned no main aggregate")
        announcement = _restore_joined_announcement(row)
        if not isinstance(announcement.state, ExpandingAnnouncement):
            return
        progress_row = await db.fetch_one(
            "SELECT COUNT(*), COUNT(*) FILTER ("
            "WHERE status IN ('expanded', 'failed_final')) "
            "FROM admin.announcement_recipients "
            "WHERE announcement_id = CAST(%s AS UUID) "
            "AND recipient_kind IN ('user', 'group')",
            (str(announcement_id),),
            connection=connection,
        )
        if progress_row is None:
            raise RuntimeError("Announcement audience progress returned no counts")
        progress = AnnouncementAudienceProgress(
            recipient_count=_integer(progress_row[0]),
            terminal_count=_integer(progress_row[1]),
        )
        if progress.terminal_count != progress.recipient_count:
            return
        decision = announcement.finish_audience_expansion(
            progress,
            finished_at=now,
        )
        expected = decision.announcement
        post_row = await db.fetch_one(
            "UPDATE admin.announcements AS announcement SET "
            "state = 'delivering', updated_at = %s "
            "WHERE announcement.announcement_id = CAST(%s AS UUID) "
            "AND announcement.state = 'expanding' "
            "AND announcement.recipient_count = %s "
            f"RETURNING {_ANNOUNCEMENT_COLUMNS}",
            (
                expected.updated_at,
                str(expected.announcement_id),
                announcement.recipient_count,
            ),
            connection=connection,
        )
        if post_row is None:
            raise RuntimeError("Locked expanding announcement could not begin delivery")
        _require_expected_announcement_post_state(
            post_row,
            expected,
            completion_address=announcement.intent.completion_address,
            operation="audience expansion",
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


__all__ = [
    "AnnouncementIdempotencyConflict",
    "PostgresAdminAnnouncementOperations",
]
