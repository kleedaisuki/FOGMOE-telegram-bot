"""Booru 图库 HTTP adapter / Booru picture-gallery HTTP adapter."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import aiohttp

from fogmoe_bot.domain.media.picture import PictureCandidate, PictureRating
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind
from fogmoe_bot.infrastructure.network.proxy import create_aiohttp_session

from .common import HEADERS, optional_str


logger = logging.getLogger(__name__)

_ENDPOINTS: tuple[str, ...] = (
    "https://konachan.net/post.json",
    "https://konachan.com/post.json",
    "https://yande.re/post.json",
)


@dataclass(slots=True)
class _Circuit:
    """单端点 403 熔断状态 / Per-endpoint 403 circuit state."""

    forbidden_until: float = 0


class BooruPictureSource:
    """具有实例级熔断和有界重试的 booru 图库 / Booru gallery with instance-owned circuit breaking and bounded retries."""

    def __init__(
        self,
        *,
        telemetry: Telemetry,
        endpoints: Sequence[str] = _ENDPOINTS,
        timeout_seconds: float = 10,
        retry_count: int = 3,
        retry_delay_seconds: float = 1,
        forbidden_cooldown_seconds: float = 30 * 60,
    ) -> None:
        if not endpoints:
            raise ValueError("at least one gallery endpoint is required")
        if min(timeout_seconds, retry_delay_seconds, forbidden_cooldown_seconds) <= 0:
            raise ValueError("gallery time bounds must be positive")
        if retry_count <= 0:
            raise ValueError("retry_count must be positive")
        self._endpoints = tuple(endpoints)
        self._timeout = timeout_seconds
        self._retry_count = retry_count
        self._retry_delay = retry_delay_seconds
        self._forbidden_cooldown = forbidden_cooldown_seconds
        self._circuits = {endpoint: _Circuit() for endpoint in self._endpoints}
        self._telemetry = telemetry

    async def fetch(
        self,
        rating: PictureRating,
        *,
        limit: int,
    ) -> tuple[PictureCandidate, ...]:
        """获取规范图库批次 / Fetch a canonical gallery batch."""

        with self._telemetry.span(
            "media.picture.fetch",
            kind=SpanKind.CLIENT,
            attributes={
                "fogmoe.dependency.name": "booru",
                "picture.rating": rating.value,
            },
        ):
            try:
                value = await self._fetch(rating, limit=limit)
            except Exception:
                self._telemetry.counter(
                    MetricName.DEPENDENCY_OUTCOMES,
                    attributes={
                        "outcome": Outcome.FAILURE,
                        "fogmoe.dependency.name": "booru",
                    },
                )
                raise
            self._telemetry.counter(
                MetricName.DEPENDENCY_OUTCOMES,
                attributes={
                    "outcome": Outcome.SUCCESS,
                    "fogmoe.dependency.name": "booru",
                },
            )
            return value

    async def _fetch(
        self,
        rating: PictureRating,
        *,
        limit: int,
    ) -> tuple[PictureCandidate, ...]:
        """@brief 执行实际图库查询 / Execute the actual gallery query.

        @param rating 内容分级 / Content rating.
        @param limit 最大候选数 / Maximum candidate count.
        @return 图片候选 / Picture candidates.
        """

        bounded_limit = min(max(limit, 1), 200)
        params = {
            "limit": str(bounded_limit),
            "tags": (
                "rating:questionable" if rating is PictureRating.NSFW else "rating:safe"
            ),
            "order": "random",
        }
        now = time.monotonic()
        for endpoint in self._endpoints:
            circuit = self._circuits[endpoint]
            if circuit.forbidden_until > now:
                continue
            for attempt in range(self._retry_count):
                try:
                    response = await self._request(endpoint, params)
                except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                    logger.warning(
                        "Gallery request failed endpoint=%s: %s", endpoint, error
                    )
                else:
                    status, payload = response
                    if status == 403:
                        circuit.forbidden_until = (
                            time.monotonic() + self._forbidden_cooldown
                        )
                        break
                    if status == 200:
                        pictures = _parse_pictures(payload, rating, bounded_limit)
                        if pictures:
                            return pictures
                if attempt + 1 < self._retry_count:
                    await asyncio.sleep(self._retry_delay)
        return _fallback_pictures(rating)

    async def _request(
        self,
        endpoint: str,
        params: Mapping[str, str],
    ) -> tuple[int, object]:
        """执行一次图库 HTTP 请求 / Execute one gallery HTTP request."""

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with create_aiohttp_session() as session:
            async with session.get(
                endpoint,
                params=params,
                headers=HEADERS,
                timeout=timeout,
            ) as response:
                if response.status != 200:
                    return response.status, None
                return response.status, await response.json(content_type=None)


def _parse_pictures(
    payload: object,
    rating: PictureRating,
    limit: int,
) -> tuple[PictureCandidate, ...]:
    """严格解析图库 JSON / Strictly parse gallery JSON."""

    if not isinstance(payload, list):
        return ()
    result: list[PictureCandidate] = []
    for index, raw in enumerate(payload[:limit]):
        if not isinstance(raw, dict):
            continue
        sample_url = optional_str(raw.get("sample_url"))
        file_url = optional_str(raw.get("file_url"))
        if not sample_url and not file_url:
            continue
        source_id = optional_str(raw.get("id")) or optional_str(raw.get("md5"))
        if not source_id:
            source_id = f"row-{index}-{hash((sample_url, file_url)) & 0xFFFFFFFF:x}"
        try:
            result.append(
                PictureCandidate(
                    source_id=source_id,
                    sample_url=sample_url,
                    file_url=file_url,
                    tags=optional_str(raw.get("tags")) or "",
                    width=_optional_int(raw.get("width")),
                    height=_optional_int(raw.get("height")),
                    file_size=_optional_int(raw.get("file_size")),
                    score=_optional_int(raw.get("score")),
                    rating=rating,
                )
            )
        except ValueError:
            continue
    return tuple(result)


def _optional_int(value: object) -> int | None:
    """安全转换整数 / Safely convert an integer."""

    try:
        return int(str(value)) if value is not None else None
    except ValueError:
        return None


def _fallback_pictures(rating: PictureRating) -> tuple[PictureCandidate, ...]:
    """返回既有静态 fallback / Return the established static fallback."""

    if rating is PictureRating.NSFW:
        rows = (
            (
                "backup-nsfw-1",
                "https://konachan.net/sample/9ef08c3e40591a6d118edbd5a36b534f/Konachan.com%20-%20341083%20sample.jpg",
                "https://konachan.net/image/9ef08c3e40591a6d118edbd5a36b534f/Konachan.com%20-%20341083%20anthropomorphism%20azur_lane%20breasts%20brown_eyes.jpg",
            ),
            (
                "backup-nsfw-2",
                "https://konachan.net/sample/3c1ac17a13b9214d26fec2ad9683f425/Konachan.com%20-%20340831%20sample.jpg",
                "https://konachan.net/image/3c1ac17a13b9214d26fec2ad9683f425/Konachan.com%20-%20340831%20anthropomorphism%20aqua_eyes%20azur_lane.jpg",
            ),
        )
    else:
        rows = (
            (
                "backup-safe-1",
                "https://konachan.net/sample/e2739d73cde2f5e6f70ece824838247e/Konachan.com%20-%20341231%20sample.jpg",
                "https://konachan.net/image/e2739d73cde2f5e6f70ece824838247e/Konachan.com%20-%20341231%20animal%20bird%20fish%20nobody%20original%20scenic%20signed%20sunset%20water.jpg",
            ),
            (
                "backup-safe-2",
                "https://konachan.net/sample/c76f10765c5a35c0af224a7607fb767a/Konachan.com%20-%20340969%20sample.jpg",
                "https://konachan.net/image/c76f10765c5a35c0af224a7607fb767a/Konachan.com%20-%20340969%20animal%20bird%20cat%20grass%20nobody%20original%20tree.jpg",
            ),
        )
    return tuple(
        PictureCandidate(
            source_id=source_id,
            sample_url=sample_url,
            file_url=file_url,
            tags="",
            width=None,
            height=None,
            file_size=None,
            score=None,
            rating=rating,
        )
        for source_id, sample_url, file_url in rows
    )
