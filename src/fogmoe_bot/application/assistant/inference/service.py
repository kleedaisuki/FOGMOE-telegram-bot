"""@brief Provider route 与可恢复 Agent 的异步编排 / Async orchestration of provider routes and the resumable Agent."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Protocol, cast

from fogmoe_bot.application.runtime import FailureCircuit
from fogmoe_bot.domain.assistant.routing.models import ProviderRoute
from fogmoe_bot.domain.assistant.routing.policy import model_supports_vision
from fogmoe_bot.domain.context import ContextState
from fogmoe_bot.domain.memory.models import MAX_WORKING_MEMORY_MESSAGES
from fogmoe_bot.domain.conversation.payloads import JsonValue

from ..agent_loop import AgentExecutionConfig, AgentResponse
from ..errors import (
    AssistantInferenceUnavailableError,
    ResumableAgentInterruptedError,
    SafetyBlockError,
)
from ..tool_runtime import ToolExecutionContext
from .message_content import messages_have_images, strip_image_content


class AgentRunner(Protocol):
    """@brief inference service 所需 Agent 窄端口 / Narrow Agent port required by inference service."""

    async def run(
        self,
        context: ContextState,
        config: AgentExecutionConfig,
        *,
        tool_context: ToolExecutionContext | None = None,
    ) -> AgentResponse:
        """@brief 运行一个 route / Run one route.

        @param context route-local context / Route-local context.
        @param config route 配置 / Route configuration.
        @param tool_context 可选 durable identity / Optional durable identity.
        @return Agent response / Agent response.
        """

        ...


class AssistantInferenceService:
    """@brief 路由并执行可恢复 Agent 推理 / Route and execute resumable Agent inference."""

    def __init__(
        self,
        *,
        service_order: Iterable[str],
        profiles: Mapping[str, ProviderRoute],
        circuit: FailureCircuit[str],
        text_only_model_patterns: Iterable[str],
        working_memory_limit: int,
        working_memory_max_tokens: int,
        working_memory_enabled: bool,
        agent_loop: AgentRunner,
    ) -> None:
        """@brief 注入 route policy 与 Agent / Inject route policy and Agent.

        @param service_order 候选顺序 / Candidate order.
        @param profiles route profiles / Route profiles.
        @param circuit runtime-owned circuit / Runtime-owned circuit.
        @param text_only_model_patterns 纯文本模型模式 / Text-only model patterns.
        @param working_memory_limit 每次模型 Query 的 WorkingMemory 消息上限 / WorkingMemory message limit per model query.
        @param working_memory_max_tokens 每次换入的硬 token 预算 / Hard token budget for each page-in.
        @param working_memory_enabled 是否为该任务启用 WorkingMemory / Whether WorkingMemory is enabled for this task.
        @param agent_loop 可恢复 Agent port / Resumable Agent port.
        """

        self._service_order = tuple(service_order)
        self._profiles = dict(profiles)
        self._circuit = circuit
        self._text_only_model_patterns = tuple(text_only_model_patterns)
        if not 1 <= working_memory_limit <= MAX_WORKING_MEMORY_MESSAGES:
            raise ValueError(
                "working_memory_limit must be between 1 and "
                f"{MAX_WORKING_MEMORY_MESSAGES}"
            )
        if working_memory_max_tokens < 256:
            raise ValueError("working_memory_max_tokens must be at least 256")
        self._working_memory_limit = working_memory_limit
        self._working_memory_max_tokens = working_memory_max_tokens
        self._working_memory_enabled = working_memory_enabled
        self._agent_loop = agent_loop

    @property
    def circuit(self) -> FailureCircuit[str]:
        """@brief 返回 circuit / Return the circuit.

        @return circuit / Circuit.
        """

        return self._circuit

    async def infer(
        self,
        context_state: ContextState,
        *,
        allow_tools: bool = False,
        request_timeout: float | None = None,
        tool_context: ToolExecutionContext | None = None,
    ) -> AgentResponse:
        """@brief 执行一次可回退推理 / Execute one fallback-capable inference.

        @param context_state attempt-local context / Attempt-local context.
        @param allow_tools 是否暴露 durable tools / Whether to expose durable tools.
        @param request_timeout 单 provider timeout / Per-provider timeout.
        @param tool_context durable tool identity / Durable tool identity.
        @return Agent response / Agent response.
        """

        if request_timeout is not None and request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if allow_tools and tool_context is None:
            raise ValueError("tool_context is required when tools are enabled")
        response, last_error = await self._try_routes(
            context_state,
            allow_tools=allow_tools,
            request_timeout=request_timeout,
            tool_context=tool_context,
        )
        if response is not None:
            return response
        if messages_have_images(context_state.messages):
            fallback = (
                list(context_state.text_fallback_messages)
                if context_state.text_fallback_messages is not None
                else strip_image_content(context_state.messages)
            )
            response, last_error = await self._try_routes(
                context_state,
                messages=fallback,
                allow_tools=allow_tools,
                request_timeout=request_timeout,
                tool_context=tool_context,
            )
            if response is not None:
                return response
        raise AssistantInferenceUnavailableError(
            "All configured Assistant inference routes failed",
            last_error=last_error,
        ) from last_error

    async def _try_routes(
        self,
        context_state: ContextState,
        *,
        messages: list[dict[str, object]] | None = None,
        allow_tools: bool,
        request_timeout: float | None,
        tool_context: ToolExecutionContext | None,
    ) -> tuple[AgentResponse | None, Exception | None]:
        """@brief 按 policy 尝试 routes / Try routes in policy order.

        @param context_state 原 context / Original context.
        @param messages 可选降级消息 / Optional fallback messages.
        @param allow_tools 是否允许工具 / Whether tools are enabled.
        @param request_timeout timeout / Timeout.
        @param tool_context durable identity / Durable identity.
        @return response 与最后错误 / Response and last error.
        """

        last_error: Exception | None = None
        for service_name in self._service_order:
            route = self._profiles.get(service_name)
            if route is None or self._circuit.is_open(service_name):
                continue
            route_context = ContextState(
                context_id=context_state.context_id,
                scope=context_state.scope,
                user_state=context_state.user_state,
                messages=list(messages or context_state.messages),
                tool_context=context_state.tool_context,
                text_fallback_messages=context_state.text_fallback_messages,
                current_user_text=context_state.current_user_text,
            )
            try:
                response = await self._run_route(
                    route,
                    route_context,
                    allow_tools=allow_tools,
                    request_timeout=request_timeout,
                    tool_context=tool_context,
                )
            except ResumableAgentInterruptedError:
                raise
            except SafetyBlockError as error:
                if route.safety_block_on_error:
                    last_error = error
                    continue
                raise
            except Exception as error:
                logging.warning("Assistant route %s failed: %s", service_name, error)
                self._circuit.record_failure(service_name)
                last_error = error
                continue
            self._circuit.record_success(service_name)
            if response.context_state is not None:
                context_state.messages = response.context_state.messages
                context_state.text_fallback_messages = (
                    response.context_state.text_fallback_messages
                )
                response = AgentResponse(
                    response.text,
                    response.events,
                    context_state,
                    response.history_messages,
                )
            return response, None
        return None, last_error

    async def _run_route(
        self,
        route: ProviderRoute,
        context_state: ContextState,
        *,
        allow_tools: bool,
        request_timeout: float | None,
        tool_context: ToolExecutionContext | None,
    ) -> AgentResponse:
        """@brief 尝试 route 内模型链 / Try the model chain within a route.

        @param route route profile / Route profile.
        @param context_state route-local context / Route-local context.
        @param allow_tools 是否允许工具 / Whether tools are enabled.
        @param request_timeout timeout / Timeout.
        @param tool_context durable identity / Durable identity.
        @return Agent response / Agent response.
        """

        if not route.models:
            raise RuntimeError(
                f"No chat model configured for provider: {route.service_name}"
            )
        last_error: Exception | None = None
        original_messages = list(context_state.messages)
        models = list(route.models)
        if messages_have_images(original_messages):
            models.sort(
                key=lambda model: (
                    not model_supports_vision(model, self._text_only_model_patterns)
                )
            )
        for model in models:
            context_state.messages = self._messages_for_model(
                model,
                original_messages,
                context_state.text_fallback_messages,
            )
            options: dict[str, JsonValue] = {
                key: cast(JsonValue, value)
                for key, value in route.completion_kwargs.items()
            }
            if request_timeout is not None:
                options["timeout"] = request_timeout
            try:
                return await self._agent_loop.run(
                    context_state,
                    AgentExecutionConfig(
                        provider=route.provider_name,
                        model=model or "",
                        provider_name=route.display_name,
                        skip_tools=frozenset(route.skip_tools),
                        allow_tools=allow_tools,
                        working_memory_limit=self._working_memory_limit,
                        working_memory_max_tokens=self._working_memory_max_tokens,
                        working_memory_enabled=self._working_memory_enabled,
                        completion_options=options,
                    ),
                    tool_context=tool_context,
                )
            except ResumableAgentInterruptedError:
                raise
            except Exception as error:
                last_error = error
        if (
            route.safety_block_on_error
            and "safety" in str(last_error).lower()
            and "block" in str(last_error).lower()
        ):
            raise SafetyBlockError(str(last_error)) from last_error
        raise RuntimeError(
            f"All models failed for provider: {route.service_name}"
        ) from last_error

    def _messages_for_model(
        self,
        model: str,
        messages: list[dict[str, object]],
        text_fallback_messages: list[dict[str, object]] | None,
    ) -> list[dict[str, object]]:
        """@brief 为模型选择多模态或文本消息 / Select multimodal or text messages for a model.

        @param model 当前候选模型 / Candidate model.
        @param messages 原消息 / Original messages.
        @param text_fallback_messages 文本降级 / Text fallback.
        @return 适合候选模型的消息 / Messages suitable for the candidate model.
        """

        if not messages_have_images(messages) or model_supports_vision(
            model,
            self._text_only_model_patterns,
        ):
            return list(messages)
        return (
            list(text_fallback_messages)
            if text_fallback_messages is not None
            else strip_image_content(messages)
        )


__all__ = ["AgentRunner", "AssistantInferenceService"]
