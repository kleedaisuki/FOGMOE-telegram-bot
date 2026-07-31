"""持久化音乐搜索、翻页与平台切换用例 / Durable music search, pagination, and platform switching."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fogmoe_bot.domain.media.identifiers import UserId
from fogmoe_bot.domain.media.music import (
    MusicPage,
    MusicPlatform,
    MusicSearchId,
    MusicSearchPolicy,
    MusicSearchSession,
    MusicTrack,
)

from .account import MediaAccountProfiles
from .music_ports import MusicSessionRepository, MusicSource
from .music_runtime import MusicRuntime

MUSIC_SERVICE_DATA_KEY = "media.music.service"


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
        policy: MusicSearchPolicy = MusicSearchPolicy(),
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

        search_query = self._policy.query_from(query)
        if search_query.requests_help:
            return MusicHelp()
        profile = await self._accounts.profile(user_id)
        if not profile.registered:
            return MusicNotRegistered()
        limited = await self._rate_limit(user_id)
        if limited is not None:
            return limited
        platform = self._policy.default_platform
        tracks: tuple[MusicTrack, ...] = ()
        for search_term in search_query.search_terms:
            tracks = await self._search_tracks(search_term, platform)
            if tracks:
                break
        if not tracks:
            return MusicUnavailable()
        session = MusicSearchSession.start(
            search_id=MusicSearchId(self._id_factory()),
            requester_id=user_id,
            query=search_query,
            tracks=tracks,
            now=self._now(),
            policy=self._policy,
        )
        await self._sessions.save(session)
        return session.page(1, policy=self._policy)

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
        return session.page(page, policy=self._policy)

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
        tracks = await self._search_tracks(current.query.text, platform)
        if not tracks:
            return MusicUnavailable()
        updated = current.replace_platform_results(
            platform=platform,
            tracks=tracks,
            now=self._now(),
            policy=self._policy,
        )
        await self._sessions.save(updated)
        return updated.page(page, policy=self._policy)

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
