"""@brief 自包含 route 的 Assistant 推理服务测试 / Tests for Assistant inference with self-contained routes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from fogmoe_bot.application.assistant.agent_loop import AgentResponse
from fogmoe_bot.application.assistant.errors import (
    AssistantInferenceUnavailableError,
    ProviderFailure,
    ProviderFailureKind,
    ResumableAgentInterruptedError,
    SafetyBlockError,
)
from fogmoe_bot.application.assistant.inference.service import AssistantInferenceService
from fogmoe_bot.application.runtime import FailureCircuit, FailureCircuitPolicy
from fogmoe_bot.domain.assistant.messages import (
    CanonicalMessage,
    ImagePart,
    TextPart,
    UrlImageSource,
)
from fogmoe_bot.domain.assistant.routing.models import (
    ProviderAuth,
    ProviderRoute,
    RouteModel,
)
from fogmoe_bot.domain.context import ContextState, ConversationScope, UserState
from fogmoe_bot.domain.conversation.errors import StaleClaimError
from fogmoe_bot.domain.conversation.message import MessageRole


def _route(
    route_id: str,
    *,
    models: tuple[RouteModel, ...] | None = None,
    supports_tools: bool = True,
    safety_block_is_terminal: bool = False,
) -> ProviderRoute:
    """@brief 构造测试用自包含 OpenAI-style route / Build a self-contained OpenAI-style test route.

    @param route_id circuit 与 telemetry route ID / Route ID for circuit and telemetry.
    @param models 同 provider 模型 fallback 链 / Same-provider model fallback chain.
    @param supports_tools 是否支持 tools / Whether tools are supported.
    @param safety_block_is_terminal safety block 是否终止 fallback / Whether a safety block terminates fallback.
    @return 有效 provider route / Valid provider route.
    """

    return ProviderRoute(
        route_id=route_id,
        provider_id=route_id,
        provider_label=route_id,
        style="openai",
        endpoint=f"https://{route_id}.example.test/v1/chat/completions",
        auth=ProviderAuth(),
        models=models or (RouteModel("model", accepts_images=True),),
        supports_tools=supports_tools,
        safety_block_is_terminal=safety_block_is_terminal,
    )


def _service(
    *,
    routes: tuple[ProviderRoute, ...],
    runner: Callable[..., AgentResponse],
) -> AssistantInferenceService:
    """@brief 构造带记录 fake Agent 的推理服务 / Build an inference service with a recording fake Agent.

    @param routes 有序 route fallback 链 / Ordered route fallback chain.
    @param runner 注入的模型行为 / Injected model behavior.
    @return 已配置 inference service / Configured inference service.
    """

    class _AgentLoop:
        """@brief 将 AgentExecutionConfig 转交给测试函数 / Forward AgentExecutionConfig to a test function."""

        async def run(
            self,
            context: ContextState,
            config: Any,
            *,
            tool_context: object | None = None,
        ) -> AgentResponse:
            """@brief 运行记录的测试行为 / Run the recorded test behavior.

            @param context 模型上下文 / Model context.
            @param config 路由执行配置 / Route execution configuration.
            @param tool_context 可选工具身份 / Optional tool identity.
            @return 测试 runner 的 response / Response from the test runner.
            """

            return runner(
                config.route.provider_id,
                config.model,
                context.messages,
                route=config.route,
                provider_name=config.route.provider_label,
                skip_tools=config.skip_tools,
                allow_tools=config.allow_tools,
                timeout_seconds=config.timeout_seconds,
                request_meta=config.request_meta,
                tool_context=tool_context,
            )

    return AssistantInferenceService(
        routes=routes,
        circuit=FailureCircuit[str](
            FailureCircuitPolicy(
                failure_threshold=3,
                failure_window_seconds=300,
                cooldown_seconds=1800,
            )
        ),
        working_memory_limit=4,
        working_memory_max_tokens=8192,
        working_memory_enabled=True,
        agent_loop=_AgentLoop(),
    )


def _context(
    messages: list[CanonicalMessage],
    *,
    text_fallback_messages: list[CanonicalMessage] | None = None,
) -> ContextState:
    """@brief 构造最小 Assistant 上下文 / Build a minimal Assistant context.

    @param messages 原始模型消息 / Original model messages.
    @param text_fallback_messages 预生成的纯文本 fallback / Pre-generated text-only fallback.
    @return 可供推理的上下文 / Context ready for inference.
    """

    return ContextState(
        context_id=uuid4(),
        scope=ConversationScope(user_id=123),
        user_state=UserState(coins=10, plan="free", permission=0, profile=None),
        messages=messages,
        tool_context={
            "user_id": 123,
            "is_group": False,
            "group_id": None,
            "message_id": None,
        },
        text_fallback_messages=text_fallback_messages,
    )


def _user_message(*parts: TextPart | ImagePart) -> CanonicalMessage:
    """@brief 构造规范用户消息 / Build a canonical user message.

    @param parts 保序的文本或图像 parts / Ordered text or image parts.
    @return 规范 V2 用户消息 / Canonical V2 user message.
    """

    return CanonicalMessage(MessageRole.USER, parts)


def test_inference_retries_image_messages_as_text_after_all_routes_fail() -> None:
    """@brief 图像 route 均失败后以纯文本重试 / Retry image messages as text after all image routes fail."""

    image_messages = [
        _user_message(
            TextPart("describe this image"),
            ImagePart(UrlImageSource("https://example.test/a.png")),
        )
    ]
    calls: list[object] = []

    def runner(
        provider: str, model: str, messages: object, **_: object
    ) -> AgentResponse:
        """@brief 首次调用失败、第二次成功 / Fail once, then succeed.

        @param provider provider ID / Provider ID.
        @param model 模型名 / Model name.
        @param messages 传给模型的消息 / Messages passed to the model.
        @return 成功 response 或抛出错误 / Successful response or an error.
        """

        del provider, model
        calls.append(messages)
        if len(calls) == 1:
            raise RuntimeError("provider failed")
        return AgentResponse("text fallback response", [])

    service = _service(routes=(_route("openai"),), runner=runner)

    response = asyncio.run(service.infer(_context(image_messages)))

    assert response.text == "text fallback response"
    assert response.events == []
    assert calls == [
        image_messages,
        [_user_message(TextPart("describe this image"))],
    ]


def test_text_only_route_uses_vision_text_fallback_messages() -> None:
    """@brief accepts_images=false 的模型使用文本 fallback / A non-image model uses the text fallback."""

    image_messages = [
        _user_message(
            TextPart("runtime message without description"),
            ImagePart(UrlImageSource("https://example.test/a.png")),
        )
    ]
    text_fallback_messages = [_user_message(TextPart("a cat on a desk"))]
    calls: list[object] = []

    def runner(
        provider: str, model: str, messages: object, **_: object
    ) -> AgentResponse:
        """@brief 记录消息并成功 / Record messages and succeed.

        @param provider provider ID / Provider ID.
        @param model 模型名 / Model name.
        @param messages 传给模型的消息 / Messages passed to the model.
        @return 固定成功 response / Fixed successful response.
        """

        del provider, model
        calls.append(messages)
        return AgentResponse("ok", [])

    service = _service(
        routes=(
            _route(
                "siliconflow",
                models=(RouteModel("vendor/text-small", accepts_images=False),),
            ),
        ),
        runner=runner,
    )

    response = asyncio.run(
        service.infer(
            _context(image_messages, text_fallback_messages=text_fallback_messages)
        )
    )
    assert response.text == "ok"
    assert response.events == []
    assert calls == [text_fallback_messages]


def test_vision_capable_route_keeps_multimodal_messages() -> None:
    """@brief accepts_images=true 的模型保留多模态消息 / An image-capable model keeps multimodal messages."""

    image_messages = [
        _user_message(ImagePart(UrlImageSource("https://example.test/a.png")))
    ]
    calls: list[object] = []

    def runner(
        provider: str, model: str, messages: object, **_: object
    ) -> AgentResponse:
        """@brief 记录多模态消息并成功 / Record multimodal messages and succeed.

        @param provider provider ID / Provider ID.
        @param model 模型名 / Model name.
        @param messages 传给模型的消息 / Messages passed to the model.
        @return 固定成功 response / Fixed successful response.
        """

        del provider, model
        calls.append(messages)
        return AgentResponse("ok", [])

    service = _service(
        routes=(
            _route(
                "openai",
                models=(RouteModel("gpt-4o", accepts_images=True),),
            ),
        ),
        runner=runner,
    )

    response = asyncio.run(service.infer(_context(image_messages)))
    assert response.text == "ok"
    assert response.events == []
    assert calls == [image_messages]


def test_image_messages_prioritize_the_image_capable_model_within_one_route() -> None:
    """@brief 图像消息优先同 route 内 accepts_images 模型 / Image messages prioritize an accepts_images model in the same route."""

    image_messages = [
        _user_message(ImagePart(UrlImageSource("https://example.test/a.png")))
    ]
    calls: list[tuple[str, object]] = []

    def runner(
        provider: str, model: str, messages: object, **_: object
    ) -> AgentResponse:
        """@brief 记录候选模型并成功 / Record the candidate model and succeed.

        @param provider provider ID / Provider ID.
        @param model 模型名 / Model name.
        @param messages 传给模型的消息 / Messages passed to the model.
        @return 固定成功 response / Fixed successful response.
        """

        del provider
        calls.append((model, messages))
        return AgentResponse("ok", [])

    service = _service(
        routes=(
            _route(
                "openrouter",
                models=(
                    RouteModel("deepseek-text", accepts_images=False),
                    RouteModel("qwen-vision", accepts_images=True),
                ),
            ),
        ),
        runner=runner,
    )

    response = asyncio.run(service.infer(_context(image_messages)))

    assert response.text == "ok"
    assert calls == [("qwen-vision", image_messages)]


def test_resumable_partial_stream_failure_does_not_run_model_b_in_generation() -> None:
    """@brief 模型 A 已产生可见 partial 后中断时，同 generation 不运行模型 B / Model B is not run in the same generation after model A has a visible partial interruption."""

    calls: list[str] = []

    def runner(
        provider: str,
        model: str,
        messages: object,
        **_: object,
    ) -> AgentResponse:
        """@brief A 抛出 partial-stream 中断，B 若被调用则暴露错误 / Model A raises a partial-stream interruption; a call to B exposes the bug."""

        del provider, messages
        calls.append(model)
        if model == "model-a":
            raise ResumableAgentInterruptedError(
                "model A emitted old before disconnecting"
            )
        return AgentResponse("new", [])

    service = _service(
        routes=(
            _route(
                "partial",
                models=(
                    RouteModel("model-a"),
                    RouteModel("model-b"),
                ),
            ),
        ),
        runner=runner,
    )

    with pytest.raises(
        ResumableAgentInterruptedError,
        match="emitted old",
    ):
        asyncio.run(service.infer(_context([_user_message(TextPart("hello"))])))

    assert calls == ["model-a"]


def test_stale_generation_does_not_fall_back_to_another_model_or_route() -> None:
    """@brief steer 失效的 generation 不得继续模型或 route fallback /
    A generation invalidated by steering must not continue to another model or route.
    """

    calls: list[tuple[str, str]] = []

    def runner(
        provider: str,
        model: str,
        messages: object,
        **_: object,
    ) -> AgentResponse:
        """@brief 首个候选抛出 generation fence / Raise the generation fence from the first candidate.

        @param provider provider ID / Provider ID.
        @param model 模型名 / Model name.
        @param messages 未使用模型消息 / Unused model messages.
        @return 永不返回 / Never returns.
        @raise StaleClaimError 当前 generation 已被 steer / Current generation was steered.
        """

        del messages
        calls.append((provider, model))
        raise StaleClaimError("steered")

    service = _service(
        routes=(
            _route(
                "primary",
                models=(RouteModel("model-a"), RouteModel("model-b")),
            ),
            _route("fallback"),
        ),
        runner=runner,
    )

    with pytest.raises(StaleClaimError, match="steered"):
        asyncio.run(service.infer(_context([_user_message(TextPart("hello"))])))

    assert calls == [("primary", "model-a")]


def test_open_circuit_skips_to_next_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """@brief 已打开的 route circuit 直接跳至下一个 route / An open route circuit skips directly to the next route.

    @param monkeypatch pytest monkeypatch fixture / Pytest monkeypatch fixture.
    """

    calls: list[str] = []

    def runner(
        provider: str, model: str, messages: object, **_: object
    ) -> AgentResponse:
        """@brief 记录 provider 并成功 / Record the provider and succeed.

        @param provider provider ID / Provider ID.
        @param model 模型名 / Model name.
        @param messages 传给模型的消息 / Messages passed to the model.
        @return 固定成功 response / Fixed successful response.
        """

        del model, messages
        calls.append(provider)
        return AgentResponse("ok", [])

    service = _service(
        routes=(_route("gemini"), _route("siliconflow")),
        runner=runner,
    )
    acquire = service.circuit.try_acquire
    monkeypatch.setattr(
        service.circuit,
        "try_acquire",
        lambda route_id: None if route_id == "gemini" else acquire(route_id),
    )

    response = asyncio.run(service.infer(_context([])))
    assert response.text == "ok"
    assert response.events == []
    assert calls == ["siliconflow"]


def test_terminal_safety_block_does_not_bypass_to_a_later_route() -> None:
    """@brief terminal safety block 不会被后续 route 绕过 / A terminal safety block is not bypassed by later routes."""

    calls: list[str] = []

    def runner(
        provider: str, model: str, messages: object, **_: object
    ) -> AgentResponse:
        """@brief 抛出 safety block / Raise a safety block.

        @param provider provider ID / Provider ID.
        @param model 模型名 / Model name.
        @param messages 传给模型的消息 / Messages passed to the model.
        @return 永不返回 / Never returns.
        """

        del model, messages
        calls.append(provider)
        raise RuntimeError("safety block")

    service = _service(
        routes=(
            _route("safe", safety_block_is_terminal=True),
            _route("fallback"),
        ),
        runner=runner,
    )

    with pytest.raises(SafetyBlockError):
        asyncio.run(service.infer(_context([])))
    assert calls == ["safe"]


def test_service_preserves_typed_provider_failure_after_model_exhaustion() -> None:
    """@brief 同 route 模型耗尽后保留 typed provider failure / Preserve a typed provider failure after same-route model exhaustion."""

    failure = ProviderFailure(
        kind=ProviderFailureKind.RATE_LIMITED,
        status=429,
        message="LLM provider HTTP 429",
    )

    def runner(_: str, __: str, ___: object, **____: object) -> AgentResponse:
        """@brief 使唯一模型返回同一 typed failure / Make the only model return the same typed failure.

        @param _ 未使用 provider ID / Unused provider ID.
        @param __ 未使用模型名 / Unused model name.
        @param ___ 未使用消息 / Unused messages.
        @param ____ 未使用附加参数 / Unused extra arguments.
        @return 永不返回 / Never returns.
        @raise ProviderFailure 固定 typed provider failure / Fixed typed provider failure.
        """

        raise failure

    service = _service(routes=(_route("openrouter"),), runner=runner)

    with pytest.raises(AssistantInferenceUnavailableError) as captured:
        asyncio.run(service.infer(_context([])))
    assert captured.value.last_error is failure


def test_contract_failure_short_circuits_models_routes_and_image_fallback() -> None:
    """@brief contract 失败立即短路模型、路由和图像降级 /
    A contract failure immediately short-circuits models, routes, and image fallback.
    """

    failure = ProviderFailure(
        kind=ProviderFailureKind.CONTRACT,
        status=400,
        message="invalid model slug",
    )
    calls: list[tuple[str, str]] = []

    def runner(
        provider: str,
        model: str,
        messages: object,
        **_: object,
    ) -> AgentResponse:
        """@brief 记录唯一请求并抛出 contract / Record the sole request and raise a contract failure."""

        del messages
        calls.append((provider, model))
        raise failure

    service = _service(
        routes=(
            _route(
                "primary",
                models=(RouteModel("bad-slug"), RouteModel("model-b")),
            ),
            _route("fallback"),
        ),
        runner=runner,
    )
    context = _context(
        [
            _user_message(
                TextPart("describe"),
                ImagePart(UrlImageSource("https://example.test/a.png")),
            )
        ]
    )

    with pytest.raises(AssistantInferenceUnavailableError) as captured:
        asyncio.run(service.infer(context))

    assert captured.value.last_error is failure
    assert calls == [("primary", "bad-slug")]


def test_transient_provider_failure_still_falls_back_to_the_next_model() -> None:
    """@brief 暂态 provider 失败仍允许切换同 route 下一模型 /
    A transient provider failure still permits the next model in the same route.
    """

    calls: list[str] = []

    def runner(
        provider: str,
        model: str,
        messages: object,
        **_: object,
    ) -> AgentResponse:
        """@brief 首模型超时、次模型成功 / Time out the first model and succeed on the second."""

        del provider, messages
        calls.append(model)
        if len(calls) == 1:
            raise ProviderFailure(
                kind=ProviderFailureKind.TIMEOUT,
                message="provider timed out",
            )
        return AgentResponse("ok", [])

    service = _service(
        routes=(
            _route(
                "primary",
                models=(RouteModel("model-a"), RouteModel("model-b")),
            ),
        ),
        runner=runner,
    )

    response = asyncio.run(service.infer(_context([])))

    assert response.text == "ok"
    assert calls == ["model-a", "model-b"]
