"""音乐服务的持久 callback 与分页语义测试 / Durable callback and pagination semantics for the music service."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from fogmoe_bot.application.media.music_runtime import MusicRuntime
from fogmoe_bot.application.media.music_service import MusicPage, MusicService
from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.music import (
    MusicPlatform,
    MusicSearchId,
    MusicSearchSession,
    MusicTrack,
)


@dataclass(frozen=True)
class Profile:
    """@brief 测试媒体准入快照 / Test media-admission snapshot."""

    registered: bool = True
    permission: int = 2


class Accounts:
    """固定账户 profile / Fixed account profiles."""

    async def profile(self, user_id: UserId) -> Profile:
        return Profile()


class Sessions:
    """内存持久会话仓储 / In-memory durable-session repository."""

    def __init__(self) -> None:
        self.values: dict[MusicSearchId, MusicSearchSession] = {}

    async def save(self, session: MusicSearchSession) -> None:
        self.values[session.search_id] = session

    async def load(
        self,
        search_id: MusicSearchId,
        *,
        now: datetime,
    ) -> MusicSearchSession | None:
        return self.values.get(search_id)


class Source:
    """固定音乐搜索上游 / Fixed music-search upstream."""

    async def search(
        self,
        query: str,
        platform: MusicPlatform,
        *,
        limit: int,
    ) -> tuple[MusicTrack, ...]:
        return tuple(
            MusicTrack(str(index), f"{query}-{index}", "artist", "album", platform)
            for index in range(8)
        )


def test_music_callback_uses_opaque_persisted_session() -> None:
    """callback 只携带短 token，结果可从仓储恢复 / Callback carries a short token and reloads persisted results."""

    async def scenario() -> None:
        sessions = Sessions()
        ids = iter(("a" * 32, "b" * 32))
        service = MusicService(
            accounts=Accounts(),
            sessions=sessions,
            source=Source(),
            runtime=MusicRuntime(),
            id_factory=lambda: next(ids),
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        result = await service.search(user_id=UserId(1), query="long song query")
        assert isinstance(result, MusicPage)
        assert len(str(result.session.search_id)) == 32
        assert result.total_pages == 2
        recovered = await service.page(
            user_id=UserId(2),
            search_id=result.session.search_id,
            page=2,
        )
        assert isinstance(recovered, MusicPage)
        assert recovered.page == 2
        switched = await service.switch_platform(
            user_id=UserId(2),
            search_id=result.session.search_id,
            platform=MusicPlatform.QQ,
            page=1,
        )
        assert isinstance(switched, MusicPage)
        assert switched.session.platform is MusicPlatform.QQ

    asyncio.run(scenario())
