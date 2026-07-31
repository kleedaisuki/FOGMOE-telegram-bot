"""@brief Provider SSE 与稳定前缀缓存 wire contract 测试 / Provider SSE and stable-prefix cache wire-contract tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence

import aiohttp
import pytest
from aiohttp import web

from fogmoe_bot.application.assistant.completion import (
    CompletionFinished,
    CompletionTextDelta,
    PromptCacheDirective,
    PromptCacheKey,
)
from fogmoe_bot.application.assistant.errors import (
    ProviderFailure,
    ProviderFailureKind,
)
from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.domain.assistant.messages import CanonicalMessage
from fogmoe_bot.domain.assistant.request_metadata import normalize_request_meta
from fogmoe_bot.domain.assistant.routing.models import (
    ProviderAuth,
    ProviderRoute,
    RouteModel,
)
from fogmoe_bot.domain.observability.signals import SpanSignal
from fogmoe_bot.infrastructure.llm.anthropic_codec import (
    decode_anthropic_response,
    encode_anthropic_request,
)
from fogmoe_bot.infrastructure.llm.messages import MessageContractError
from fogmoe_bot.infrastructure.llm.openai_codec import (
    decode_openai_response,
    encode_openai_request,
)
from fogmoe_bot.infrastructure.llm.provider_completion import ProviderCompletionClient
from fogmoe_bot.infrastructure.llm.provider_response import OpenAIChatStreamAccumulator


def _message(role: str, text: str) -> CanonicalMessage:
    """@brief 构造纯文本 canonical message / Build a text-only canonical message.

    @param role canonical role / Canonical role.
    @param text message text / Message text.
    @return Canonical Message V2 / Canonical Message V2.
    """

    return CanonicalMessage.from_json(
        {
            "schema_version": 2,
            "role": role,
            "parts": [{"type": "text", "text": text}],
            "policy": {"include_in_context": True},
            "meta": {},
        }
    )


def _cache_key(policy_revision: str = "assistant-policy-v1") -> PromptCacheKey:
    """@brief 从静态测试命名空间派生 opaque cache key / Derive an opaque cache key from static test namespaces.

    @param policy_revision 静态 policy revision / Static policy revision.
    @return 固定 64 字符 key / Fixed 64-character key.
    """

    return PromptCacheKey.for_route_model(
        deployment_namespace="provider-wire-tests",
        route_id="assistant-primary",
        model="test-model",
        policy_revision=policy_revision,
    )


def _openai_wire(
    messages: Sequence[CanonicalMessage],
    directive: PromptCacheDirective | None,
) -> Mapping[str, object]:
    """@brief 编码 OpenAI 测试 payload / Encode an OpenAI test payload.

    @param messages canonical messages / Canonical messages.
    @param directive 可选缓存指令 / Optional cache directive.
    @return OpenAI wire payload / OpenAI wire payload.
    """

    return encode_openai_request(
        model="gpt-test",
        messages=tuple(message.to_json() for message in messages),
        tools=(),
        tool_choice=None,
        max_tokens=128,
        metadata={},
        strict_tools=False,
        temperature=None,
        top_p=None,
        stop_sequences=(),
        seed=None,
        reasoning_effort=None,
        parallel_tool_calls=None,
        prompt_cache=directive,
    )


def _anthropic_wire(
    messages: Sequence[CanonicalMessage],
    directive: PromptCacheDirective | None,
) -> Mapping[str, object]:
    """@brief 编码 Anthropic 测试 payload / Encode an Anthropic test payload.

    @param messages canonical messages / Canonical messages.
    @param directive 可选缓存指令 / Optional cache directive.
    @return Anthropic wire payload / Anthropic wire payload.
    """

    return encode_anthropic_request(
        model="claude-test",
        messages=tuple(message.to_json() for message in messages),
        tools=(),
        tool_choice=None,
        max_tokens=128,
        metadata={},
        strict_tools=False,
        temperature=None,
        top_p=None,
        stop_sequences=(),
        prompt_cache=directive,
    )


def _route(
    *,
    style: str,
    endpoint: str,
    explicit_cache: bool = False,
) -> ProviderRoute:
    """@brief 构造本地 provider route / Build a local provider route.

    @param style provider wire style / Provider wire style.
    @param endpoint local HTTP endpoint / Local HTTP endpoint.
    @param explicit_cache 是否由 operator 显式声明 cache capability /
        Whether the operator explicitly declared cache capability.
    @return 完整测试 route / Complete test route.
    """

    if style == "openai":
        return ProviderRoute(
            route_id="stream-openai",
            provider_id="openai-test",
            provider_label="OpenAI test",
            style="openai",
            endpoint=endpoint,
            auth=ProviderAuth(),
            models=(
                RouteModel(
                    "gpt-test",
                    prompt_cache_policy=("explicit" if explicit_cache else "automatic"),
                    prompt_cache_retention="30m" if explicit_cache else None,
                ),
            ),
        )
    return ProviderRoute(
        route_id="stream-anthropic",
        provider_id="anthropic-test",
        provider_label="Anthropic test",
        style="anthropic",
        endpoint=endpoint,
        auth=ProviderAuth(),
        api_version="2023-06-01",
        models=(
            RouteModel(
                "claude-test",
                prompt_cache_policy=("explicit" if explicit_cache else "automatic"),
                prompt_cache_retention="1h" if explicit_cache else None,
            ),
        ),
    )


async def _start_server(
    handler: object,
    *,
    path: str,
) -> tuple[web.AppRunner, str]:
    """@brief 启动一个 ephemeral aiohttp provider / Start an ephemeral aiohttp provider.

    @param handler aiohttp request handler / aiohttp request handler.
    @param path endpoint path / Endpoint path.
    @return runner 与完整 endpoint / Runner and complete endpoint.
    """

    application = web.Application()
    application.router.add_post(path, handler)  # type: ignore[arg-type]
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", None)
    assert sockets
    port = sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}{path}"


def test_openai_explicit_cache_keeps_dynamic_suffix_outside_breakpoint() -> None:
    """@brief WorkingMemory/steer 后缀变化不改变 OpenAI 稳定 wire 前缀 / WorkingMemory/steer suffix changes do not alter the stable OpenAI wire prefix."""

    stable = (
        _message("system", "stable policy"),
        _message("assistant", "stable history"),
    )
    first = (*stable, _message("user", "working-memory A\nsteer A"))
    second = (*stable, _message("user", "working-memory B\nsteer B"))
    directive = PromptCacheDirective(
        stable_prefix_message_count=2,
        cache_key=_cache_key(),
        mode="explicit",
        ttl="30m",
    )

    first_wire = _openai_wire(first, directive)
    second_wire = _openai_wire(second, directive)
    assert first_wire["prompt_cache_key"] == _cache_key().wire_value
    assert len(_cache_key().wire_value) == 64
    assert "working-memory" not in _cache_key().wire_value
    assert first_wire["prompt_cache_options"] == {
        "mode": "explicit",
        "ttl": "30m",
    }
    first_messages = first_wire["messages"]
    second_messages = second_wire["messages"]
    assert isinstance(first_messages, list)
    assert isinstance(second_messages, list)
    assert first_messages[:2] == second_messages[:2]
    assert (
        json.dumps(
            first_messages[:2],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        == json.dumps(
            second_messages[:2],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    assert first_messages[2:] != second_messages[2:]
    target_content = first_messages[1]["content"]
    assert target_content[-1]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "prompt_cache_breakpoint" not in json.dumps(first_messages[2:])


def test_prompt_cache_key_is_bounded_opaque_and_namespace_isolated() -> None:
    """@brief Cache key 固定长度、不泄漏输入命名空间并按静态 policy 隔离 / Cache keys are fixed-length, hide source namespaces, and isolate static policy revisions."""

    first = _cache_key("policy-revision-A")
    repeated = _cache_key("policy-revision-A")
    isolated = _cache_key("policy-revision-B")
    assert first == repeated
    assert first != isolated
    assert len(first.wire_value) == 64
    assert set(first.wire_value) <= set("0123456789abcdef")
    for raw_namespace in (
        "provider-wire-tests",
        "assistant-primary",
        "test-model",
        "policy-revision-A",
    ):
        assert raw_namespace not in first.wire_value


def test_cache_modes_and_provider_ttls_fail_closed() -> None:
    """@brief 自动模式不写私有字段，错误 provider TTL 显式失败 / Automatic mode emits no private fields and mismatched provider TTLs fail explicitly."""

    messages = (_message("system", "stable"), _message("user", "dynamic"))
    automatic = PromptCacheDirective(
        stable_prefix_message_count=1,
        cache_key=_cache_key("automatic"),
    )
    openai = _openai_wire(messages, automatic)
    anthropic = _anthropic_wire(messages, automatic)
    assert "prompt_cache_key" not in openai
    assert "prompt_cache_options" not in openai
    assert "prompt_cache_breakpoint" not in json.dumps(openai)
    assert "cache_control" not in json.dumps(anthropic)

    with pytest.raises(MessageContractError, match="30m TTL"):
        _openai_wire(
            messages,
            PromptCacheDirective(1, _cache_key("bad-openai"), "explicit", "1h"),
        )
    with pytest.raises(MessageContractError, match="5m or 1h TTL"):
        _anthropic_wire(
            messages,
            PromptCacheDirective(
                1,
                _cache_key("bad-anthropic"),
                "explicit",
                "30m",
            ),
        )


def test_client_rejects_explicit_fields_without_route_model_capability() -> None:
    """@brief 默认 automatic/disabled model 即使被误传 directive 也不能发送显式字段 / Default automatic or disabled models cannot send explicit fields even when a directive is passed accidentally."""

    async def scenario() -> None:
        """@brief 在建立 HTTP session 前验证 capability gate / Validate the capability gate before opening an HTTP session.

        @return None / None.
        """

        client = ProviderCompletionClient(
            telemetry=Telemetry(TelemetryBuffer(32)),
            session_factory=aiohttp.ClientSession,
        )
        explicit = PromptCacheDirective(
            1,
            _cache_key("capability-gate"),
            "explicit",
            "30m",
        )
        automatic_route = _route(
            style="openai",
            endpoint="http://127.0.0.1:1/chat/completions",
        )
        disabled_route = ProviderRoute(
            route_id="disabled-cache",
            provider_id="compatible-gateway",
            provider_label="Compatible gateway",
            style="openai",
            endpoint="http://127.0.0.1:1/chat/completions",
            auth=ProviderAuth(),
            models=(
                RouteModel(
                    "gpt-test",
                    prompt_cache_policy="disabled",
                ),
            ),
        )
        try:
            for route in (automatic_route, disabled_route):
                with pytest.raises(
                    ProviderFailure,
                    match="provider contract",
                ) as captured:
                    await client.complete(
                        route=route,
                        model="gpt-test",
                        messages=(_message("system", "stable"),),
                        tools=(),
                        tool_choice=None,
                        max_tokens=32,
                        timeout_seconds=None,
                        request_meta=normalize_request_meta({}),
                        prompt_cache=explicit,
                    )
                assert captured.value.kind is ProviderFailureKind.CONTRACT
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_anthropic_explicit_cache_preserves_tools_system_messages_prefix_order() -> (
    None
):
    """@brief Anthropic 断点留在 tools→system→messages 的稳定部分 / The Anthropic breakpoint stays in the stable tools→system→messages portion."""

    stable = (
        _message("system", "stable policy"),
        _message("user", "stable example"),
        _message("assistant", "stable answer"),
    )
    directive = PromptCacheDirective(
        3,
        _cache_key("anthropic"),
        "explicit",
        "1h",
    )
    first = _anthropic_wire(
        (*stable, _message("user", "working-memory A\nsteer A")),
        directive,
    )
    second = _anthropic_wire(
        (*stable, _message("user", "working-memory B\nsteer B")),
        directive,
    )
    assert first["system"] == second["system"]
    first_messages = first["messages"]
    second_messages = second["messages"]
    assert isinstance(first_messages, list)
    assert isinstance(second_messages, list)
    assert first_messages[:2] == second_messages[:2]
    assert (
        json.dumps(
            {
                "system": first["system"],
                "messages": first_messages[:2],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        == json.dumps(
            {
                "system": second["system"],
                "messages": second_messages[:2],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )
    assert first_messages[2:] != second_messages[2:]
    target = first_messages[1]["content"][-1]
    assert target["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in json.dumps(first_messages[2:])


def test_cache_usage_decoders_expose_reads_and_writes() -> None:
    """@brief 两种 provider 的缓存读写 token 都进入统一结果 / Cache-read and cache-write tokens from both providers enter the unified result."""

    openai = decode_openai_response(
        {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {
                    "cached_tokens": 80,
                    "cache_write_tokens": 20,
                },
            },
        }
    )
    anthropic = decode_anthropic_response(
        {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 5,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 30,
            },
        }
    )
    assert (openai.cached_input_tokens, openai.cache_write_input_tokens) == (80, 20)
    assert (anthropic.cached_input_tokens, anthropic.cache_write_input_tokens) == (
        70,
        30,
    )


@pytest.mark.parametrize(
    "usage_choices",
    [
        [],
        [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": "tool_calls",
            }
        ],
    ],
    ids=["openai-usage-only", "openrouter-terminal-echo"],
)
def test_openai_chat_sse_streams_text_and_finishes_with_tools_and_usage(
    usage_choices: list[object],
) -> None:
    """@brief OpenAI Chat SSE 增量、工具 JSON 与 usage 被完整聚合 / OpenAI Chat SSE deltas, tool JSON, and usage are fully aggregated."""

    async def scenario() -> None:
        """@brief 运行 OpenAI SSE 场景 / Run the OpenAI SSE scenario.

        @return None / None.
        """

        captured: list[Mapping[str, object]] = []

        async def endpoint(request: web.Request) -> web.StreamResponse:
            """@brief 返回分片 OpenAI Chat SSE / Return chunked OpenAI Chat SSE.

            @param request local request / Local request.
            @return streamed response / Streamed response.
            """

            captured.append(await request.json())
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            payloads = (
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "Hel"},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": "lo ",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "weather",
                                            "arguments": '{"city":',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": "world",
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": '"SG"}'}}
                                ],
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                {
                    "choices": usage_choices,
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 7,
                        "prompt_tokens_details": {
                            "cached_tokens": 80,
                            "cache_write_tokens": 20,
                        },
                    },
                },
            )
            for payload in payloads:
                await response.write(f"data: {json.dumps(payload)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        runner, endpoint_url = await _start_server(endpoint, path="/chat/completions")
        buffer = TelemetryBuffer(64)
        client = ProviderCompletionClient(
            telemetry=Telemetry(buffer),
            session_factory=aiohttp.ClientSession,
        )
        try:
            events = [
                event
                async for event in client.stream(
                    route=_route(
                        style="openai",
                        endpoint=endpoint_url,
                        explicit_cache=True,
                    ),
                    model="gpt-test",
                    messages=(
                        _message("system", "stable"),
                        _message("user", "question"),
                    ),
                    tools=(),
                    tool_choice=None,
                    max_tokens=128,
                    timeout_seconds=None,
                    request_meta=normalize_request_meta({}),
                    prompt_cache=PromptCacheDirective(
                        1,
                        _cache_key("openai-stream"),
                        "explicit",
                        "30m",
                    ),
                )
            ]
        finally:
            await client.aclose()
            await runner.cleanup()
        deltas = [
            event.text
            for event in events[:-1]
            if isinstance(event, CompletionTextDelta)
        ]
        assert deltas == ["Hel", "lo ", "world"]
        final = events[-1]
        assert isinstance(final, CompletionFinished)
        completion = final.completion
        assert completion.content == "Hello world"
        assert completion.tool_calls[0].arguments == {"city": "SG"}
        non_streamed = decode_openai_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Hello world",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "weather",
                                        "arguments": '{"city":"SG"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 7,
                    "prompt_tokens_details": {
                        "cached_tokens": 80,
                        "cache_write_tokens": 20,
                    },
                },
            }
        )
        assert completion.message == non_streamed.message
        assert captured[0]["stream"] is True
        assert captured[0]["stream_options"] == {"include_usage": True}
        span = next(
            signal for signal in buffer.drain(64) if isinstance(signal, SpanSignal)
        )
        assert span.attributes["gen_ai.usage.cached_input_tokens"] == 80
        assert span.attributes["gen_ai.usage.cache_write_input_tokens"] == 20

    asyncio.run(scenario())


def test_openrouter_usage_terminal_echo_cannot_smuggle_post_finish_output() -> None:
    """@brief usage 终块只能幂等重复终止 choice，不能夹带新输出 /
    A usage chunk may echo the terminal choice idempotently but cannot carry new output.
    """

    accumulator = OpenAIChatStreamAccumulator()
    accumulator.consume(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ]
        }
    )

    with pytest.raises(
        MessageContractError,
        match="carried content after finish_reason",
    ):
        accumulator.consume(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "late"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )


def test_anthropic_messages_sse_streams_text_and_tool_input() -> None:
    """@brief Anthropic Messages SSE 按具名事件聚合文本与工具输入 / Anthropic Messages SSE aggregates text and tool input by named events."""

    async def scenario() -> None:
        """@brief 运行 Anthropic SSE 场景 / Run the Anthropic SSE scenario.

        @return None / None.
        """

        captured: list[Mapping[str, object]] = []

        async def endpoint(request: web.Request) -> web.StreamResponse:
            """@brief 返回 Anthropic Messages SSE / Return Anthropic Messages SSE.

            @param request local request / Local request.
            @return streamed response / Streamed response.
            """

            captured.append(await request.json())
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            events = (
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "role": "assistant",
                            "content": [],
                            "usage": {
                                "input_tokens": 90,
                                "output_tokens": 1,
                                "cache_read_input_tokens": 60,
                                "cache_creation_input_tokens": 30,
                            },
                        },
                    },
                ),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Hi"},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "weather",
                            "input": {},
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": '{"city":"SG"}',
                        },
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use"},
                        "usage": {"output_tokens": 8},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            )
            for event_name, payload in events:
                await response.write(
                    f"event: {event_name}\ndata: {json.dumps(payload)}\n\n".encode()
                )
            await response.write_eof()
            return response

        runner, endpoint_url = await _start_server(endpoint, path="/v1/messages")
        buffer = TelemetryBuffer(64)
        client = ProviderCompletionClient(
            telemetry=Telemetry(buffer),
            session_factory=aiohttp.ClientSession,
        )
        try:
            events = [
                event
                async for event in client.stream(
                    route=_route(
                        style="anthropic",
                        endpoint=endpoint_url,
                        explicit_cache=True,
                    ),
                    model="claude-test",
                    messages=(
                        _message("system", "stable"),
                        _message("user", "question"),
                    ),
                    tools=(),
                    tool_choice=None,
                    max_tokens=128,
                    timeout_seconds=None,
                    request_meta=normalize_request_meta({}),
                    prompt_cache=PromptCacheDirective(
                        1,
                        _cache_key("anthropic-stream"),
                        "explicit",
                        "1h",
                    ),
                )
            ]
        finally:
            await client.aclose()
            await runner.cleanup()
        assert isinstance(events[0], CompletionTextDelta)
        assert events[0].text == "Hi"
        final = events[1]
        assert isinstance(final, CompletionFinished)
        assert final.completion.content == "Hi"
        assert final.completion.tool_calls[0].arguments == {"city": "SG"}
        non_streamed = decode_anthropic_response(
            {
                "content": [
                    {"type": "text", "text": "Hi"},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "weather",
                        "input": {"city": "SG"},
                    },
                ],
                "usage": {
                    "input_tokens": 90,
                    "output_tokens": 8,
                    "cache_read_input_tokens": 60,
                    "cache_creation_input_tokens": 30,
                },
            }
        )
        assert final.completion.message == non_streamed.message
        assert captured[0]["stream"] is True
        system = captured[0]["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {
            "type": "ephemeral",
            "ttl": "1h",
        }
        span = next(
            signal for signal in buffer.drain(64) if isinstance(signal, SpanSignal)
        )
        assert span.attributes["gen_ai.usage.cached_input_tokens"] == 60
        assert span.attributes["gen_ai.usage.cache_write_input_tokens"] == 30

    asyncio.run(scenario())


def test_stream_size_limit_and_in_stream_errors_are_safe() -> None:
    """@brief SSE 总大小受硬限制，in-stream error 不泄漏正文 / SSE total size has a hard limit and in-stream errors do not leak their body."""

    async def scenario() -> None:
        """@brief 运行两个失败 SSE 场景 / Run two failing SSE scenarios.

        @return None / None.
        """

        secret = "provider-secret-must-not-leak"

        async def oversized(request: web.Request) -> web.StreamResponse:
            """@brief 返回超限 SSE / Return oversized SSE.

            @param request local request / Local request.
            @return streamed response / Streamed response.
            """

            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: " + b"x" * 256 + b"\n\n")
            return response

        async def error_event(request: web.Request) -> web.StreamResponse:
            """@brief 返回含敏感正文的 OpenAI error event / Return an OpenAI error event containing sensitive text.

            @param request local request / Local request.
            @return streamed response / Streamed response.
            """

            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(
                f'data: {{"error":{{"message":"{secret}"}}}}\n\n'.encode()
            )
            return response

        for handler, maximum_bytes, expected_kind in (
            (oversized, 64, ProviderFailureKind.CONTRACT),
            (error_event, 1024, ProviderFailureKind.SERVER),
        ):
            runner, endpoint_url = await _start_server(
                handler,
                path="/chat/completions",
            )
            client = ProviderCompletionClient(
                telemetry=Telemetry(TelemetryBuffer(64)),
                session_factory=aiohttp.ClientSession,
                max_response_bytes=maximum_bytes,
            )
            try:
                with pytest.raises(ProviderFailure) as captured:
                    _ = [
                        event
                        async for event in client.stream(
                            route=_route(style="openai", endpoint=endpoint_url),
                            model="gpt-test",
                            messages=(_message("user", "question"),),
                            tools=(),
                            tool_choice=None,
                            max_tokens=32,
                            timeout_seconds=None,
                            request_meta=normalize_request_meta({}),
                        )
                    ]
            finally:
                await client.aclose()
                await runner.cleanup()
            assert captured.value.kind is expected_kind
            assert secret not in str(captured.value)

    asyncio.run(scenario())


def test_visible_delta_followed_by_error_has_no_terminal_completion() -> None:
    """@brief 已展示 delta 后的 provider 错误保留中断边界且不伪造终态 / A provider error after a visible delta preserves the interruption boundary and emits no fake terminal completion."""

    async def scenario() -> None:
        """@brief 运行 delta-then-error 场景 / Run the delta-then-error scenario.

        @return None / None.
        """

        async def endpoint(request: web.Request) -> web.StreamResponse:
            """@brief 先发送 old 文本再发送安全错误 / Send old text before a safe error.

            @param request local request / Local request.
            @return streamed response / Streamed response.
            """

            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(
                b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"old"},"finish_reason":null}]}\n\n'
            )
            await response.write(
                b'data: {"error":{"message":"must-not-append-fallback"}}\n\n'
            )
            return response

        runner, endpoint_url = await _start_server(endpoint, path="/chat/completions")
        client = ProviderCompletionClient(
            telemetry=Telemetry(TelemetryBuffer(32)),
            session_factory=aiohttp.ClientSession,
        )
        seen: list[object] = []
        try:
            with pytest.raises(ProviderFailure) as captured:
                async for event in client.stream(
                    route=_route(style="openai", endpoint=endpoint_url),
                    model="gpt-test",
                    messages=(_message("user", "question"),),
                    tools=(),
                    tool_choice=None,
                    max_tokens=32,
                    timeout_seconds=None,
                    request_meta=normalize_request_meta({}),
                ):
                    seen.append(event)
        finally:
            await client.aclose()
            await runner.cleanup()
        assert captured.value.kind is ProviderFailureKind.SERVER
        assert [
            event.text for event in seen if isinstance(event, CompletionTextDelta)
        ] == ["old"]
        assert not any(isinstance(event, CompletionFinished) for event in seen)

    asyncio.run(scenario())


def test_stream_cancellation_propagates_without_terminal_event() -> None:
    """@brief 等待下一 SSE event 时取消会原样传播且无终态 / Cancellation while awaiting the next SSE event propagates without a terminal event."""

    async def scenario() -> None:
        """@brief 运行可取消 SSE 场景 / Run a cancellable SSE scenario.

        @return None / None.
        """

        release = asyncio.Event()

        async def endpoint(request: web.Request) -> web.StreamResponse:
            """@brief 发送一个 delta 后等待测试释放 / Send one delta and wait for test release.

            @param request local request / Local request.
            @return streamed response / Streamed response.
            """

            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(
                b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"first"},"finish_reason":null}]}\n\n'
            )
            await release.wait()
            return response

        runner, endpoint_url = await _start_server(endpoint, path="/chat/completions")
        client = ProviderCompletionClient(
            telemetry=Telemetry(TelemetryBuffer(64)),
            session_factory=aiohttp.ClientSession,
        )
        emitted = asyncio.Event()
        seen: list[object] = []

        async def consume() -> None:
            """@brief 在单一 task/context 中消费流 / Consume the stream in one task/context.

            @return None / None.
            """

            async for event in client.stream(
                route=_route(style="openai", endpoint=endpoint_url),
                model="gpt-test",
                messages=(_message("user", "question"),),
                tools=(),
                tool_choice=None,
                max_tokens=32,
                timeout_seconds=None,
                request_meta=normalize_request_meta({}),
            ):
                seen.append(event)
                emitted.set()

        task = asyncio.create_task(consume())
        try:
            await emitted.wait()
            assert isinstance(seen[0], CompletionTextDelta)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not any(isinstance(event, CompletionFinished) for event in seen)
        finally:
            release.set()
            await client.aclose()
            await runner.cleanup()

    asyncio.run(scenario())
