"""@brief Admin 公告的真实 PostgreSQL 并发、重放与 fencing 契约 / Real-PostgreSQL concurrency, replay, and fencing contracts for Admin announcements."""

from __future__ import annotations

import asyncio
from observability_testkit import make_telemetry
from datetime import UTC, datetime, timedelta
import json
import os
from uuid import uuid4

import pytest

from fogmoe_bot.application.admin.models import RequestAnnouncement
from fogmoe_bot.infrastructure.admin.announcements import (
    AnnouncementIdempotencyConflict,
    PostgresAdminAnnouncementOperations,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.standalone_outbound import (
    PostgresStandaloneOutboundCapability,
)
from fogmoe_bot.domain.conversation.identity import OutboundMessageId
from fogmoe_bot.presentation.telegram.admin_handlers import (
    TelegramAnnouncementOutboundFactory,
)


def _user_id(offset: int) -> int:
    """@brief 生成不与 Telegram 用户冲突的 BIGINT / Generate a BIGINT disjoint from Telegram users.

    @param offset 测试内偏移 / In-test offset.
    @return 正用户 ID / Positive user ID.
    """

    return 8_300_000_000_000_000_000 + int(uuid4().hex[:10], 16) * 10 + offset


def _update_id() -> int:
    """@brief 生成测试 Update ID / Generate a test Update ID.

    @return 非负 BIGINT / Non-negative BIGINT.
    """

    return 7_000_000_000_000_000_000 + int(uuid4().hex[:12], 16)


def test_admin_announcement_snapshot_is_concurrent_replayable_and_fenced() -> None:
    """@brief 并发接收只创建一份快照，过期 token 不能终结新租约 / Concurrent acceptance creates one snapshot and an expired token cannot finalize a new lease."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        """@brief 驱动真实数据库契约 / Drive the real-database contract.

        @return None / None.
        """

        users = (_user_id(1), _user_id(2))
        update_id = _update_id()
        now = datetime.now(UTC)
        key = f"admin-pg:announcement:{uuid4().hex}"
        conversation_id = f"assistant-user:{users[0]}"
        announcement_conversation: str | None = None
        operations = PostgresAdminAnnouncementOperations()
        outbound = PostgresStandaloneOutboundCapability(
            telemetry=make_telemetry(),
        )
        factory = TelegramAnnouncementOutboundFactory()
        command = RequestAnnouncement(
            actor_id=users[0],
            source_update_id=update_id,
            idempotency_key=key,
            body="durable announcement",
            reply_chat_id=users[0],
            reply_message_id=99,
            reply_message_thread_id=None,
            requested_at=now,
        )
        try:
            await db_connection.execute(
                "INSERT INTO identity.users "
                "(id, tg_uid, provider, name, coins, coins_paid, user_plan) "
                "VALUES (%s, %s, 'telegram', 'admin-pg-a', 0, 0, 'free'), "
                "(%s, %s, 'telegram', 'admin-pg-b', 0, 0, 'free')",
                (users[0], users[0], users[1], users[1]),
            )
            await db_connection.execute(
                "INSERT INTO conversation.inbound_updates "
                "(update_id, conversation_id, payload, status, version, attempt_count, "
                "next_attempt_at, received_at, updated_at) "
                "VALUES (%s, %s, CAST(%s AS JSONB), 'pending', 0, 0, %s, %s, %s)",
                (
                    update_id,
                    conversation_id,
                    json.dumps({"update_id": update_id}),
                    now,
                    now,
                    now,
                ),
            )

            first, second = await asyncio.wait_for(
                asyncio.gather(operations.accept(command), operations.accept(command)),
                timeout=5,
            )
            assert first.announcement_id == second.announcement_id
            assert {first.inserted, second.inserted} == {True, False}
            assert first.recipient_count == second.recipient_count
            assert first.recipient_count >= 2
            announcement_conversation = f"admin-announcement:{first.announcement_id}"
            known_snapshot = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM admin.announcement_recipients "
                "WHERE announcement_id = CAST(%s AS UUID) "
                "AND recipient_kind = 'user' AND chat_id = ANY(%s)",
                (str(first.announcement_id), users),
            )
            assert known_snapshot is not None and int(known_snapshot[0]) == 2
            completion = await db_connection.fetch_one(
                "SELECT status FROM admin.announcement_recipients "
                "WHERE announcement_id = CAST(%s AS UUID) "
                "AND recipient_kind = 'completion' AND chat_id = %s",
                (str(first.announcement_id), users[0]),
            )
            assert completion is not None and str(completion[0]) == "blocked"

            concurrent_claims = await asyncio.gather(
                operations.claim_ready(
                    now=now,
                    lease_for=timedelta(seconds=1),
                    limit=1,
                ),
                operations.claim_ready(
                    now=now,
                    lease_for=timedelta(seconds=1),
                    limit=1,
                ),
            )
            old_claims = tuple(claim for batch in concurrent_claims for claim in batch)
            assert len(old_claims) == 2
            old_identities = {
                (claim.recipient_kind, claim.chat_id) for claim in old_claims
            }
            assert len(old_identities) == 2

            recovered = await operations.recover_expired(
                now=now + timedelta(seconds=2),
                limit=10,
            )
            assert recovered >= 2
            reclaimed = await operations.claim_ready(
                now=now + timedelta(seconds=2),
                lease_for=timedelta(minutes=1),
                limit=10,
            )
            new_claim = next(
                claim
                for claim in reclaimed
                if (claim.recipient_kind, claim.chat_id) in old_identities
            )
            old_claim = next(
                claim
                for claim in old_claims
                if (claim.recipient_kind, claim.chat_id)
                == (new_claim.recipient_kind, new_claim.chat_id)
            )
            assert new_claim.claim_token != old_claim.claim_token

            outbound_command = factory.build(new_claim)
            await outbound.enqueue(outbound_command)
            deterministic_id = OutboundMessageId.for_conversation(
                outbound_command.conversation_id,
                outbound_command.idempotency_key,
            )
            assert not await operations.mark_expanded(
                old_claim,
                outbound_message_id=deterministic_id,
                completed_at=now + timedelta(seconds=3),
            )
            assert await operations.mark_expanded(
                new_claim,
                outbound_message_id=deterministic_id,
                completed_at=now + timedelta(seconds=3),
            )

            replay = await operations.accept(command)
            assert (
                not replay.inserted and replay.announcement_id == first.announcement_id
            )
            with pytest.raises(AnnouncementIdempotencyConflict):
                await operations.accept(
                    RequestAnnouncement(
                        actor_id=command.actor_id,
                        source_update_id=command.source_update_id,
                        idempotency_key=command.idempotency_key,
                        body="different body",
                        reply_chat_id=command.reply_chat_id,
                        reply_message_id=command.reply_message_id,
                        reply_message_thread_id=None,
                        requested_at=command.requested_at,
                    )
                )
        finally:
            acceptance_row = await db_connection.fetch_one(
                "SELECT announcement_id FROM admin.announcements "
                "WHERE idempotency_key = %s",
                (key,),
            )
            if acceptance_row is not None:
                await db_connection.execute(
                    "DELETE FROM admin.announcements WHERE announcement_id = CAST(%s AS UUID)",
                    (str(acceptance_row[0]),),
                )
            if announcement_conversation is not None:
                await db_connection.execute(
                    "DELETE FROM conversation.outbound_messages WHERE conversation_id = %s",
                    (announcement_conversation,),
                )
            await db_connection.execute(
                "DELETE FROM conversation.inbound_updates WHERE update_id = %s",
                (update_id,),
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id = ANY(%s)",
                (users,),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
