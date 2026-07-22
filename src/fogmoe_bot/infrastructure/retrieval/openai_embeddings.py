"""@brief OpenAI-compatible embedding HTTP adapter / OpenAI-compatible embedding HTTP adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import cast

import aiohttp
from aiohttp_socks import ProxyConnector

from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.application.retrieval import (
    EmbeddingContractError,
    RetryableEmbeddingError,
)
from fogmoe_bot.domain.observability.signals import SpanKind
from fogmoe_bot.domain.retrieval import EmbeddingSpace, EmbeddingVector

logger = logging.getLogger(__name__)
"""@brief Embedding adapter logger / Embedding-adapter logger."""

_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
"""@brief 单响应硬上限 / Hard per-response size limit."""


class OpenAICompatibleEmbeddings:
    """@brief 具有共享连接池和严格响应验证的 embedding adapter / Embedding adapter with pooling and strict validation."""

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        timeout_seconds: float,
        telemetry: Telemetry,
        proxy_url: str | None = None,
    ) -> None:
        """@brief 保存连接配置但不执行 I/O / Store connection configuration without I/O.

        @param api_key Bearer token / Bearer token.
        @param api_base OpenAI-compatible API root / OpenAI-compatible API root.
        @param timeout_seconds 总请求超时 / Total request timeout.
        @param telemetry 进程 typed telemetry / Process typed telemetry.
        @param proxy_url 可选 HTTP/SOCKS proxy / Optional HTTP/SOCKS proxy.
        @raise ValueError 配置非法 / Invalid configuration.
        """

        key = api_key.strip()
        base = _normalize_api_base(api_base)
        proxy = proxy_url.strip() if proxy_url is not None else None
        if not key:
            raise ValueError("Embedding API key cannot be blank")
        if not base.startswith(("https://", "http://")):
            raise ValueError("Embedding API base must be HTTP(S)")
        if timeout_seconds <= 0.0:
            raise ValueError("Embedding timeout must be positive")
        self._api_key = key
        self._endpoint = f"{base}/embeddings"
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._proxy_url = proxy or None
        self._telemetry = telemetry
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 在顶层 runtime 中拥有 HTTP session 生命周期 / Own the HTTP-session lifecycle in the top-level runtime.

        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        await self._get_session()
        try:
            await stop_event.wait()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """@brief 幂等关闭共享 session / Idempotently close the shared session.

        @return None / None.
        """

        async with self._session_lock:
            session = self._session
            self._session = None
        if session is not None and not session.closed:
            await session.close()

    async def embed_documents(
        self,
        texts: Sequence[str],
        *,
        space: EmbeddingSpace,
    ) -> tuple[EmbeddingVector, ...]:
        """@brief 批量嵌入原始 passage 文本 / Embed raw passage text in a batch.

        @return 与输入同序向量 / Vectors in input order.
        """

        normalized = _validate_texts(texts)
        return await self._request(
            normalized, space=space, input_type="search_document"
        )

    async def embed_query(
        self,
        text: str,
        *,
        space: EmbeddingSpace,
    ) -> EmbeddingVector:
        """@brief 按 Qwen instruction contract 嵌入 Query / Embed a query with the Qwen instruction contract.

        @return Query 向量 / Query vector.
        """

        query = text.strip()
        if not query or len(query) > 20_000:
            raise ValueError("Embedding query must contain 1-20000 characters")
        instructed = f"Instruct: {space.query_instruction}\nQuery: {query}"
        vectors = await self._request(
            (instructed,),
            space=space,
            input_type="search_query",
        )
        return vectors[0]

    async def _request(
        self,
        texts: Sequence[str],
        *,
        space: EmbeddingSpace,
        input_type: str,
    ) -> tuple[EmbeddingVector, ...]:
        """@brief 调用并严格解析 embeddings endpoint / Call and strictly parse the embeddings endpoint.

        @return 输入顺序向量 / Vectors in input order.
        @raise RetryableEmbeddingError 网络、限流或 5xx / Network, rate-limit, or 5xx failure.
        @raise EmbeddingContractError 请求或响应契约错误 / Request or response contract error.
        """

        session = await self._get_session()
        payload = {
            "model": space.model,
            "input": list(texts),
            "dimensions": space.dimensions,
            "encoding_format": "float",
            "input_type": input_type,
        }
        with self._telemetry.span(
            "retrieval.embedding.request",
            kind=SpanKind.CLIENT,
            attributes={
                "retrieval.embedding.model": space.model,
                "retrieval.embedding.dimensions": space.dimensions,
                "retrieval.embedding.input_type": input_type,
                "retrieval.batch.size": len(texts),
            },
        ) as span:
            try:
                async with session.post(
                    self._endpoint,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    proxy=_http_proxy(self._proxy_url),
                ) as response:
                    raw = await _read_bounded(response)
                    span.set_attribute("http.response.status_code", response.status)
                    span.set_attribute("http.response.body.size", len(raw))
                    if response.status != 200:
                        self._raise_http_error(response, raw)
                    decoded = _decode_json(raw)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError) as error:
                raise RetryableEmbeddingError(
                    f"Embedding transport failed: {type(error).__name__}"
                ) from error
            vectors = _parse_vectors(decoded, expected_count=len(texts), space=space)
        logger.info(
            "Embedding request completed model=%s dimensions=%s batch_size=%s",
            space.model,
            space.dimensions,
            len(texts),
        )
        return vectors

    async def _get_session(self) -> aiohttp.ClientSession:
        """@brief 延迟创建进程共享 session / Lazily create the process-shared session.

        @return 活跃 session / Active session.
        """

        session = self._session
        if session is not None and not session.closed:
            return session
        async with self._session_lock:
            session = self._session
            if session is not None and not session.closed:
                return session
            connector = _connector(self._proxy_url)
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                connector=connector,
                trust_env=self._proxy_url is None,
                raise_for_status=False,
            )
            return self._session

    @staticmethod
    def _raise_http_error(response: aiohttp.ClientResponse, raw: bytes) -> None:
        """@brief 将 HTTP 状态分类为 retryable 或 contract error / Classify an HTTP status.

        @return 永不返回 / Never returns.
        """

        detail = _safe_error_detail(raw)
        message = f"Embedding HTTP {response.status}: {detail}"
        if response.status in {408, 409, 425, 429} or response.status >= 500:
            raise RetryableEmbeddingError(
                message,
                retry_after=_retry_after(response.headers.get("Retry-After")),
            )
        raise EmbeddingContractError(message)


def _normalize_api_base(value: str) -> str:
    """@brief 规范 OpenAI-compatible API root / Normalize an OpenAI-compatible API root.

    @return 不含 endpoint 后缀的 root / Root without an endpoint suffix.
    """

    base = value.strip().rstrip("/")
    for suffix in ("/embeddings", "/chat/completions"):
        if base.lower().endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


def _validate_texts(texts: Sequence[str]) -> tuple[str, ...]:
    """@brief 规范并限制 passage batch / Normalize and bound a passage batch.

    @return 非空文本 tuple / Non-empty text tuple.
    """

    normalized = tuple(text.strip() for text in texts)
    if not normalized or len(normalized) > 128:
        raise ValueError("Embedding document batch must contain 1-128 texts")
    if any(not text or len(text) > 20_000 for text in normalized):
        raise ValueError("Embedding documents must contain 1-20000 characters")
    return normalized


def _connector(proxy_url: str | None) -> aiohttp.BaseConnector | None:
    """@brief 为 SOCKS proxy 构建 connector / Build a connector for a SOCKS proxy.

    @return SOCKS connector 或默认 connector / SOCKS connector or default connector.
    """

    if proxy_url is not None and proxy_url.lower().startswith(
        ("socks4://", "socks5://")
    ):
        return ProxyConnector.from_url(proxy_url)
    return aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)


def _http_proxy(proxy_url: str | None) -> str | None:
    """@brief 返回 aiohttp 可直接使用的 HTTP proxy / Return an HTTP proxy usable by aiohttp.

    @return HTTP(S) proxy 或 None / HTTP(S) proxy or None.
    """

    return (
        proxy_url
        if proxy_url is not None
        and proxy_url.lower().startswith(("http://", "https://"))
        else None
    )


def _decode_json(raw: bytes) -> Mapping[str, object]:
    """@brief 解码顶层 JSON object / Decode a top-level JSON object.

    @return JSON mapping / JSON mapping.
    """

    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EmbeddingContractError("Embedding response is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise EmbeddingContractError("Embedding response must be a JSON object")
    return cast(Mapping[str, object], value)


async def _read_bounded(response: aiohttp.ClientResponse) -> bytes:
    """@brief 读取完整分块响应并执行硬上限 / Read a complete chunked response under a hard limit.

    @param response 活跃 HTTP response / Active HTTP response.
    @return 完整响应 bytes / Complete response bytes.
    @raise EmbeddingContractError 响应超限 / Response exceeds the limit.
    @note ``StreamReader.read(n)`` 可以在收到首个网络 chunk 后提前返回，不能表示
        “读取至多 n 字节直到 EOF”。/ ``StreamReader.read(n)`` may return after the first
        network chunk and does not mean "read until EOF with an n-byte limit."
    """

    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > _MAX_RESPONSE_BYTES:
            raise EmbeddingContractError("Embedding response exceeded size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_vectors(
    payload: Mapping[str, object],
    *,
    expected_count: int,
    space: EmbeddingSpace,
) -> tuple[EmbeddingVector, ...]:
    """@brief 按 index 恢复并验证 vector 顺序 / Restore and validate vector order by index.

    @return 输入顺序向量 / Vectors in input order.
    """

    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise EmbeddingContractError("Embedding response data length is invalid")
    ordered: list[EmbeddingVector | None] = [None] * expected_count
    for item in data:
        if not isinstance(item, Mapping):
            raise EmbeddingContractError("Embedding response item must be an object")
        index = item.get("index")
        raw_vector = item.get("embedding")
        if isinstance(index, bool) or not isinstance(index, int):
            raise EmbeddingContractError("Embedding response index must be an integer")
        if not 0 <= index < expected_count or ordered[index] is not None:
            raise EmbeddingContractError(
                "Embedding response index is duplicate or out of range"
            )
        if not isinstance(raw_vector, list) or any(
            isinstance(value, bool) or not isinstance(value, int | float)
            for value in raw_vector
        ):
            raise EmbeddingContractError("Embedding response vector must be numeric")
        vector = EmbeddingVector(tuple(float(value) for value in raw_vector))
        vector.require_space(space)
        ordered[index] = vector
    if any(vector is None for vector in ordered):
        raise EmbeddingContractError("Embedding response omitted an input index")
    return tuple(cast(EmbeddingVector, vector) for vector in ordered)


def _retry_after(value: str | None) -> timedelta | None:
    """@brief 解析 delta-seconds Retry-After / Parse a delta-seconds Retry-After value.

    @return 正等待或 None / Positive delay or None.
    """

    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return timedelta(seconds=seconds) if seconds > 0.0 else None


def _safe_error_detail(raw: bytes) -> str:
    """@brief 提取不含凭据的有界错误文本 / Extract bounded error text without credentials.

    @return 最多 300 字符 / At most 300 characters.
    """

    try:
        decoded = json.loads(raw)
    except UnicodeDecodeError, json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")[:300]
    if isinstance(decoded, Mapping):
        error = decoded.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return str(error["message"])[:300]
        if isinstance(error, str):
            return error[:300]
    return "provider rejected the request"


__all__ = ["OpenAICompatibleEmbeddings"]
