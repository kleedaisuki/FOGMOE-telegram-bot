"""音乐聚合 API HTTP adapter / Music-aggregation API HTTP adapter."""

import aiohttp

from fogmoe_bot.application.media.errors import UpstreamUnavailable
from fogmoe_bot.domain.media.music import MusicPlatform, MusicTrack
from fogmoe_bot.infrastructure.network.proxy import create_aiohttp_session

from .common import HEADERS, optional_str


_ENDPOINT = "https://api.jkyai.top/API/hqyyid.php"


class JkyMusicSource:
    """JKY 音乐搜索 API adapter / JKY music-search API adapter."""

    def __init__(
        self, *, endpoint: str = _ENDPOINT, timeout_seconds: float = 10
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._endpoint = endpoint
        self._timeout = timeout_seconds

    async def search(
        self,
        query: str,
        platform: MusicPlatform,
        *,
        limit: int,
    ) -> tuple[MusicTrack, ...]:
        """搜索规范歌曲 / Search canonical tracks."""

        bounded_limit = min(max(limit, 1), 50)
        params = {
            "name": query,
            "type": platform.value,
            "page": "1",
            "limit": str(bounded_limit),
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with create_aiohttp_session() as session:
                async with session.get(
                    self._endpoint,
                    params=params,
                    headers=HEADERS,
                    timeout=timeout,
                ) as response:
                    if response.status != 200:
                        raise UpstreamUnavailable(
                            f"music API returned HTTP {response.status}"
                        )
                    payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise UpstreamUnavailable("music API request failed") from error
        return _parse_tracks(payload, platform, bounded_limit)


def _parse_tracks(
    payload: object,
    platform: MusicPlatform,
    limit: int,
) -> tuple[MusicTrack, ...]:
    """严格解析音乐 JSON / Strictly parse music JSON."""

    if not isinstance(payload, dict) or payload.get("code") != 1:
        raise UpstreamUnavailable("music API returned an unsuccessful payload")
    raw_tracks = payload.get("data")
    if not isinstance(raw_tracks, list):
        raise UpstreamUnavailable("music API data is not a list")
    result: list[MusicTrack] = []
    for raw in raw_tracks[:limit]:
        if not isinstance(raw, dict):
            continue
        track_id = optional_str(raw.get("id"))
        name = optional_str(raw.get("name"))
        if not track_id or not name:
            continue
        raw_platform = optional_str(raw.get("type"))
        try:
            track_platform = MusicPlatform(raw_platform) if raw_platform else platform
        except ValueError:
            track_platform = platform
        result.append(
            MusicTrack(
                track_id=track_id,
                name=name,
                artist=optional_str(raw.get("artist")) or "未知",
                album=optional_str(raw.get("album")) or "未知",
                platform=track_platform,
            )
        )
    return tuple(result)
