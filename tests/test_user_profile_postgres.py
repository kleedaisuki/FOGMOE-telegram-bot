"""@brief User Profile Dreaming 的真实 PostgreSQL 契约 / Real-PostgreSQL contract for User Profile Dreaming."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from postgres_test_support import configure_bot_database

from fogmoe_bot.application.user_profile.ports import (
    DreamProfileUnchanged,
    DreamProfileUpdated,
)
from fogmoe_bot.domain.user_profile.dream import DreamResult, StaleDreamClaimError
from fogmoe_bot.domain.user_profile.models import (
    ProfileClaimKind,
    ProfileConfidence,
    ProfileEvidence,
    ProfileMetadata,
    ProfilePatch,
    UpsertProfileClaim,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.user_profile.store import (
    PostgresUserProfileStore,
)


def _postgres_url() -> str:
    """@brief 读取显式隔离 DSN / Read an explicit isolated DSN."""

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    pytest.skip("set FOGMOE_TEST_DATABASE_URL to run the real PostgreSQL contract")


def test_projection_job_claim_and_revision_converge_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 并发 projection/enqueue/claim 收敛到一次 Profile revision / Concurrent projection, enqueue, and claim converge to one Profile revision."""

    async def scenario() -> None:
        """@brief 执行真实 PostgreSQL 状态机 / Execute the real PostgreSQL state machine."""

        await db.dispose_current_engine()
        configure_bot_database(_postgres_url())
        suffix = uuid4().hex
        user_id = 7_000_000_000_000_000_000 + int(suffix[:12], 16)
        turn_id = uuid4()
        now = datetime(2000, 1, 1, tzinfo=UTC) + timedelta(
            microseconds=int(suffix[:12], 16) % 1_000_000
        )
        store = PostgresUserProfileStore()
        try:
            async with db.transaction() as connection:
                await db.execute(
                    "INSERT INTO identity.users (id, tg_uid, provider, name) "
                    "VALUES (%s, %s, 'telegram', %s)",
                    (user_id, user_id, f"profile-{suffix}"),
                    connection=connection,
                )
                await db.execute(
                    "INSERT INTO conversation.conversation_turns "
                    "(turn_id, conversation_id, state, created_at, updated_at, completed_at, "
                    "source_kind, source_key) VALUES (CAST(%s AS UUID), %s, 'delivered', "
                    "%s, %s, %s, 'scheduled.prompt', %s)",
                    (
                        str(turn_id),
                        f"assistant-user:{user_id}",
                        now,
                        now,
                        now,
                        f"profile:{suffix}",
                    ),
                    connection=connection,
                )
            source = ProfileEvidence(
                event_id=0,
                source_turn_id=turn_id,
                owner_user_id=user_id,
                user_text="I prefer green tea",
                assistant_text="I will remember that",
                occurred_at=now,
                metadata=ProfileMetadata("Klee", "klee", "CS researcher"),
            )

            await asyncio.gather(
                *(store.project_evidence(source, projected_at=now) for _ in range(8))
            )
            evidence_count = await db.fetch_one(
                "SELECT COUNT(*) FROM user_profile.evidence_events WHERE owner_user_id = %s",
                (user_id,),
            )
            assert evidence_count is not None and evidence_count[0] == 1

            unrelated_ready = await db.fetch_one(
                "SELECT "
                "(SELECT COUNT(*) FROM user_profile.profiles "
                "WHERE user_id <> %s AND next_eligible_at <= %s) + "
                "(SELECT COUNT(*) FROM user_profile.dreams "
                "WHERE user_id <> %s AND status IN ('pending','retry_wait') "
                "AND next_attempt_at <= %s)",
                (user_id, now, user_id, now),
            )
            assert unrelated_ready is not None and unrelated_ready[0] == 0

            enqueued = await store.enqueue_eligible(
                now=now,
                limit=1,
                max_events_per_dream=16,
                max_evidence_chars=60_000,
            )
            assert enqueued == 1

            claimed_batches = await asyncio.gather(
                *(
                    store.claim_dreams(
                        now=now,
                        limit=1,
                        lease_for=timedelta(minutes=2),
                    )
                    for _ in range(8)
                )
            )
            claims = tuple(claim for batch in claimed_batches for claim in batch)
            assert len(claims) == 1
            expired_claim = claims[0]
            assert (
                await store.recover_expired_dream_leases(
                    now=now + timedelta(minutes=1),
                    max_attempts=5,
                    limit=1,
                )
                == 0
            )
            assert (
                await store.recover_expired_dream_leases(
                    now=now + timedelta(minutes=2),
                    max_attempts=5,
                    limit=1,
                )
                == 1
            )
            reclaimed_batches = await asyncio.gather(
                *(
                    store.claim_dreams(
                        now=now + timedelta(minutes=2),
                        limit=1,
                        lease_for=timedelta(minutes=2),
                    )
                    for _ in range(8)
                )
            )
            reclaimed = tuple(claim for batch in reclaimed_batches for claim in batch)
            assert len(reclaimed) == 1
            claim = reclaimed[0]
            assert claim.token != expired_claim.token
            assert (
                claim.activity.attempt_count == expired_claim.activity.attempt_count + 1
            )
            event_id = claim.evidence[0].event_id
            result = DreamResult(
                ProfilePatch(
                    (
                        UpsertProfileClaim(
                            key="drink.preference",
                            kind=ProfileClaimKind.PREFERENCE,
                            statement="偏好绿茶",
                            confidence=ProfileConfidence.EXPLICIT,
                            evidence_event_ids=(event_id,),
                        ),
                    )
                ),
                "test:profile-model",
                1,
            )
            with pytest.raises(StaleDreamClaimError):
                await store.complete_dream(
                    expired_claim.prepare_completion(
                        result,
                        completed_at=now + timedelta(minutes=2, seconds=1),
                    ),
                    refresh_after=timedelta(hours=6),
                )
            changed_receipt = await store.complete_dream(
                claim.prepare_completion(
                    result,
                    completed_at=now + timedelta(minutes=2, seconds=1),
                ),
                refresh_after=timedelta(hours=6),
            )

            assert isinstance(changed_receipt, DreamProfileUpdated)
            completed = changed_receipt.snapshot
            assert completed.revision == 1
            pinned = await store.read_profile(user_id)
            assert pinned == completed
            assert pinned.document.claims[0].statement == "偏好绿茶"
            with pytest.raises(StaleDreamClaimError):
                await store.complete_dream(
                    claim.prepare_completion(
                        result,
                        completed_at=now + timedelta(minutes=2, seconds=2),
                    ),
                    refresh_after=timedelta(hours=6),
                )

            second_turn_id = uuid4()
            second_occurred_at = now + timedelta(minutes=3)
            async with db.transaction() as connection:
                await db.execute(
                    "INSERT INTO conversation.conversation_turns "
                    "(turn_id, conversation_id, state, created_at, updated_at, "
                    "completed_at, source_kind, source_key) VALUES "
                    "(CAST(%s AS UUID), %s, 'delivered', %s, %s, %s, "
                    "'scheduled.prompt', %s)",
                    (
                        str(second_turn_id),
                        f"assistant-user:{user_id}",
                        second_occurred_at,
                        second_occurred_at,
                        second_occurred_at,
                        f"profile-no-op:{suffix}",
                    ),
                    connection=connection,
                )
            await store.project_evidence(
                ProfileEvidence(
                    event_id=0,
                    source_turn_id=second_turn_id,
                    owner_user_id=user_id,
                    user_text="No preference change",
                    assistant_text="Understood",
                    occurred_at=second_occurred_at,
                    metadata=ProfileMetadata("Klee", "klee", "CS researcher"),
                ),
                projected_at=second_occurred_at,
            )
            next_pass_at = now + timedelta(hours=7)
            assert (
                await store.enqueue_eligible(
                    now=next_pass_at,
                    limit=1,
                    max_events_per_dream=16,
                    max_evidence_chars=60_000,
                )
                == 1
            )
            no_op_claims = await store.claim_dreams(
                now=next_pass_at,
                limit=1,
                lease_for=timedelta(minutes=2),
            )
            assert len(no_op_claims) == 1
            no_op_claim = no_op_claims[0]
            no_op_receipt = await store.complete_dream(
                no_op_claim.prepare_completion(
                    DreamResult(ProfilePatch(), "test:profile-model", 1),
                    completed_at=next_pass_at + timedelta(seconds=1),
                ),
                refresh_after=timedelta(hours=6),
            )

            assert isinstance(no_op_receipt, DreamProfileUnchanged)
            assert no_op_receipt.retained_revision == completed.revision
            assert (
                no_op_receipt.scheduler_head_event_id
                == no_op_claim.activity.through_event_id
            )
            pinned_after_no_op = await store.read_profile(user_id)
            assert pinned_after_no_op == completed
            assert (
                pinned_after_no_op.observed_through_event_id
                == claim.activity.through_event_id
            )
            head_row = await db.fetch_one(
                "SELECT observed_through_event_id FROM user_profile.profiles "
                "WHERE user_id = %s",
                (user_id,),
            )
            assert head_row is not None
            assert head_row[0] == no_op_receipt.scheduler_head_event_id
        finally:
            await db.execute(
                "DELETE FROM identity.users WHERE id = %s",
                (user_id,),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
