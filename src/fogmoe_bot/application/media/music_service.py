"""持久化音乐搜索、翻页与平台切换用例 / Durable music search, pagination, and platform switching."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.music import (
    MusicPlatform,
    MusicSearchId,
    MusicSearchSession,
    MusicTrack,
)

from .account import MediaAccountProfiles
from .music_ports import MusicSessionRepository, MusicSource
from .music_runtime import MusicRuntime


MUSIC_SERVICE_DATA_KEY = "media.music.service"


@dataclass(frozen=True, slots=True)
class MusicPolicy:
    """音乐搜索、分页与会话的显式边界 / Explicit bounds for music search, pagination, and sessions."""

    result_limit: int = 20
    session_ttl: timedelta = timedelta(minutes=30)
    page_size: int = 5
    query_chars: int = 200

    def __post_init__(self) -> None:
        """校验音乐容量与时限 / Validate music capacities and duration."""

        if min(self.result_limit, self.page_size, self.query_chars) <= 0:
            raise ValueError("music policy bounds must be positive")
        if self.session_ttl <= timedelta(0):
            raise ValueError("music session TTL must be positive")


@dataclass(frozen=True, slots=True)
class MusicHelp:
    """展示音乐帮助 / Show music help."""


@dataclass(frozen=True, slots=True)
class MusicNotRegistered:
    """音乐请求用户未注册 / Music requester is not registered."""


@dataclass(frozen=True, slots=True)
class MusicRateLimited:
    """音乐交互被限流 / Music interaction is rate-limited."""

    retry_after_seconds: int


@dataclass(frozen=True, slots=True)
class MusicSessionExpired:
    """音乐 callback 会话已过期 / Music callback session expired."""


@dataclass(frozen=True, slots=True)
class MusicUnavailable:
    """音乐上游无结果或暂不可用 / Music upstream returned no result or is unavailable."""


@dataclass(frozen=True, slots=True)
class MusicPage:
    """可渲染音乐结果页 / Renderable music-result page."""

    session: MusicSearchSession
    page: int
    total_pages: int
    tracks: tuple[MusicTrack, ...]


type MusicResult = (
    MusicHelp
    | MusicNotRegistered
    | MusicRateLimited
    | MusicSessionExpired
    | MusicUnavailable
    | MusicPage
)
type IdFactory = Callable[[], str]
type UtcNow = Callable[[], datetime]


def _utc_now() -> datetime:
    """读取系统 UTC 时间 / Read system UTC time."""

    return datetime.now(UTC)


class MusicService:
    """协调持久会话与有界音乐上游 / Coordinate durable sessions and a bounded music upstream."""

    def __init__(
        self,
        *,
        accounts: MediaAccountProfiles,
        sessions: MusicSessionRepository,
        source: MusicSource,
        runtime: MusicRuntime,
        policy: MusicPolicy = MusicPolicy(),
        id_factory: IdFactory = lambda: uuid.uuid4().hex,
        now: UtcNow = _utc_now,
    ) -> None:
        self._accounts = accounts
        self._sessions = sessions
        self._source = source
        self._runtime = runtime
        self._policy = policy
        self._id_factory = id_factory
        self._now = now

    async def search(
        self,
        *,
        user_id: UserId,
        query: str,
    ) -> MusicResult:
        """创建持久化音乐搜索会话 / Create a durable music-search session."""

        normalized = " ".join(query.split())[: self._policy.query_chars]
        if not normalized or normalized.casefold() == "help":
            return MusicHelp()
        profile = await self._accounts.profile(user_id)
        if not profile.registered:
            return MusicNotRegistered()
        limited = await self._rate_limit(user_id)
        if limited is not None:
            return limited
        platform = MusicPlatform.NETEASE
        tracks = await self._search_tracks(normalized, platform)
        if not tracks and len(normalized.split()) > 1:
            words = normalized.split()
            tracks = await self._search_tracks(
                " ".join(words[: max(1, len(words) // 2)]),
                platform,
            )
        if not tracks:
            return MusicUnavailable()
        session = MusicSearchSession(
            search_id=MusicSearchId(self._id_factory()),
            requester_id=user_id,
            query=normalized,
            platform=platform,
            tracks=tracks,
            expires_at=self._now() + self._policy.session_ttl,
        )
        await self._sessions.save(session)
        return self._page(session, 1)

    async def page(
        self,
        *,
        user_id: UserId,
        search_id: MusicSearchId,
        page: int,
    ) -> MusicResult:
        """读取一个持久化音乐页 / Read one durable music page."""

        limited = await self._rate_limit(user_id)
        if limited is not None:
            return limited
        session = await self._sessions.load(search_id, now=self._now())
        if session is None:
            return MusicSessionExpired()
        return self._page(session, page)

    async def switch_platform(
        self,
        *,
        user_id: UserId,
        search_id: MusicSearchId,
        platform: MusicPlatform,
        page: int,
    ) -> MusicResult:
        """切换音乐平台并持久化新结果 / Switch platform and persist the new result."""

        limited = await self._rate_limit(user_id)
        if limited is not None:
            return limited
        current = await self._sessions.load(search_id, now=self._now())
        if current is None:
            return MusicSessionExpired()
        tracks = await self._search_tracks(current.query, platform)
        if not tracks:
            return MusicUnavailable()
        updated = MusicSearchSession(
            search_id=current.search_id,
            requester_id=current.requester_id,
            query=current.query,
            platform=platform,
            tracks=tracks,
            expires_at=self._now() + self._policy.session_ttl,
        )
        await self._sessions.save(updated)
        return self._page(updated, page)

    async def _search_tracks(
        self,
        query: str,
        platform: MusicPlatform,
    ) -> tuple[MusicTrack, ...]:
        """通过有界 cache/bulkhead 搜索歌曲 / Search tracks through bounded cache and bulkhead."""

        key = (query.casefold(), platform)
        cached = await self._runtime.results.get(key)
        if cached is not None:
            return cached
        try:
            tracks = await self._runtime.upstream_bulkhead.run(
                lambda: self._source.search(
                    query,
                    platform,
                    limit=self._policy.result_limit,
                )
            )
        except Exception:
            return ()
        await self._runtime.results.put(key, tracks)
        return tracks

    async def _rate_limit(self, user_id: UserId) -> MusicRateLimited | None:
        """应用音乐交互限流 / Apply music-interaction rate limiting."""

        allowed, retry_after = await self._runtime.rate_limit.admit(user_id)
        if allowed:
            return None
        return MusicRateLimited(retry_after or 1)

    def _page(self, session: MusicSearchSession, requested_page: int) -> MusicPage:
        """从会话切出一个规范页 / Slice one canonical page from a session."""

        total_pages = max(
            1,
            (len(session.tracks) + self._policy.page_size - 1)
            // self._policy.page_size,
        )
        page = min(max(requested_page, 1), total_pages)
        start = (page - 1) * self._policy.page_size
        return MusicPage(
            session=session,
            page=page,
            total_pages=total_pages,
            tracks=session.tracks[start : start + self._policy.page_size],
        )
