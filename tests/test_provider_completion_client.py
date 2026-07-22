"""@brief 原生 OpenAI/Anthropic completion client contract 测试 / Native OpenAI/Anthropic completion-client contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta

import aiohttp
import pytest
from aiohttp import web

from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.domain.assistant.messages import CanonicalMessage
from fogmoe_bot.domain.assistant.request_metadata import (
    MAX_REQUEST_META_ITEMS,
    normalize_request_meta,
)
from fogmoe_bot.domain.assistant.routing.models import (
    ProviderAuth,
    ProviderRoute,
    RouteModel,
)
from fogmoe_bot.infrastructure.llm.messages import MessageContractError
from fogmoe_bot.infrastructure.llm.openai_codec import decode_openai_response
from fogmoe_bot.infrastructure.llm.provider_completion import ProviderCompletionClient
from fogmoe_bot.application.assistant.errors import (
    ProviderContractError,
    ProviderFailure,
    ProviderFailureKind,
)


def _message(role: str, parts: list[dict[str, object]]) -> CanonicalMessage:
    """@brief 构造测试用 Canonical Message V2 JSON / Build Canonical Message V2 JSON for a test.

    @param role canonical role / Canonical role.
    @param parts canonical parts / Canonical parts.
    @return 可持久化 canonical JSON object / Persistable canonical JSON object.
    """

    return CanonicalMessage.from_json(
        {
            "schema_version": 2,
            "role": role,
            "parts": parts,
            "policy": {"include_in_context": True},
            "meta": {},
        }
    )


def _client() -> ProviderCompletionClient:
    """@brief 构造不受环境代理影响的测试 client / Build a test client unaffected by environment proxy settings.

    @return 使用本地 aiohttp session 的 client / Client using a local aiohttp session.
    """

    return ProviderCompletionClient(
        telemetry=Telemetry(TelemetryBuffer(64)),
        session_factory=aiohttp.ClientSession,
    )


def test_openrouter_style_request_maps_custom_metadata_and_tool_role() -> None:
    """@brief OpenAI-style route 显式发送 metadata，并将 canonical tool role 渲染为 OpenAI tool / An OpenAI-style route sends metadata explicitly and renders canonical tool role as an OpenAI tool message."""

    async def scenario() -> None:
        """@brief 执行本地 OpenAI-style HTTP 场景 / Run the local OpenAI-style HTTP scenario.

        @return None / None.
        """

        captured: list[Mapping[str, object]] = []

        async def completions(request: web.Request) -> web.Response:
            """@brief 捕获 wire payload 并返回文本完成 / Capture the wire payload and return a text completion.

            @param request 本地 HTTP 请求 / Local HTTP request.
            @return 固定 OpenAI-style JSON response / Fixed OpenAI-style JSON response.
            """

            assert request.headers["Authorization"] == "Bearer test-openrouter-key"
            payload = await request.json()
            assert isinstance(payload, Mapping)
            captured.append(payload)
            return web.json_response(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "done"}}
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                }
            )

        application = web.Application()
        application.router.add_post("/chat/completions", completions)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sockets = getattr(site._server, "sockets", None)
        assert sockets
        port = sockets[0].getsockname()[1]
        client = _client()
        route = ProviderRoute(
            route_id="chat-openrouter",
            provider_id="openrouter",
            provider_label="OpenRouter",
            style="openai",
            endpoint=f"http://127.0.0.1:{port}/chat/completions",
            auth=ProviderAuth(api_key="test-openrouter-key"),
            models=(RouteModel("openrouter/test-model"),),
            meta={"tenant": "klee"},
        )
        try:
            completion = await client.complete(
                route=route,
                model="openrouter/test-model",
                messages=(
                    _message("user", [{"type": "text", "text": "weather?"}]),
                    _message(
                        "assistant",
                        [
                            {
                                "type": "tool_call",
                                "call_id": "call_weather",
                                "name": "weather",
                                "arguments": {"city": "Singapore"},
                            }
                        ],
                    ),
                    _message(
                        "tool",
                        [
                            {
                                "type": "tool_result",
                                "call_id": "call_weather",
                                "name": "weather",
                                "result": {"temperature": 30},
                                "is_error": False,
                            }
                        ],
                    ),
                ),
                tools=(),
                tool_choice=None,
                max_tokens=128,
                timeout_seconds=None,
                request_meta=normalize_request_meta(
                    {"request_id": "req-1", "tenant": "caller-cannot-spoof"}
                ),
            )
        finally:
            await client.aclose()
            await runner.cleanup()
        assert completion.content == "done"
        assert completion.message.to_json()["schema_version"] == 2
        assert len(captured) == 1
        payload = captured[0]
        assert payload["metadata"] == {"tenant": "klee", "request_id": "req-1"}
        messages = payload["messages"]
        assert isinstance(messages, list)
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "tool",
        ]
        tool_message = messages[-1]
        assert tool_message == {
            "role": "tool",
            "tool_call_id": "call_weather",
            "content": '{"temperature":30}',
        }

    asyncio.run(scenario())


def test_route_and_caller_metadata_cannot_bypass_the_merged_size_limit() -> None:
    """@brief route 与调用方 metadata 合并后仍必须满足统一上限 / Route and caller metadata must still satisfy the shared limit after merging.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 在不建立 HTTP 连接前验证合并 metadata / Validate merged metadata before any HTTP connection is opened.

        @return None / None.
        """

        client = _client()
        route = ProviderRoute(
            route_id="chat-openrouter-meta-limit",
            provider_id="openrouter",
            provider_label="OpenRouter",
            style="openai",
            endpoint="http://127.0.0.1:1/chat/completions",
            auth=ProviderAuth(api_key=None),
            models=(RouteModel("openrouter/test-model"),),
            meta={
                f"operator_{index}": "value"
                for index in range(MAX_REQUEST_META_ITEMS)
            },
        )
        try:
            with pytest.raises(ProviderContractError, match="provider contract"):
                await client.complete(
                    route=route,
                    model="openrouter/test-model",
                    messages=(
                        _message("user", [{"type": "text", "text": "hello"}]),
                    ),
                    tools=(),
                    tool_choice=None,
                    max_tokens=128,
                    timeout_seconds=None,
                    request_meta=normalize_request_meta({"caller": "value"}),
                )
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_anthropic_user_id_metadata_and_tool_turn_aggregation() -> None:
    """@brief Anthropic 仅发送 user_id metadata，并把连续 tool messages 聚合为 user tool_result blocks / Anthropic sends only user_id metadata and aggregates consecutive tool messages into user tool_result blocks."""

    async def scenario() -> None:
        """@brief 执行本地 Anthropic HTTP 场景 / Run the local Anthropic HTTP scenario.

        @return None / None.
        """

        captured: list[Mapping[str, object]] = []

        async def messages_endpoint(request: web.Request) -> web.Response:
            """@brief 捕获 Anthropic payload 并返回文本 / Capture an Anthropic payload and return text.

            @param request 本地 HTTP 请求 / Local HTTP request.
            @return 固定 Anthropic JSON response / Fixed Anthropic JSON response.
            """

            assert request.headers["x-api-key"] == "test-anthropic-key"
            assert request.headers["anthropic-version"] == "2023-06-01"
            payload = await request.json()
            assert isinstance(payload, Mapping)
            captured.append(payload)
            return web.json_response(
                {
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 13, "output_tokens": 5},
                }
            )

        application = web.Application()
        application.router.add_post("/v1/messages", messages_endpoint)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sockets = getattr(site._server, "sockets", None)
        assert sockets
        port = sockets[0].getsockname()[1]
        client = _client()
        route = ProviderRoute(
            route_id="chat-claude",
            provider_id="claude",
            provider_label="Claude",
            style="anthropic",
            endpoint=f"http://127.0.0.1:{port}/v1/messages",
            auth=ProviderAuth(
                api_key="test-anthropic-key",
                header="x-api-key",
                prefix="",
            ),
            api_version="2023-06-01",
            models=(RouteModel("claude-test"),),
            meta={"user_id": "opaque-user"},
        )
        history = (
            _message("user", [{"type": "text", "text": "weather?"}]),
            _message(
                "assistant",
                [
                    {
                        "type": "tool_call",
                        "call_id": "call_1",
                        "name": "weather",
                        "arguments": {"city": "Singapore"},
                    },
                    {
                        "type": "tool_call",
                        "call_id": "call_2",
                        "name": "time",
                        "arguments": {},
                    },
                ],
            ),
            _message(
                "tool",
                [
                    {
                        "type": "tool_result",
                        "call_id": "call_1",
                        "name": "weather",
                        "result": "sunny",
                        "is_error": False,
                    }
                ],
            ),
            _message(
                "tool",
                [
                    {
                        "type": "tool_result",
                        "call_id": "call_2",
                        "name": "time",
                        "result": "10:00",
                        "is_error": False,
                    }
                ],
            ),
        )
        try:
            completion = await client.complete(
                route=route,
                model="claude-test",
                messages=history,
                tools=(),
                tool_choice=None,
                max_tokens=128,
                timeout_seconds=None,
                request_meta=normalize_request_meta({}),
            )
            with pytest.raises(ProviderContractError, match="only user_id"):
                await client.complete(
                    route=route,
                    model="claude-test",
                    messages=history,
                    tools=(),
                    tool_choice=None,
                    max_tokens=128,
                    timeout_seconds=None,
                    request_meta=normalize_request_meta({"trace": "not-supported"}),
                )
        finally:
            await client.aclose()
            await runner.cleanup()
        assert completion.content == "done"
        assert len(captured) == 1
        payload = captured[0]
        assert payload["metadata"] == {"user_id": "opaque-user"}
        messages = payload["messages"]
        assert isinstance(messages, list)
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "user",
        ]
        final_blocks = messages[-1]["content"]
        assert isinstance(final_blocks, list)
        assert [block["tool_use_id"] for block in final_blocks] == ["call_1", "call_2"]
        assert all(block["type"] == "tool_result" for block in final_blocks)

    asyncio.run(scenario())


def test_openai_decoder_rejects_invalid_function_arguments() -> None:
    """@brief OpenAI function.arguments 不是 JSON 时必须显式失败 / OpenAI function.arguments that is not JSON must fail explicitly."""

    with pytest.raises(MessageContractError, match="not valid JSON"):
        decode_openai_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_bad",
                                    "type": "function",
                                    "function": {
                                        "name": "weather",
                                        "arguments": "{not-json}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("status", "retry_after", "kind"),
    (
        (401, None, ProviderFailureKind.REJECTED),
        (403, None, ProviderFailureKind.REJECTED),
        (429, "17", ProviderFailureKind.RATE_LIMITED),
        (503, None, ProviderFailureKind.SERVER),
        (504, None, ProviderFailureKind.TIMEOUT),
    ),
)
def test_http_failure_is_typed_and_never_echoes_provider_body(
    status: int,
    retry_after: str | None,
    kind: ProviderFailureKind,
) -> None:
    """@brief HTTP 失败保留 typed 语义且不回显 provider body / HTTP failures retain typed semantics and never echo provider bodies.

    @param status 本地 provider 返回的 HTTP 状态 / HTTP status returned by the local provider.
    @param retry_after 可选 Retry-After header / Optional Retry-After header.
    @param kind 预期稳定 failure kind / Expected stable failure kind.
    """

    async def scenario() -> None:
        """@brief 调用返回失败的本地 OpenAI-style endpoint / Call a failing local OpenAI-style endpoint.

        @return None / None.
        """

        provider_body_secret = "synthetic-provider-body-secret"

        async def completions(_: web.Request) -> web.Response:
            """@brief 返回包含不应泄漏文本的失败 response / Return a failing response containing text that must not leak.

            @param _ 未使用的本地 request / Unused local request.
            @return 非 2xx response / Non-2xx response.
            """

            headers = {} if retry_after is None else {"Retry-After": retry_after}
            return web.Response(
                status=status,
                headers=headers,
                body=(f"token={provider_body_secret}").encode(),
            )

        application = web.Application()
        application.router.add_post("/chat/completions", completions)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sockets = getattr(site._server, "sockets", None)
        assert sockets
        port = sockets[0].getsockname()[1]
        client = _client()
        route = ProviderRoute(
            route_id="failure-route",
            provider_id="failure-provider",
            provider_label="Failure provider",
            style="openai",
            endpoint=f"http://127.0.0.1:{port}/chat/completions",
            auth=ProviderAuth(api_key="test-key"),
            models=(RouteModel("failure-model"),),
        )
        try:
            with pytest.raises(ProviderFailure) as captured:
                await client.complete(
                    route=route,
                    model="failure-model",
                    messages=(_message("user", [{"type": "text", "text": "hi"}]),),
                    tools=(),
                    tool_choice=None,
                    max_tokens=32,
                    timeout_seconds=None,
                    request_meta=normalize_request_meta({}),
                )
        finally:
            await client.aclose()
            await runner.cleanup()

        failure = captured.value
        assert failure.kind is kind
        assert failure.status == status
        assert provider_body_secret not in str(failure)
        assert str(failure) == f"LLM provider HTTP {status}"
        if status == 429:
            assert failure.retry_after == timedelta(seconds=17)
        else:
            assert failure.retry_after is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("transport_error", "kind"),
    (
        (TimeoutError(), ProviderFailureKind.TIMEOUT),
        (aiohttp.ClientConnectionError(), ProviderFailureKind.TRANSPORT),
    ),
)
def test_transport_and_timeout_failures_are_typed(
    transport_error: Exception,
    kind: ProviderFailureKind,
) -> None:
    """@brief 传输与超时异常映射为稳定 provider kind / Transport and timeout exceptions map to stable provider kinds.

    @param transport_error 注入到 aiohttp context manager 的异常 / Exception injected into the aiohttp context manager.
    @param kind 预期稳定 failure kind / Expected stable failure kind.
    """

    class _FailingRequest:
        """@brief 进入时抛出指定错误的最小 async context manager / Minimal async context manager raising a chosen error on entry."""

        async def __aenter__(self) -> object:
            """@brief 在 HTTP request 进入点抛出异常 / Raise at HTTP request entry.

            @return 永不返回 / Never returns.
            @raise Exception 注入的传输或超时错误 / Injected transport or timeout error.
            """

            raise transport_error

        async def __aexit__(
            self,
            _: object,
            __: object,
            ___: object,
        ) -> bool:
            """@brief 不吞掉 context manager 异常 / Do not suppress context-manager exceptions.

            @param _ 未使用异常类型 / Unused exception type.
            @param __ 未使用异常值 / Unused exception value.
            @param ___ 未使用 traceback / Unused traceback.
            @return False / False.
            """

            return False

    class _FailingSession:
        """@brief 不建立网络连接的最小 session fake / Minimal session fake that establishes no network connection."""

        closed = False
        """@brief session 是否关闭 / Whether the session is closed."""

        def post(self, _endpoint: str, **_: object) -> _FailingRequest:
            """@brief 返回失败 request context manager / Return the failing request context manager.

            @param _endpoint 未使用 endpoint / Unused endpoint.
            @param _ 未使用 HTTP 参数 / Unused HTTP parameters.
            @return 失败 request context manager / Failing request context manager.
            """

            return _FailingRequest()

        async def close(self) -> None:
            """@brief 模拟关闭 session / Simulate closing the session.

            @return None / None.
            """

            self.closed = True

    async def scenario() -> None:
        """@brief 运行不访问网络的 completion 场景 / Run a completion scenario without network access.

        @return None / None.
        """

        client = ProviderCompletionClient(
            telemetry=Telemetry(TelemetryBuffer(64)),
            session_factory=_FailingSession,
        )
        route = ProviderRoute(
            route_id="transport-route",
            provider_id="transport-provider",
            provider_label="Transport provider",
            style="openai",
            endpoint="https://provider.example.test/v1/chat/completions",
            auth=ProviderAuth(),
            models=(RouteModel("transport-model"),),
        )
        try:
            with pytest.raises(ProviderFailure) as captured:
                await client.complete(
                    route=route,
                    model="transport-model",
                    messages=(_message("user", [{"type": "text", "text": "hi"}]),),
                    tools=(),
                    tool_choice=None,
                    max_tokens=32,
                    timeout_seconds=None,
                    request_meta=normalize_request_meta({}),
                )
        finally:
            await client.aclose()

        assert captured.value.kind is kind
        assert captured.value.status is None

    asyncio.run(scenario())


def test_local_contract_failure_is_typed_and_generic() -> None:
    """@brief 本地 request contract 错误为不可重试 typed failure / A local request-contract error is a non-retryable typed failure."""

    async def scenario() -> None:
        """@brief 用非法 tool choice 触发本地 contract 校验 / Trigger local contract validation with an invalid tool choice.

        @return None / None.
        """

        client = _client()
        route = ProviderRoute(
            route_id="contract-route",
            provider_id="contract-provider",
            provider_label="Contract provider",
            style="openai",
            endpoint="https://provider.example.test/v1/chat/completions",
            auth=ProviderAuth(),
            models=(RouteModel("contract-model"),),
        )
        try:
            with pytest.raises(ProviderContractError) as captured:
                await client.complete(
                    route=route,
                    model="contract-model",
                    messages=(_message("user", [{"type": "text", "text": "hi"}]),),
                    tools=(),
                    tool_choice="auto",
                    max_tokens=32,
                    timeout_seconds=None,
                    request_meta=normalize_request_meta({}),
                )
        finally:
            await client.aclose()

        assert captured.value.kind is ProviderFailureKind.CONTRACT
        assert str(captured.value) == "LLM request or response violated the provider contract"

    asyncio.run(scenario())
