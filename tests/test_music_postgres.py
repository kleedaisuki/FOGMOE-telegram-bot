"""音乐会话的真实 PostgreSQL 持久化测试 / Real-PostgreSQL persistence tests for music sessions."""

import asyncio
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest

from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.music import (
    MusicPlatform,
    MusicSearchId,
    MusicSearchSession,
    MusicTrack,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.media.music import (
    PostgresMusicSessionRepository,
)


def test_music_session_survives_adapter_restart_and_upsert() -> None:
    """音乐会话可跨 adapter 重建并保持既有 upsert 语义 / Sessions survive adapter reconstruction with established upsert semantics."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        requester = UserId(8_300_000_000 + uuid4().int % 100_000_000)
        search_id = MusicSearchId(uuid4().hex)
        now = datetime.now(UTC)
        session = MusicSearchSession(
            search_id=search_id,
            requester_id=requester,
            query="durable query",
            platform=MusicPlatform.NETEASE,
            tracks=(MusicTrack("1", "song", "artist", "album", MusicPlatform.NETEASE),),
            expires_at=now + timedelta(minutes=30),
        )
        repository = PostgresMusicSessionRepository()
        try:
            await db_connection.execute(
                "INSERT INTO identity.users (id, tg_uid, name) VALUES (%s, %s, %s)",
                (int(requester), int(requester), f"music-test-{requester}"),
            )
            await repository.save(session)
            await repository.save(session)
            loaded = await PostgresMusicSessionRepository().load(search_id, now=now)
            assert loaded == session
        finally:
            await db_connection.execute(
                "DELETE FROM media.music_sessions WHERE search_id = CAST(%s AS UUID)",
                (str(search_id),),
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id = %s",
                (int(requester),),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
