"""@brief 共享 aiohttp session 的原生 LLM completion client / Native LLM completion client with a shared aiohttp session.

Client 只识别两种显式 wire style：OpenAI Chat Completions 与原生 Anthropic Messages。
完整 route 由领域层传入，adapter 不读取配置、不拼 endpoint、不做自动重试，也不保留
旧 provider registry。/
The client recognizes only two explicit wire styles: OpenAI Chat Completions and native
Anthropic Messages. A complete route is supplied by the domain layer; the adapter reads no
configuration, appends no endpoint paths, performs no automatic retry, and keeps no legacy
provider registry.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import aiohttp

from fogmoe_bot.application.assistant.completion import AssistantCompletion
from fogmoe_bot.application.assistant.tools.catalog import ToolDefinition
from fogmoe_bot.application.observability.telemetry import SpanScope, Telemetry
from fogmoe_bot.domain.assistant.messages import CanonicalMessage, CanonicalMessageError
from fogmoe_bot.domain.assistant.request_metadata import (
    RequestMeta,
    RequestMetaError,
    normalize_request_meta,
)
from fogmoe_bot.domain.assistant.routing.models import ProviderRoute
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind
from fogmoe_bot.infrastructure.network.proxy import create_aiohttp_session

from .anthropic_codec import decode_anthropic_response, encode_anthropic_request
from .messages import MessageContractError, ProviderPayload
from .openai_codec import decode_openai_response, encode_openai_request
from fogmoe_bot.application.assistant.errors import (
    ProviderContractError,
    ProviderFailure,
    ProviderFailureKind,
)
from .provider_response import DecodedProviderCompletion

type SessionFactory = Callable[[], aiohttp.ClientSession]
"""@brief 无参数 aiohttp session 工厂 / Zero-argument aiohttp session factory."""

_DEFAULT_TIMEOUT_SECONDS = 90.0
"""@brief 单个 LLM 请求的默认总 deadline / Default total deadline for one LLM request."""

_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
"""@brief 单个 LLM response 的硬字节上限 / Hard byte limit for one LLM response."""


class ProviderCompletionClient:
    """@brief 无 SDK、可生命周期管理的原生 completion adapter / SDK-free native completion adapter with lifecycle management.

构造时不创建 socket；``run`` 或首次 ``complete`` 才会在当前 event loop 创建共享
``aiohttp.ClientSession``。/
Construction creates no socket; ``run`` or the first ``complete`` creates one shared
``aiohttp.ClientSession`` in the current event loop.
"""

    def __init__(
        self,
        *,
        telemetry: Telemetry,
        default_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        session_factory: SessionFactory | None = None,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        """@brief 注入 telemetry 与 session 工厂 / Inject telemetry and a session factory.

        @param telemetry 进程 typed telemetry / Process typed telemetry.
        @param default_timeout_seconds 默认请求 deadline / Default request deadline.
        @param session_factory 可测试的 session 工厂 / Testable session factory.
        @param max_response_bytes 单 response 字节上限 / Per-response byte limit.
        @return None / None.
        @raise ValueError timeout 或上限非法时抛出 / Raised for an invalid timeout or limit.
        """

        if (
            isinstance(default_timeout_seconds, bool)
            or not math.isfinite(default_timeout_seconds)
            or default_timeout_seconds <= 0.0
        ):
            raise ValueError("default_timeout_seconds must be a positive finite number")
        if isinstance(max_response_bytes, bool) or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive integer")
        self._telemetry = telemetry
        self._default_timeout_seconds = default_timeout_seconds
        self._session_factory = session_factory or _default_session_factory
        self._max_response_bytes = max_response_bytes
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 在顶层 runtime 中拥有共享 session 生命周期 / Own the shared-session lifecycle in the top-level runtime.

        @param stop_event 停止信号 / Stop signal.
        @return None / None.
        """

        await self._get_session()
        try:
            await stop_event.wait()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """@brief 幂等关闭共享 HTTP session / Idempotently close the shared HTTP session.

        @return None / None.
        """

        async with self._session_lock:
            session = self._session
            self._session = None
        if session is not None and not session.closed:
            await session.close()

    async def complete(
        self,
        *,
        route: ProviderRoute,
        model: str,
        messages: Sequence[CanonicalMessage],
        tools: Sequence[ToolDefinition],
        tool_choice: str | JsonObject | None,
        max_tokens: int,
        timeout_seconds: float | None,
        request_meta: RequestMeta,
    ) -> AssistantCompletion:
        """@brief 发送一次原生 provider completion 请求 / Send one native provider completion request.

        @param route 自包含的 provider route / Self-contained provider route.
        @param model route 选中的模型 / Model selected by the route.
        @param messages canonical V2 历史 / Canonical V2 history.
        @param tools 应用层 typed tools / Application-layer typed tools.
        @param tool_choice 工具选择 / Tool choice.
        @param max_tokens 输出 token 上限 / Output-token limit.
        @param timeout_seconds 单次请求总 deadline / Per-request total deadline.
        @param request_meta 调用方显式 metadata / Explicit caller metadata.
        @return provider-neutral completion / Provider-neutral completion.
        @raise ProviderFailure 传输、HTTP 或协议失败 / Transport, HTTP, or protocol failure.
        @note 本方法不自动重试；durable inference 层拥有 retry/fallback 决策 /
            This method never retries automatically; durable inference owns retry/fallback decisions.
        """

        if not isinstance(route, ProviderRoute):
            raise TypeError("route must be ProviderRoute")
        if tools and not route.supports_tools:
            raise ProviderContractError(
                f"Route {route.route_id!r} does not support tools"
            )
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0.0
        ):
            raise ProviderContractError("timeout_seconds must be a positive finite number")

        attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": route.provider_id,
            "gen_ai.request.model": model,
            "gen_ai.request.max_tokens": max_tokens,
        }
        with self._telemetry.span(
            "chat",
            kind=SpanKind.CLIENT,
            attributes=attributes,
        ) as span:
            try:
                metadata = _request_metadata(route, request_meta)
                payload = self._encode_request(
                    route=route,
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    metadata=metadata,
                )
                session = await self._get_session()
                deadline = timeout_seconds or self._default_timeout_seconds
                async with session.post(
                    route.endpoint,
                    json=payload,
                    headers=_request_headers(route),
                    timeout=aiohttp.ClientTimeout(total=deadline),
                ) as response:
                    raw = await _read_bounded(
                        response,
                        maximum_bytes=self._max_response_bytes,
                    )
                    span.set_attribute("http.response.status_code", response.status)
                    span.set_attribute("http.response.body.size", len(raw))
                    if not 200 <= response.status < 300:
                        raise _http_failure(response, raw)
                decoded = self._decode_response(route, _decode_json_object(raw))
                completion = AssistantCompletion(decoded.message)
            except asyncio.CancelledError:
                raise
            except ProviderFailure as error:
                self._record_failure(route, model, error)
                raise
            except (aiohttp.ClientError, TimeoutError) as error:
                failure = ProviderFailure(
                    kind=(
                        ProviderFailureKind.TIMEOUT
                        if isinstance(error, TimeoutError)
                        else ProviderFailureKind.TRANSPORT
                    ),
                    message=(
                        f"LLM provider {route.provider_id!r} transport failed: "
                        f"{type(error).__name__}"
                    ),
                )
                self._record_failure(route, model, failure)
                raise failure from error
            except (
                CanonicalMessageError,
                MessageContractError,
                RequestMetaError,
                ValueError,
            ) as error:
                # Provider response body and arbitrary model/user data must never become a
                # durable error string.  The typed kind/status below retain the only routing
                # signal the caller needs; the causal exception is kept for local debugging.
                failure = ProviderContractError(
                    "LLM request or response violated the provider contract"
                )
                self._record_failure(route, model, failure)
                raise failure from error
            self._record_usage(route, model, span, decoded)
            self._telemetry.counter(
                MetricName.LLM_OUTCOMES,
                attributes={
                    "outcome": Outcome.SUCCESS,
                    "gen_ai.provider.name": route.provider_id,
                    "gen_ai.request.model": model,
                },
            )
        return completion

    async def _get_session(self) -> aiohttp.ClientSession:
        """@brief 懒创建当前 event loop 的共享 session / Lazily create the shared session for the current event loop.

        @return 活跃 aiohttp session / Active aiohttp session.
        @raise ProviderContractError session factory 返回已关闭 session 时抛出 /
            Raised when the session factory returns a closed session.
        """

        session = self._session
        if session is not None and not session.closed:
            return session
        async with self._session_lock:
            session = self._session
            if session is not None and not session.closed:
                return session
            created = self._session_factory()
            if created.closed:
                raise ProviderContractError("LLM session factory returned a closed session")
            self._session = created
            return created

    @staticmethod
    def _encode_request(
        *,
        route: ProviderRoute,
        model: str,
        messages: Sequence[CanonicalMessage],
        tools: Sequence[ToolDefinition],
        tool_choice: str | JsonObject | None,
        max_tokens: int,
        metadata: Mapping[str, str],
    ) -> ProviderPayload:
        """@brief 按 route style 编码一个 request / Encode one request by route style.

        @param route 自包含 route / Self-contained route.
        @param model 模型名 / Model name.
        @param messages canonical history / Canonical history.
        @param tools typed tools / Typed tools.
        @param tool_choice 工具选择 / Tool choice.
        @param max_tokens 输出上限 / Output limit.
        @param metadata 合并后的 metadata / Merged metadata.
        @return provider wire payload / Provider wire payload.
        """

        projected_messages = tuple(message.to_json() for message in messages)
        if route.style == "openai":
            return encode_openai_request(
                model=model,
                messages=projected_messages,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                metadata=metadata,
                strict_tools=route.strict_tools,
                temperature=None,
                top_p=None,
                stop_sequences=(),
                seed=None,
                reasoning_effort=None,
                parallel_tool_calls=None,
            )
        return encode_anthropic_request(
            model=model,
            messages=projected_messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            metadata=metadata,
            strict_tools=route.strict_tools,
            temperature=None,
            top_p=None,
            stop_sequences=(),
        )

    @staticmethod
    def _decode_response(
        route: ProviderRoute,
        payload: Mapping[str, object],
    ) -> DecodedProviderCompletion:
        """@brief 按 route style 解码一个 response / Decode one response by route style.

        @param route 自包含 route / Self-contained route.
        @param payload 顶层 response JSON object / Top-level response JSON object.
        @return provider-neutral decoded completion / Provider-neutral decoded completion.
        """

        return (
            decode_openai_response(payload)
            if route.style == "openai"
            else decode_anthropic_response(payload)
        )

    def _record_usage(
        self,
        route: ProviderRoute,
        model: str,
        span: SpanScope,
        completion: DecodedProviderCompletion,
    ) -> None:
        """@brief 写入成功请求的 token telemetry / Write token telemetry for a successful request.

        @param route 自包含 route / Self-contained route.
        @param model 模型名 / Model name.
        @param span 当前 telemetry span / Active telemetry span.
        @param completion 已解码完成 / Decoded completion.
        @return None / None.
        """

        for token_type, value, attribute in (
            ("input", completion.input_tokens, "gen_ai.usage.input_tokens"),
            ("output", completion.output_tokens, "gen_ai.usage.output_tokens"),
        ):
            if value is None:
                continue
            span.set_attribute(attribute, value)
            self._telemetry.counter(
                "gen_ai.client.token.usage",
                float(value),
                unit="{token}",
                attributes={
                    "gen_ai.provider.name": route.provider_id,
                    "gen_ai.request.model": model,
                    "gen_ai.token.type": token_type,
                },
            )

    def _record_failure(
        self,
        route: ProviderRoute,
        model: str,
        error: ProviderFailure,
    ) -> None:
        """@brief 写入一次失败 outcome metric / Write one failed outcome metric.

        @param route 自包含 route / Self-contained route.
        @param model 模型名 / Model name.
        @param error 已分类 provider failure / Classified provider failure.
        @return None / None.
        """

        self._telemetry.counter(
            MetricName.LLM_OUTCOMES,
            attributes={
                "outcome": (
                    Outcome.TIMEOUT
                    if error.kind is ProviderFailureKind.TIMEOUT
                    else Outcome.FAILURE
                ),
                "gen_ai.provider.name": route.provider_id,
                "gen_ai.request.model": model,
                "fogmoe.llm.failure.kind": error.kind.value,
            },
        )


def _default_session_factory() -> aiohttp.ClientSession:
    """@brief 创建受统一出站代理约束的 session / Create a session constrained by unified outbound proxy settings.

    @return 新建 aiohttp session / Fresh aiohttp session.
    """

    return create_aiohttp_session(raise_for_status=False)


def _request_metadata(
    route: ProviderRoute,
    request_meta: RequestMeta,
) -> Mapping[str, str]:
    """@brief 合并 route 与请求 metadata / Merge route and request metadata.

    @param route 自包含 route / Self-contained route.
    @param request_meta 用户请求 metadata / Caller request metadata.
    @return route 优先的安全 metadata / Safe metadata with route precedence.
    @raise RequestMetaError 请求 metadata 越界时抛出 / Raised when request metadata violates its boundary.
    @note 配置 route 的同名 key 覆盖用户值，避免调用方伪造 operator 级归因 /
        Route keys override caller values to prevent caller spoofing of operator-level attribution.
    """

    normalized_request = normalize_request_meta(request_meta)
    metadata = {**dict(normalized_request), **dict(route.meta)}
    if route.style == "anthropic" and set(metadata) - {"user_id"}:
        raise ProviderContractError(
            "Anthropic request metadata may contain only user_id"
        )
    return normalize_request_meta(metadata)


def _request_headers(route: ProviderRoute) -> dict[str, str]:
    """@brief 构造一次请求的无泄漏 headers / Build non-leaking headers for one request.

    @param route 自包含 route / Self-contained route.
    @return headers；只在内存中含 API key / Headers; API key exists only in memory.
    """

    headers = dict(route.headers)
    _set_header(headers, "Content-Type", "application/json")
    if route.auth.api_key is not None:
        _set_header(
            headers,
            route.auth.header,
            f"{route.auth.prefix}{route.auth.api_key}",
        )
    if route.style == "anthropic":
        api_version = route.api_version
        if api_version is None:
            raise ProviderContractError("Anthropic route is missing api_version")
        _set_header(headers, "anthropic-version", api_version)
    return headers


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    """@brief 按 HTTP header 的大小写无关语义覆盖值 / Override a value using HTTP header case-insensitive semantics.

    @param headers 待修改 headers / Headers to mutate.
    @param name 规范 header 名 / Canonical header name.
    @param value 新值 / New value.
    @return None / None.
    """

    normalized = name.casefold()
    for existing in tuple(headers):
        if existing.casefold() == normalized:
            del headers[existing]
    headers[name] = value


async def _read_bounded(
    response: aiohttp.ClientResponse,
    *,
    maximum_bytes: int,
) -> bytes:
    """@brief 分块读取 response 且执行硬上限 / Read a response in chunks under a hard limit.

    @param response 活跃 HTTP response / Active HTTP response.
    @param maximum_bytes 最大响应字节数 / Maximum response bytes.
    @return 完整 response bytes / Complete response bytes.
    @raise MessageContractError response 超限时抛出 / Raised when response exceeds the limit.
    """

    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > maximum_bytes:
            raise MessageContractError("LLM provider response exceeded size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_json_object(raw: bytes) -> Mapping[str, object]:
    """@brief 解码顶层 JSON object / Decode a top-level JSON object.

    @param raw response bytes / Response bytes.
    @return JSON object mapping / JSON object mapping.
    @raise MessageContractError body 非法或非对象时抛出 / Raised for invalid or non-object body.
    """

    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MessageContractError("LLM provider response is not valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise MessageContractError("LLM provider response must be a JSON object")
    return decoded


def _http_failure(response: aiohttp.ClientResponse, raw: bytes) -> ProviderFailure:
    """@brief 将非 2xx HTTP response 映射为 typed failure / Map a non-2xx HTTP response into a typed failure.

    @param response 活跃 HTTP response / Active HTTP response.
    @param raw 有界 response body；仅为确保调用方已消耗 body，绝不写入异常文本 /
        Bounded response body; only proves the body was consumed and is never written to exception text.
    @return 已分类失败 / Classified failure.
    """

    status = response.status
    if status == 429:
        kind = ProviderFailureKind.RATE_LIMITED
    elif status in {408, 504}:
        kind = ProviderFailureKind.TIMEOUT
    elif status in {409, 425} or status >= 500:
        kind = ProviderFailureKind.SERVER
    else:
        kind = ProviderFailureKind.REJECTED
    return ProviderFailure(
        kind=kind,
        status=status,
        retry_after=_retry_after(response.headers.get("Retry-After")),
        message=f"LLM provider HTTP {status}",
    )


def _retry_after(value: str | None) -> timedelta | None:
    """@brief 解析 Retry-After 的秒数或 HTTP-date / Parse Retry-After delta-seconds or HTTP-date.

    @param value 原始 header 值 / Raw header value.
    @return 正等待时间或 None / Positive wait duration or None.
    """

    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is not None:
        return (
            timedelta(seconds=seconds)
            if math.isfinite(seconds) and seconds > 0
            else None
        )
    try:
        deadline = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    delay = deadline.astimezone(UTC) - datetime.now(UTC)
    return delay if delay > timedelta(0) else None


__all__ = ["ProviderCompletionClient"]
