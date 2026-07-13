"""@brief User Profile Dreaming 的真实 PostgreSQL 契约 / Real-PostgreSQL contract for User Profile Dreaming."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from fogmoe_bot.application.user_profile.ports import DreamResult, StaleDreamClaimError
from fogmoe_bot.domain.user_profile.models import (
    ProfileClaimKind,
    ProfileConfidence,
    ProfileDocument,
    ProfileEvidence,
    ProfileMetadata,
    ProfilePatch,
    UpsertProfileClaim,
    apply_profile_patch,
)
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.user_profile.store import PostgresUserProfileStore
from fogmoe_dbctl.postgres import read_service, service_sqlalchemy_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


def _postgres_url() -> str:
    """@brief 读取显式隔离 DSN 或本地 automation service / Read an isolated DSN or local automation service."""

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")
    config_dir = PROJECT_ROOT / "var/psql"
    if not (config_dir / "pg_service.conf").is_file():
        pytest.skip("local PostgreSQL service configuration is unavailable")
    return service_sqlalchemy_url(read_service(config_dir, "fogmoe_automation"))


def test_projection_job_claim_and_revision_converge_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 并发 projection/enqueue/claim 收敛到一次 Profile revision / Concurrent projection, enqueue, and claim converge to one Profile revision."""

    async def scenario() -> None:
        """@brief 执行真实 PostgreSQL 状态机 / Execute the real PostgreSQL state machine."""

        monkeypatch.setattr(config, "SQLALCHEMY_DATABASE_URI", _postgres_url())
        await db.dispose_current_engine()
        suffix = uuid4().hex
        user_id = 7_000_000_000_000_000_000 + int(suffix[:12], 16)
        turn_id = uuid4()
        now = datetime(2036, 2, 3, 4, 5, tzinfo=UTC)
        store = PostgresUserProfileStore()
        try:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "INSERT INTO identity.users (id, tg_uid, provider, name) "
                    "VALUES (%s, %s, 'telegram', %s)",
                    (user_id, user_id, f"profile-{suffix}"),
                    connection=connection,
                )
                await db_connection.execute(
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
            evidence_count = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM user_profile.evidence_events WHERE owner_user_id = %s",
                (user_id,),
            )
            assert evidence_count is not None and evidence_count[0] == 1

            enqueued = await asyncio.gather(
                *(
                    store.enqueue_eligible(
                        now=now,
                        limit=4,
                        max_events_per_dream=16,
                        max_evidence_chars=60_000,
                    )
                    for _ in range(8)
                )
            )
            assert sum(enqueued) == 1

            claimed_batches = await asyncio.gather(
                *(
                    store.claim_dreams(
                        now=now,
                        limit=4,
                        lease_for=timedelta(minutes=2),
                    )
                    for _ in range(8)
                )
            )
            claims = tuple(claim for batch in claimed_batches for claim in batch)
            assert len(claims) == 1
            claim = claims[0]
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
            document = apply_profile_patch(
                ProfileDocument(),
                result.patch,
                evidence=claim.evidence,
            )
            completed = await store.complete_dream(
                claim,
                result,
                document=document,
                completed_at=now + timedelta(seconds=1),
                refresh_after=timedelta(hours=6),
            )

            assert completed is not None and completed.revision == 1
            pinned = await store.read_profile(user_id)
            assert pinned == completed
            assert pinned.document.claims[0].statement == "偏好绿茶"
            with pytest.raises(StaleDreamClaimError):
                await store.complete_dream(
                    claim,
                    result,
                    document=document,
                    completed_at=now + timedelta(seconds=2),
                    refresh_after=timedelta(hours=6),
                )
        finally:
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id = %s",
                (user_id,),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
