"""PostgreSQL 持久音乐搜索会话适配器 / PostgreSQL durable music-search-session adapter."""

import json
from datetime import datetime
from typing import cast
from uuid import UUID

from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.music import (
    MusicPlatform,
    MusicSearchId,
    MusicSearchSession,
    MusicTrack,
)
from fogmoe_bot.infrastructure.database import db

from .common import utc


class PostgresMusicSessionRepository:
    """持久化可跨进程恢复的音乐 callback 会话 / Persist music callback sessions recoverable across processes."""

    async def save(self, session: MusicSearchSession) -> None:
        """upsert 音乐会话并保留每用户最近二十条 / Upsert a session and retain twenty per user."""

        payload = json.dumps(
            [
                {
                    "id": track.track_id,
                    "name": track.name,
                    "artist": track.artist,
                    "album": track.album,
                    "platform": track.platform.value,
                }
                for track in session.tracks
            ],
            ensure_ascii=False,
        )
        async with db.transaction() as connection:
            await db.execute(
                "INSERT INTO media.music_sessions "
                "(search_id, requester_id, query, platform, tracks, expires_at, created_at, updated_at) "
                "VALUES (CAST(%s AS UUID), %s, %s, %s, CAST(%s AS JSONB), %s, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                "ON CONFLICT (search_id) DO UPDATE SET platform = EXCLUDED.platform, "
                "tracks = EXCLUDED.tracks, expires_at = EXCLUDED.expires_at, "
                "updated_at = CURRENT_TIMESTAMP",
                (
                    str(session.search_id),
                    int(session.requester_id),
                    session.query,
                    session.platform.value,
                    payload,
                    utc(session.expires_at),
                ),
                connection=connection,
            )
            await db.execute(
                "WITH stale AS ("
                "SELECT search_id FROM media.music_sessions WHERE requester_id = %s "
                "ORDER BY created_at DESC OFFSET 20) "
                "DELETE FROM media.music_sessions WHERE search_id IN (SELECT search_id FROM stale)",
                (int(session.requester_id),),
                connection=connection,
            )

    async def load(
        self,
        search_id: MusicSearchId,
        *,
        now: datetime,
    ) -> MusicSearchSession | None:
        """读取未过期音乐会话 / Load an unexpired music session."""

        row = await db.fetch_one(
            "SELECT search_id, requester_id, query, platform, tracks, expires_at "
            "FROM media.music_sessions WHERE search_id = CAST(%s AS UUID) AND expires_at > %s",
            (str(search_id), utc(now)),
        )
        if row is None:
            return None
        raw_tracks = row[4]
        decoded: object = (
            json.loads(raw_tracks) if isinstance(raw_tracks, str) else raw_tracks
        )
        tracks: list[MusicTrack] = []
        if isinstance(decoded, list):
            for item in decoded:
                if not isinstance(item, dict):
                    continue
                try:
                    tracks.append(
                        MusicTrack(
                            track_id=str(item["id"]),
                            name=str(item["name"]),
                            artist=str(item.get("artist") or "未知"),
                            album=str(item.get("album") or "未知"),
                            platform=MusicPlatform(str(item["platform"])),
                        )
                    )
                except KeyError, ValueError:
                    continue
        return MusicSearchSession(
            search_id=MusicSearchId(UUID(str(row[0])).hex),
            requester_id=UserId(int(row[1])),
            query=str(row[2]),
            platform=MusicPlatform(str(row[3])),
            tracks=tuple(tracks),
            expires_at=utc(cast(datetime, row[5])),
        )
