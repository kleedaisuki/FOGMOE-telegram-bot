"""音乐搜索与持久会话的外部端口 / External ports for music search and durable sessions."""

from datetime import datetime
from typing import Protocol

from fogmoe_bot.domain.media.music import (
    MusicPlatform,
    MusicSearchId,
    MusicSearchSession,
    MusicTrack,
)


class MusicSource(Protocol):
    """音乐搜索上游端口 / Music-search upstream port."""

    async def search(
        self,
        query: str,
        platform: MusicPlatform,
        *,
        limit: int,
    ) -> tuple[MusicTrack, ...]:
        """搜索有界歌曲结果 / Search a bounded track result."""

        ...


class MusicSessionRepository(Protocol):
    """持久化音乐 callback 会话端口 / Durable music-callback session port."""

    async def save(self, session: MusicSearchSession) -> None: ...

    async def load(
        self,
        search_id: MusicSearchId,
        *,
        now: datetime,
    ) -> MusicSearchSession | None: ...
