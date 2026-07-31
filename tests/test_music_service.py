"""音乐服务的持久 callback 与分页语义测试 / Durable callback and pagination semantics for the music service."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from fogmoe_bot.application.media.music_runtime import MusicRuntime
from fogmoe_bot.application.media.music_service import MusicHelp, MusicService
from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.music import (
    MusicPage,
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


class FallbackSource:
    """@brief 记录主查询与回退顺序的音乐上游 / Music upstream recording primary and fallback order."""

    def __init__(self) -> None:
        """@brief 创建空调用记录 / Create an empty call log."""

        self.calls: list[tuple[str, MusicPlatform, int]] = []

    async def search(
        self,
        query: str,
        platform: MusicPlatform,
        *,
        limit: int,
    ) -> tuple[MusicTrack, ...]:
        """@brief 仅为规范回退查询返回结果 / Return a result only for the canonical fallback query."""

        self.calls.append((query, platform, limit))
        if query != "alpha beta":
            return ()
        return (MusicTrack("1", "song", "artist", "album", platform),)


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


def test_music_search_preserves_normalization_fallback_and_persistence_order() -> None:
    """@brief 服务按领域搜索计划调用上游，再持久化主查询 / Service follows the domain search plan before persisting the primary query."""

    async def scenario() -> None:
        sessions = Sessions()
        source = FallbackSource()
        now = datetime(2026, 1, 1, tzinfo=UTC)
        service = MusicService(
            accounts=Accounts(),
            sessions=sessions,
            source=source,
            runtime=MusicRuntime(),
            id_factory=lambda: "c" * 32,
            now=lambda: now,
        )

        result = await service.search(
            user_id=UserId(9),
            query="  alpha   beta gamma delta  ",
        )

        assert isinstance(result, MusicPage)
        assert source.calls == [
            ("alpha beta gamma delta", MusicPlatform.NETEASE, 20),
            ("alpha beta", MusicPlatform.NETEASE, 20),
        ]
        assert result.session.query.text == "alpha beta gamma delta"
        assert result.session.expires_at == now.replace(minute=30)
        assert sessions.values[result.session.search_id] == result.session

    asyncio.run(scenario())


def test_music_help_short_circuits_accounts_rate_limit_and_upstream() -> None:
    """@brief help 查询在任何外部端口之前返回 / Help queries return before any external port."""

    class UnexpectedAccounts:
        """@brief 被调用即失败的账户端口 / Account port that fails if called."""

        async def profile(self, user_id: UserId) -> Profile:
            """@brief 防止 help 路径访问账户 / Prevent account access on the help path."""

            raise AssertionError(f"unexpected account lookup for {user_id}")

    async def scenario() -> None:
        source = FallbackSource()
        result = await MusicService(
            accounts=UnexpectedAccounts(),
            sessions=Sessions(),
            source=source,
            runtime=MusicRuntime(),
        ).search(user_id=UserId(9), query="  HELP ")

        assert isinstance(result, MusicHelp)
        assert source.calls == []

    asyncio.run(scenario())
