"""@brief Retrieval/pgvector 的真实 PostgreSQL 契约 / Real-PostgreSQL contract for retrieval and pgvector."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from fogmoe_bot.application.retrieval import EpisodicPassageRenderer
from fogmoe_bot.application.retrieval.ports import StaleVectorClaimError
from fogmoe_bot.domain.retrieval import EmbeddingSpace, EmbeddingVector
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.retrieval import (
    PostgresEpisodicSource,
    PostgresRetrievalStore,
)
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


async def _insert_episode(
    *,
    user_id: int,
    turn_id: str,
    activity_id: str,
    suffix: str,
    text: str,
    occurred_at: datetime,
) -> None:
    """@brief 插入一个完整私聊 Turn / Insert one complete private turn."""

    conversation_id = f"assistant-user:{user_id}"
    request = {
        "task_kind": "assistant",
        "user": {"user_id": user_id},
        "scope": {"is_group": False},
    }
    async with db_connection.transaction() as connection:
        await db_connection.execute(
            "INSERT INTO identity.users (id, tg_uid, provider, name) "
            "VALUES (%s, %s, 'telegram', %s)",
            (user_id, user_id, f"retrieval-{suffix}"),
            connection=connection,
        )
        await db_connection.execute(
            "INSERT INTO conversation.conversation_turns "
            "(turn_id, conversation_id, state, created_at, updated_at, completed_at, "
            "source_kind, source_key) VALUES (CAST(%s AS UUID), %s, 'delivered', "
            "%s, %s, %s, 'scheduled.prompt', %s)",
            (
                turn_id,
                conversation_id,
                occurred_at,
                occurred_at,
                occurred_at,
                f"retrieval:{suffix}",
            ),
            connection=connection,
        )
        await db_connection.execute(
            "INSERT INTO conversation.inference_activities "
            "(activity_id, turn_id, conversation_id, request, status, version, "
            "attempt_count, next_attempt_at, claim_token, lease_expires_at, "
            "completion_token, created_at, updated_at, completed_at, traceparent) "
            "VALUES (CAST(%s AS UUID), CAST(%s AS UUID), %s, CAST(%s AS JSONB), "
            "'completed', 1, 1, NULL, NULL, NULL, CAST(%s AS UUID), %s, %s, %s, %s)",
            (
                activity_id,
                turn_id,
                conversation_id,
                json.dumps(request),
                str(uuid4()),
                occurred_at,
                occurred_at,
                occurred_at,
                "00-11111111111111111111111111111111-2222222222222222-01",
            ),
            connection=connection,
        )
        for sequence, (role, content) in enumerate(
            (("user", text), ("assistant", f"remembered {text}")),
            start=1,
        ):
            await db_connection.execute(
                "INSERT INTO conversation.conversation_messages "
                "(message_id, conversation_id, sequence, turn_id, role, content, "
                "idempotency_key, created_at) VALUES (CAST(%s AS UUID), %s, %s, "
                "CAST(%s AS UUID), %s, CAST(%s AS JSONB), %s, %s)",
                (
                    str(uuid4()),
                    conversation_id,
                    sequence,
                    turn_id,
                    role,
                    json.dumps({"text": content}),
                    f"retrieval:{suffix}:{role}",
                    occurred_at + timedelta(microseconds=sequence),
                ),
                connection=connection,
            )


async def _cleanup(*, user_ids: tuple[int, ...], space_id: str) -> None:
    """@brief 依赖级联删除测试资料 / Delete test data through dependency cascades."""

    async with db_connection.transaction() as connection:
        conversation_ids = [f"assistant-user:{user_id}" for user_id in user_ids]
        await db_connection.execute(
            "DELETE FROM conversation.inference_activities "
            "WHERE conversation_id = ANY(CAST(%s AS TEXT[]))",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.conversation_messages "
            "WHERE conversation_id = ANY(CAST(%s AS TEXT[]))",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM conversation.conversation_turns "
            "WHERE conversation_id = ANY(CAST(%s AS TEXT[]))",
            (conversation_ids,),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM identity.users WHERE id = ANY(CAST(%s AS BIGINT[]))",
            (list(user_ids),),
            connection=connection,
        )
        await db_connection.execute(
            "DELETE FROM retrieval.embedding_spaces WHERE space_id = %s",
            (space_id,),
            connection=connection,
        )


def test_real_pgvector_projection_fencing_and_tenant_filtered_exact_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 实库验证投影、fencing、1024 维与检索前租户过滤 / Verify projection, fencing, 1024 dimensions, and pre-search tenant filtering."""

    async def scenario() -> None:
        """@brief 执行真实数据库场景 / Execute the real-database scenario."""

        monkeypatch.setattr(config, "SQLALCHEMY_DATABASE_URI", _postgres_url())
        await db.dispose_current_engine()
        suffix = uuid4().hex
        owner_a = 6_000_000_000_000_000_000 + int(suffix[:10], 16)
        owner_b = owner_a + 1
        turn_a = str(uuid4())
        turn_b = str(uuid4())
        activity_a = str(uuid4())
        activity_b = str(uuid4())
        now = datetime(2033, 1, 1, tzinfo=UTC)
        format_version = int(suffix[:7], 16) + 2
        space = EmbeddingSpace(
            space_id=f"test.{suffix[:16]}",
            model="test/embedding",
            dimensions=1024,
            query_instruction="Retrieve test evidence.",
            passage_format_version=format_version,
        )
        store = PostgresRetrievalStore()
        renderer = EpisodicPassageRenderer(format_version=format_version)
        try:
            await _insert_episode(
                user_id=owner_a,
                turn_id=turn_a,
                activity_id=activity_a,
                suffix=f"{suffix}:a",
                text="owner A drinks tea",
                occurred_at=now,
            )
            await _insert_episode(
                user_id=owner_b,
                turn_id=turn_b,
                activity_id=activity_b,
                suffix=f"{suffix}:b",
                text="owner B drinks coffee",
                occurred_at=now + timedelta(seconds=1),
            )
            await store.ensure_space(space)
            episodes = await PostgresEpisodicSource().read_unprojected(
                format_version=format_version,
                limit=128,
            )
            selected = tuple(
                episode
                for episode in episodes
                if episode.owner_user_id in {owner_a, owner_b}
            )
            assert {str(episode.turn_id) for episode in selected} == {turn_a, turn_b}
            await asyncio.gather(
                *(
                    store.project_turn(
                        episode,
                        renderer.render(episode),
                        space=space,
                        projected_at=now + timedelta(seconds=2),
                    )
                    for episode in selected
                    for _ in range(4)
                )
            )
            claims = await store.claim_vectors(
                space=space,
                now=now + timedelta(seconds=3),
                limit=10,
                lease_for=timedelta(seconds=30),
            )
            assert len(claims) == 2
            for claim in claims:
                vector = (
                    EmbeddingVector((1.0, *([0.0] * 1023)))
                    if claim.passage.owner_user_id == owner_a
                    else EmbeddingVector((0.0, 1.0, *([0.0] * 1022)))
                )
                await store.complete_vector(
                    claim,
                    vector,
                    completed_at=now + timedelta(seconds=4),
                )
            with pytest.raises(StaleVectorClaimError):
                await store.complete_vector(
                    claims[0],
                    EmbeddingVector((1.0, *([0.0] * 1023))),
                    completed_at=now + timedelta(seconds=5),
                )
            evidence = await store.search(
                owner_user_id=owner_a,
                corpus_id="conversation.episodic",
                space=space,
                query_vector=EmbeddingVector((0.0, 1.0, *([0.0] * 1022))),
                limit=5,
            )
            assert len(evidence) == 1
            assert evidence[0].passage.owner_user_id == owner_a
            assert str(evidence[0].passage.source_id) == turn_a
            assert evidence[0].cosine_distance == pytest.approx(1.0)
        finally:
            await _cleanup(user_ids=(owner_a, owner_b), space_id=space.space_id)
            await db.dispose_current_engine()

    asyncio.run(scenario())
