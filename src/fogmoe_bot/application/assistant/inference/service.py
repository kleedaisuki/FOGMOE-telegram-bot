"""@brief 状态化 Agent 推理服务 / Stateful Agent inference service."""

import asyncio
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from fogmoe_bot.domain.agent_routing import ProviderCircuit, ProviderRoute, model_supports_vision
from fogmoe_bot.domain.agent_runtime.executor import EXECUTOR
from fogmoe_bot.domain.agent_runtime.tools import (
    cleanup_linux_sandbox,
    clear_tool_request_context,
    set_tool_request_context,
)
from fogmoe_bot.infrastructure import config

from ..agent_loop import AgentLoop, AgentResponse, AgentRunRequest, DEFAULT_AGENT_LOOP
from .output import VisibleContentSink
from .visible_output import visible_content_events, visible_content_was_sent
from ..errors import PartialAgentResponseError, SafetyBlockError
from .message_content import messages_have_images, strip_image_content
from .provider_profiles import build_provider_profiles, configured_service_order

AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD = 3
AI_PROVIDER_CIRCUIT_WINDOW_SECONDS = 5 * 60
AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS = 30 * 60

PARTIAL_AGENT_RESPONSE_ERROR_MESSAGE = (
    "看起来对话出现了一些小问题呢。"
    "您可以尝试使用 /clear 命令来清空聊天记录，"
    "然后我们重新开始对话吧！\n"
    "It seems there was a small issue with the conversation."
    "You can try using the /clear command to clear the chat history,"
    "and then we can start over!\n\n"
    "错误信息 Error message: \n\n"
    "问题类型：工具执行后回复生成失败。\n"
    "Issue type: response generation failed after tool execution.\n\n"
    "内部处理失败，详细信息已记录。\n"
    "Internal processing failed. Details have been logged.\n\n"
    "您可以发送给管理员 @ScarletKc 报告此问题。\n"
    "You can report this issue to the admin @ScarletKc."
)

class AssistantInferenceService:
    """@brief 路由并执行 Agent 推理 / Route and execute Agent inference.

    该应用服务持有 provider 熔断状态，并将领域 route 策略落成实际模型调用。
    / This application service owns provider circuit state and turns domain route
    policy into actual model invocations.
    """

    def __init__(
        self,
        *,
        service_order: Iterable[str],
        profiles: Mapping[str, ProviderRoute],
        circuit: ProviderCircuit,
        text_only_model_patterns: Iterable[str],
        agent_loop: AgentLoop = DEFAULT_AGENT_LOOP,
    ) -> None:
        """@brief 初始化推理服务 / Initialize the inference service.

        @param service_order 候选服务优先级 / Candidate service priority.
        @param profiles 服务到 route 的映射 / Service-to-route mapping.
        @param circuit provider 熔断状态机 / Provider circuit-breaker state machine.
        @param text_only_model_patterns 纯文本模型模式 / Text-only model patterns.
        @param agent_loop 共享 Agent 回合编排器 / Shared Agent turn orchestrator.
        """
        self._service_order = tuple(service_order)
        self._profiles = dict(profiles)
        self._circuit = circuit
        self._text_only_model_patterns = tuple(text_only_model_patterns)
        self._agent_loop = agent_loop

    @property
    def circuit(self) -> ProviderCircuit:
        """@brief 读取 provider 熔断状态 / Read provider circuit state.

        @return 熔断状态机 / Circuit-breaker state machine.
        """
        return self._circuit

    async def infer(
        self,
        messages: list[dict[str, Any]],
        *,
        user_id: int,
        tool_context: dict[str, object] | None = None,
        text_fallback_messages: list[dict[str, Any]] | None = None,
        visible_content_sink: VisibleContentSink | None = None,
    ) -> AgentResponse:
        """@brief 执行一次可回退的 Agent 推理 / Run one fallback-capable Agent inference.

        @param messages 初始模型消息 / Initial model messages.
        @param user_id 用户标识 / User identifier.
        @param tool_context Runtime 工具上下文 / Runtime tool context.
        @param text_fallback_messages 多模态失败时的文本降级消息 / Text fallback for multimodal failure.
        @param visible_content_sink 用户可见输出端口 / User-visible output sink.
        @return 最终 Agent 响应 / Final Agent response.
        """
        request_context = dict(tool_context or {})
        request_context.setdefault("user_id", user_id)
        response, last_error = await self._try_routes(
            messages,
            user_id=user_id,
            tool_context=request_context,
            text_fallback_messages=text_fallback_messages,
            visible_content_sink=visible_content_sink,
        )
        if response is not None:
            return response

        if messages_have_images(messages):
            logging.warning("多模态 AI 调用全部失败，降级为纯文本图片描述重试: %s", last_error)
            fallback_messages = (
                list(text_fallback_messages)
                if text_fallback_messages is not None
                else strip_image_content(messages)
            )
            response, _ = await self._try_routes(
                fallback_messages,
                user_id=user_id,
                tool_context=request_context,
                visible_content_sink=visible_content_sink,
            )
            if response is not None:
                return response

        logging.error("所有AI服务均调用失败: %s", last_error)
        return AgentResponse(
            "抱歉喵，雾萌娘在处理你的请求时遇到了一点小问题！现在有点不舒服啦，请稍后再试吧～\n"
            "请联系管理员 @ScarletKc 反馈问题。",
            [],
        )

    async def _try_routes(
        self,
        messages: list[dict[str, Any]],
        *,
        user_id: int,
        tool_context: dict[str, object] | None,
        text_fallback_messages: list[dict[str, Any]] | None = None,
        visible_content_sink: VisibleContentSink | None,
    ) -> tuple[AgentResponse | None, Exception | None]:
        last_error: Exception | None = None
        loop = asyncio.get_running_loop()
        for service_name in self._service_order:
            route = self._profiles.get(service_name)
            if route is None:
                logging.warning("未知 AI route，跳过: %s", service_name)
                continue
            if self._circuit.is_open(service_name):
                logging.warning("%s 当前处于熔断冷却中，跳过调用", service_name)
                continue
            service_messages = self._messages_for_route(route, messages, text_fallback_messages)
            try:
                response = await loop.run_in_executor(
                    EXECUTOR,
                    lambda r=route, m=service_messages: self._run_route_with_context(
                        r,
                        m,
                        tool_context=tool_context,
                        visible_content_sink=visible_content_sink,
                    ),
                )
                self._circuit.record_success(service_name)
                return response, None
            except SafetyBlockError:
                if visible_content_was_sent(visible_content_sink):
                    return AgentResponse("", visible_content_events(visible_content_sink)), None
                if route.safety_block_on_error:
                    logging.warning("%s triggered safety block, trying next route", service_name)
                    last_error = SafetyBlockError("SafetyBlockError")
                    continue
                raise
            except PartialAgentResponseError as exc:
                logging.error("%s failed after partial Agent response: %s", service_name, exc, exc_info=True)
                return AgentResponse(
                    "" if visible_content_was_sent(visible_content_sink) else PARTIAL_AGENT_RESPONSE_ERROR_MESSAGE,
                    exc.events,
                ), None
            except Exception as exc:
                if visible_content_was_sent(visible_content_sink):
                    logging.error("%s failed after visible content: %s", service_name, exc, exc_info=True)
                    return AgentResponse("", visible_content_events(visible_content_sink)), None
                logging.warning("%s 调用失败: %s", service_name, exc)
                self._circuit.record_failure(service_name)
                last_error = exc
        return None, last_error

    def _run_route_with_context(
        self,
        route: ProviderRoute,
        messages: list[dict[str, Any]],
        *,
        tool_context: dict[str, object] | None,
        visible_content_sink: VisibleContentSink | None,
    ) -> AgentResponse:
        """@brief 在 Runtime 上下文中运行 route / Run a route inside Runtime context.

        @param route 要执行的 route / Route to execute.
        @param messages route 输入消息 / Route input messages.
        @param tool_context Runtime 工具上下文 / Runtime tool context.
        @param visible_content_sink 用户可见输出端口 / User-visible output sink.
        @return Agent 响应 / Agent response.
        """
        set_tool_request_context(dict(tool_context or {}))
        try:
            return self._run_route(route, messages, visible_content_sink)
        finally:
            try:
                cleanup_linux_sandbox()
            finally:
                clear_tool_request_context()

    def _run_route(
        self,
        route: ProviderRoute,
        messages: list[dict[str, Any]],
        visible_content_sink: VisibleContentSink | None,
    ) -> AgentResponse:
        """@brief 执行 route 的模型回退链 / Execute a route's model fallback chain.

        @param route 要执行的 route / Route to execute.
        @param messages 模型消息 / Model messages.
        @param visible_content_sink 用户可见输出端口 / User-visible output sink.
        @return Agent 响应 / Agent response.
        """
        if not route.models:
            raise RuntimeError(f"No chat model configured for provider: {route.service_name}")
        last_error: Exception | None = None
        for model in route.models:
            try:
                return self._agent_loop.run(
                    AgentRunRequest(
                        provider=route.provider_name,
                        model=model or "",
                        messages=list(messages),
                        provider_name=route.display_name,
                        skip_tools=frozenset(route.skip_tools),
                        completion_kwargs=route.completion_kwargs or None,
                        visible_content_handler=visible_content_sink,
                    )
                )
            except PartialAgentResponseError:
                raise
            except Exception as exc:
                last_error = exc
                logging.warning("%s model=%s failed: %s", route.display_name, model, exc)
        if route.safety_block_on_error and "safety" in str(last_error).lower() and "block" in str(last_error).lower():
            raise SafetyBlockError(str(last_error)) from last_error
        raise RuntimeError(f"All models failed for provider: {route.service_name}") from last_error

    def _messages_for_route(
        self,
        route: ProviderRoute,
        messages: list[dict[str, Any]],
        text_fallback_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """@brief 为 route 准备兼容消息 / Prepare compatible messages for a route.

        @param route 目标 route / Target route.
        @param messages 原始消息 / Original messages.
        @param text_fallback_messages 可选文本降级消息 / Optional text fallback messages.
        @return route 可消费的消息 / Messages consumable by the route.
        """
        if not messages_have_images(messages) or model_supports_vision(
            route.models[0] if route.models else None,
            self._text_only_model_patterns,
        ):
            return list(messages)
        logging.info("AI chat route %s is text-only; using vision text fallback", route.service_name)
        return list(text_fallback_messages) if text_fallback_messages is not None else strip_image_content(messages)


ASSISTANT_INFERENCE_SERVICE = AssistantInferenceService(
    service_order=configured_service_order(),
    profiles=build_provider_profiles(),
    circuit=ProviderCircuit(
        failure_threshold=AI_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
        window_seconds=AI_PROVIDER_CIRCUIT_WINDOW_SECONDS,
        cooldown_seconds=AI_PROVIDER_CIRCUIT_COOLDOWN_SECONDS,
    ),
    text_only_model_patterns=config.AI_CHAT_TEXT_ONLY_MODELS,
)
