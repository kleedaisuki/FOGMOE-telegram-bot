"""有界图片二进制下载 HTTP adapter / Bounded picture-binary HTTP adapter."""

from urllib.parse import urlparse

import aiohttp

from fogmoe_bot.application.media.errors import ArtifactTooLarge, UpstreamUnavailable
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
