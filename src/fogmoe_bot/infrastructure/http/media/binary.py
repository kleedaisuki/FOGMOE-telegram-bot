"""有界图片二进制下载 HTTP adapter / Bounded picture-binary HTTP adapter."""

from urllib.parse import urlparse

import aiohttp

from fogmoe_bot.application.media.errors import ArtifactTooLarge, UpstreamUnavailable
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind
from fogmoe_bot.infrastructure.network.proxy import create_aiohttp_session

from .common import HEADERS


class AiohttpBinaryFetcher:
    """流式、字节有界的 HTTP(S) binary fetcher / Streaming byte-bounded HTTP(S) binary fetcher."""

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        """下载一个有界二进制 / Download one bounded binary."""

        with self._telemetry.span(
            "media.binary.fetch",
            kind=SpanKind.CLIENT,
            attributes={"fogmoe.dependency.name": "media_binary"},
        ):
            try:
                value = await self._fetch(
                    url, max_bytes=max_bytes, timeout_seconds=timeout_seconds
                )
            except Exception:
                self._telemetry.counter(
                    MetricName.DEPENDENCY_OUTCOMES,
                    attributes={
                        "outcome": Outcome.FAILURE,
                        "fogmoe.dependency.name": "media_binary",
                    },
                )
                raise
            self._telemetry.counter(
                MetricName.DEPENDENCY_OUTCOMES,
                attributes={
                    "outcome": Outcome.SUCCESS,
                    "fogmoe.dependency.name": "media_binary",
                },
            )
            return value

    async def _fetch(
        self, url: str, *, max_bytes: int, timeout_seconds: float
    ) -> bytes:
        """@brief 执行实际有界下载 / Execute the actual bounded download.

        @param url 目标 URL / Target URL.
        @param max_bytes 最大响应字节数 / Maximum response bytes.
        @param timeout_seconds 总超时秒数 / Total timeout seconds.
        @return 下载到的二进制 / Downloaded binary.
        """

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise UpstreamUnavailable("artifact URL must be HTTP(S)")
        if max_bytes <= 0 or timeout_seconds <= 0:
            raise ValueError("download bounds must be positive")
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        try:
            async with create_aiohttp_session() as session:
                async with session.get(
                    url, headers=HEADERS, timeout=timeout
                ) as response:
                    if response.status != 200:
                        raise UpstreamUnavailable(
                            f"artifact download returned HTTP {response.status}"
                        )
                    declared = response.content_length
                    if declared is not None and declared > max_bytes:
                        raise ArtifactTooLarge(f"artifact exceeds {max_bytes} bytes")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise ArtifactTooLarge(
                                f"artifact exceeds {max_bytes} bytes"
                            )
                        chunks.append(chunk)
                    if not chunks:
                        raise UpstreamUnavailable("artifact response was empty")
                    return b"".join(chunks)
        except ArtifactTooLarge, UpstreamUnavailable:
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            raise UpstreamUnavailable("artifact download failed") from error

    def __init__(self, *, telemetry: Telemetry) -> None:
        """@brief 注入进程遥测 / Inject process telemetry.

        @param telemetry 进程唯一 recorder / Sole process recorder.
        """

        self._telemetry = telemetry
