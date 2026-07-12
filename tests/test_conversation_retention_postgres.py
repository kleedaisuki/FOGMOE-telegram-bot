"""@brief Conversation retention 的真实 PostgreSQL 契约 / Real-PostgreSQL contracts for conversation retention."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    TurnId,
)
from fogmoe_bot.domain.conversation.retention import (
    RetentionSegmentDraft,
    RetentionSummary,
    StaleRetentionClaimError,
    TokenCount,
)
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.conversation_retention import (
    PostgresConversationRetention,
)
from fogmoe_dbctl.postgres import read_service, service_sqlalchemy_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


def _postgres_url() -> str:
    """@brief 读取显式隔离 DSN 或本地测试 service / Read an explicit isolated DSN or the local test service.

    @return async SQLAlchemy URL / Async SQLAlchemy URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")
    config_dir = PROJECT_ROOT / "var/psql"
    if not (config_dir / "pg_service.conf").is_file():
        pytest.skip("local PostgreSQL service configuration is unavailable")
    return service_sqlalchemy_url(read_service(config_dir, "fogmoe_automation"))


async def _insert_fixture(
    *,
    user_id: int,
    conversation_id: ConversationId,
    prior_turn_id: TurnId,
    anchor_turn_id: TurnId,
    source_suffix: str,
    now: datetime,
) -> None:
    """@brief 建立账户、两个 Turn 与三条 append-only 消息 / Create an account, two Turns, and three append-only messages.

    @return None / None.
    """

    async with db_connection.transaction() as connection:
        await db_connection.execute(
            "INSERT INTO identity.users "
            "(id, tg_uid, provider, name, permanent_records_limit) "
            "VALUES (%s, %s, 'telegram', %s, 1)",
            (user_id, user_id, f"retention_{source_suffix}"),
            connection=connection,
        )
        await db_connection.execute(
            "INSERT INTO conversation.conversation_turns ("
            "turn_id, conversation_id, state, created_at, updated_at, completed_at, "
            "source_kind, source_key) VALUES ("
            "CAST(%s AS UUID), %s, 'delivered', %s, %s, %s, "
            "'scheduled.prompt', %s), ("
            "CAST(%s AS UUID), %s, 'waiting_inference', %s, %s, NULL, "
            "'scheduled.prompt', %s)",
            (
                str(prior_turn_id),
                str(conversation_id),
                now,
                now,
                now,
                f"retention-prior:{source_suffix}",
                str(anchor_turn_id),
                str(conversation_id),
                now + timedelta(seconds=1),
                now + timedelta(seconds=1),
                f"retention-anchor:{source_suffix}",
            ),
            connection=connection,
        )
        for sequence, (turn_id, content) in enumerate(
            (
                (prior_turn_id, "one"),
                (prior_turn_id, "two"),
                (anchor_turn_id, "current"),
            ),
            start=1,
        ):
            await db_connection.execute(
                "INSERT INTO conversation.conversation_messages ("
                "message_id, conversation_id, sequence, turn_id, role, content, "
                "idempotency_key, created_at) VALUES ("
                "CAST(%s AS UUID), %s, %s, CAST(%s AS UUID), 'user', "
                "CAST(%s AS JSONB), %s, %s)",
                (
                    str(uuid4()),
                    str(conversation_id),
                    sequence,
                    str(turn_id),
                    json.dumps({"text": content}),
                    f"retention:{source_suffix}:message:{sequence}",
                    now + timedelta(microseconds=sequence),
                ),
                connection=connection,
            )


async def _insert_completed_legacy(
    *,
    draft: RetentionSegmentDraft,
    summary: RetentionSummary,
) -> None:
    """@brief 插入一个已完成 legacy Segment 以验证 quota window / Insert a completed legacy segment to verify the quota window.

    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO conversation.retention_segments ("
        "segment_id, kind, conversation_id, owner_user_id, projection_version, "
        "source_digest, source_snapshot, source_row_count, source_token_count, "
        "legacy_record_id, status, completion_token, summary_text, "
        "summary_token_count, summary_route_key, created_at, updated_at, completed_at"
        ") VALUES ("
        "CAST(%s AS UUID), 'legacy_archive', %s, %s, 0, %s, CAST(%s AS JSONB), "
        "%s, %s, %s, 'completed', CAST(%s AS UUID), %s, %s, %s, %s, %s, %s)",
        (
            str(draft.segment_id),
            str(draft.conversation_id),
            draft.owner_user_id,
            draft.source_digest,
            json.dumps(draft.source_snapshot, ensure_ascii=False),
            draft.source_row_count,
            int(draft.source_token_count),
            draft.legacy_record_id,
            str(draft.segment_id),
            summary.text,
            int(summary.token_count),
            summary.route_key,
            draft.created_at,
            draft.created_at,
            draft.created_at,
        ),
    )


async def _cleanup(user_id: int, conversation_id: ConversationId) -> None:
    """@brief 按外键顺序删除 retention fixture / Delete the retention fixture in foreign-key order.

    @return None / None.
    """

    async with db_connection.transaction() as connection:
        await db_connection.execute(
            "DELETE FROM conversation.retention_segments WHERE owner_user_id = %s",
            (user_id,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.conversation_messages WHERE conversation_id = %s",
            (str(conversation_id),),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.conversation_turns WHERE conversation_id = %s",
            (str(conversation_id),),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM identity.users WHERE id = %s",
            (user_id,),
            connection=connection,
        )


def test_real_postgres_enqueue_fencing_and_paid_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 并发 enqueue 只建一段，旧 lease 被 fencing，付费 limit 控制可见记录 / Concurrent enqueue creates one segment, stale leases are fenced, and the paid limit controls visibility.

    @param monkeypatch 临时绑定隔离 DSN / Temporarily bind the isolated DSN.
    """

    async def scenario() -> None:
        """@brief 执行真实数据库契约 / Execute the real-database contract.

        @return None / None.
        """

        monkeypatch.setattr(config, "SQLALCHEMY_DATABASE_URI", _postgres_url())
        await db.dispose_current_engine()
        suffix = uuid4().hex
        user_id = 5_000_000_000_000_000_000 + int(suffix[:12], 16)
        conversation_id = ConversationId(f"assistant-user:{user_id}")
        prior_turn_id = TurnId.new()
        anchor_turn_id = TurnId.new()
        now = datetime(2031, 1, 1, tzinfo=UTC)
        repository = PostgresConversationRetention()
        try:
            await _insert_fixture(
                user_id=user_id,
                conversation_id=conversation_id,
                prior_turn_id=prior_turn_id,
                anchor_turn_id=anchor_turn_id,
                source_suffix=suffix,
                now=now,
            )
            bounds = await repository.history_bounds(
                conversation_id,
                through_turn_id=anchor_turn_id,
            )
            assert bounds is not None
            assert (bounds.first_sequence, bounds.last_sequence) == (3, 3)
            assert bounds.epoch_floor_sequence == 0

            draft = RetentionSegmentDraft.compaction(
                conversation_id=conversation_id,
                owner_user_id=user_id,
                epoch_floor_sequence=0,
                from_sequence=1,
                through_sequence=2,
                anchor_turn_id=anchor_turn_id,
                predecessor_segment_id=None,
                projection_version=1,
                source_snapshot=(
                    {
                        "role": "user",
                        "content": {
                            "text": "one",
                            "large_float": 1e20,
                            "small_float": 0.000001,
                            "integral_float": 1.0,
                        },
                    },
                    {"role": "user", "content": "two"},
                ),
                source_row_count=2,
                source_token_count=TokenCount(2),
                created_at=now + timedelta(seconds=2),
            )
            first, second = await asyncio.gather(
                repository.enqueue_compaction(draft),
                repository.enqueue_compaction(draft),
            )
            assert {first.inserted, second.inserted} == {False, True}
            assert first.segment.segment_id == second.segment.segment_id

            claimed = await repository.claim_compactions(
                now=now + timedelta(seconds=3),
                limit=2,
                lease_for=timedelta(seconds=5),
            )
            assert len(claimed) == 1
            stale_claim = claimed[0]
            assert (
                await repository.claim_compactions(
                    now=now + timedelta(seconds=9),
                    limit=1,
                    lease_for=timedelta(seconds=5),
                )
                == ()
            )
            reclaimed = await repository.claim_compactions(
                now=now + timedelta(seconds=10),
                limit=1,
                lease_for=timedelta(seconds=5),
            )
            assert len(reclaimed) == 1
            assert reclaimed[0].claim_token != stale_claim.claim_token
            with pytest.raises(StaleRetentionClaimError):
                await repository.complete_compaction(
                    stale_claim,
                    summary=RetentionSummary("stale", TokenCount(1), "test:stale"),
                    completed_at=now + timedelta(seconds=11),
                )

            completed = await repository.complete_compaction(
                reclaimed[0],
                summary=RetentionSummary("cumulative", TokenCount(1), "test:model"),
                completed_at=now + timedelta(seconds=12),
            )
            assert completed.summary is not None
            assert completed.summary.text == "cumulative"

            legacy = RetentionSegmentDraft.legacy_archive(
                legacy_record_id=int(suffix[:10], 16) + 1,
                conversation_id=conversation_id,
                owner_user_id=user_id,
                source_snapshot=({"role": "user", "content": "newest"},),
                source_token_count=TokenCount(1),
                created_at=now + timedelta(seconds=20),
            )
            await _insert_completed_legacy(
                draft=legacy,
                summary=RetentionSummary("newest", TokenCount(1), "test:legacy"),
            )
            visible = await repository.fetch_visible_segments(
                user_id,
                newest_first=True,
                limit=10,
                offset=0,
            )
            assert [segment.segment_id for segment in visible] == [legacy.segment_id]
            assert await repository.count_visible_summaries(user_id) == 1

            await db_connection.execute(
                "UPDATE identity.users SET permanent_records_limit = 0 WHERE id = %s",
                (user_id,),
            )
            assert (
                await repository.fetch_visible_segments(
                    user_id,
                    newest_first=True,
                    limit=10,
                    offset=0,
                )
                == ()
            )
            assert await repository.count_visible_summaries(user_id) == 0
            await db_connection.execute(
                "UPDATE identity.users SET permanent_records_limit = 2 WHERE id = %s",
                (user_id,),
            )
            expanded = await repository.fetch_visible_segments(
                user_id,
                newest_first=True,
                limit=10,
                offset=0,
            )
            assert [segment.segment_id for segment in expanded] == [
                legacy.segment_id,
                completed.segment_id,
            ]
        finally:
            await _cleanup(user_id, conversation_id)
            await db.dispose_current_engine()

    asyncio.run(scenario())
