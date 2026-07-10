"""@brief 状态化 Agent 回合编排 / Stateful Agent turn orchestration."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from fogmoe_bot.domain.agent_runtime import AgentRuntime, DEFAULT_AGENT_RUNTIME, ToolTask
from fogmoe_bot.domain.agent_runtime.events import RuntimeEvent
from fogmoe_bot.domain.agent_runtime.protocol import (
    assistant_message_to_plain,
    normalise_tool_calls,
)
from fogmoe_bot.domain.context import ContextState
from fogmoe_bot.infrastructure.llm.litellm_client import create_chat_completion

from .errors import PartialAgentResponseError
from .inference.output import VisibleContentSink
from .inference.visible_output import emit_visible_content


CompletionClient = Callable[..., Any]
"""@brief 同步模型完成调用 / Synchronous model-completion call."""


class AgentResponse(NamedTuple):
    """@brief Agent 回合输出 / Agent turn output."""

    text: str
    events: list[RuntimeEvent]


@dataclass(frozen=True)
class AgentExecutionConfig:
    """@brief AgentLoop 的运行配置 / Runtime configuration for AgentLoop.

    配置只描述模型与循环策略，不携带用户、消息或其他 ContextState 内容。
    / Configuration describes model and loop policy only; it carries no user,
    message, or other ContextState content.
    """

    provider: str
    model: str
    provider_name: str = "AI"
    tool_choice: str | dict[str, object] = "auto"
    max_tokens: int = 4096
    max_iterations: int = 10
    skip_tools: frozenset[str] = field(default_factory=frozenset)
    completion_kwargs: dict[str, Any] | None = None


@dataclass
class AgentExecutionState:
    """@brief 单回合的可变执行状态 / Mutable execution state for one turn.

    该对象只在 AgentLoop 内演进；长期记忆由上层持久化。
    / This object evolves only inside AgentLoop; long-term memory is persisted by
    upper layers.
    """

    context: ContextState
    config: AgentExecutionConfig
    messages: list[dict[str, Any]]
    events: list[RuntimeEvent] = field(default_factory=list)
    iteration: int = 0

    @classmethod
    def from_context(
        cls,
        context: ContextState,
        config: AgentExecutionConfig,
    ) -> "AgentExecutionState":
        """@brief 从领域上下文创建执行状态 / Create execution state from domain context.

        @param context 本回合领域上下文 / Domain context for this turn.
        @param config AgentLoop 运行配置 / AgentLoop runtime configuration.
        @return 尚未执行的回合状态 / Unexecuted turn state.
        """
        return cls(
            context=context,
            config=config,
            messages=[
                message
                for message in context.messages
                if message.get("content") is not None or message.get("tool_calls")
            ],
        )

class AgentLoop:
    """@brief 执行单次 Agent 工具回合 / Execute one Agent tool-use turn.

    Loop 持有 Runtime、模型调用与上下文构造依赖；每次 run 的消息、事件和轮次状态
    都是局部变量，因此共享实例不会在并发请求之间泄露状态。
    / The Loop owns Runtime, model-call and context-building dependencies. Each
    run keeps messages, events and iteration state local, so a shared instance
    does not leak state across concurrent requests.
    """

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        completion_client: CompletionClient,
    ) -> None:
        """@brief 初始化 Agent Loop / Initialize the Agent Loop.

        @param runtime AgentRuntime 任务执行环境 / AgentRuntime task execution environment.
        @param completion_client 同步模型调用依赖 / Synchronous model-call dependency.
        """
        self._runtime = runtime
        self._completion_client = completion_client

    def run(
        self,
        context: ContextState,
        config: AgentExecutionConfig,
        *,
        visible_content_handler: VisibleContentSink | None = None,
        state: AgentExecutionState | None = None,
    ) -> AgentResponse:
        """@brief 驱动一个 Agent 回合 / Drive an Agent turn.

        @param context 本回合领域上下文 / Domain context for this turn.
        @param config AgentLoop 运行配置 / AgentLoop runtime configuration.
        @param visible_content_handler 本回合可见输出端口 / Visible output sink for this turn.
        @param state 可选的既有执行状态 / Optional existing execution state.
        @return 文本回复和 Runtime 事件 / Text reply and Runtime events.
        """
        state = state or AgentExecutionState.from_context(context, config)
        if state.context != context or state.config != config:
            raise ValueError("AgentExecutionState does not belong to this ContextState/config pair")

        for iteration in range(state.iteration, config.max_iterations):
            state.iteration = iteration + 1
            response = self._request_with_partial_events(
                config=config,
                state=state,
                tools=self._runtime.tool_definitions,
                tool_choice=config.tool_choice,
            )
            assistant_message = response.choices[0].message
            raw_tool_calls = getattr(assistant_message, "tool_calls", None)
            assistant_content = assistant_message.content or ""
            if not raw_tool_calls:
                logging.info("%s 第 %s 轮：无工具调用，直接返回答案", config.provider_name, iteration + 1)
                return self._final_response(
                    config=config,
                    state=state,
                    content_text=assistant_content,
                    visible_content_handler=visible_content_handler,
                )

            tool_calls = normalise_tool_calls(raw_tool_calls)
            logging.info("%s 第 %s 轮：检测到 %s 个工具调用", config.provider_name, iteration + 1, len(tool_calls))
            assistant_content_for_model, completed = self._emit_intermediate_content(
                content=assistant_content,
                config=config,
                state=state,
                visible_content_handler=visible_content_handler,
            )
            if not completed:
                return AgentResponse("", state.events)

            assistant_model_message = assistant_message_to_plain(
                assistant_message,
                content=assistant_content_for_model,
                tool_calls=tool_calls,
            )
            state.messages.append(assistant_model_message)
            self._consume_tool_calls(
                tool_calls=tool_calls,
                assistant_message=assistant_model_message,
                config=config,
                state=state,
                visible_content_handler=visible_content_handler,
            )

        logging.warning("%s 工具调用次数超限（%s轮）", config.provider_name, config.max_iterations)
        response = self._request_with_partial_events(
            config=config,
            state=state,
        )
        assistant_message = response.choices[0].message
        if getattr(assistant_message, "tool_calls", None):
            logging.warning("%s 工具调用超限后的最终回复仍包含工具调用，忽略工具调用。", config.provider_name)
        return self._final_response(
            config=config,
            state=state,
            content_text=assistant_message.content or "",
            visible_content_handler=visible_content_handler,
        )

    def _request_with_partial_events(
        self,
        *,
        config: AgentExecutionConfig,
        state: AgentExecutionState,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
    ) -> Any:
        """@brief 请求模型并保留部分事件 / Request a model while preserving partial events.

        @param config AgentLoop 运行配置 / AgentLoop runtime configuration.
        @param state 当前可恢复状态 / Current resumable state.
        @param tools 可选工具定义 / Optional tool definitions.
        @param tool_choice 可选工具选择策略 / Optional tool-choice policy.
        @return provider 响应 / Provider response.
        """
        request_kwargs: dict[str, Any] = {
            "messages": state.messages,
            "max_tokens": config.max_tokens,
            **(config.completion_kwargs or {}),
        }
        if tools is not None:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = tool_choice
        try:
            return self._completion_client(config.provider, config.model, **request_kwargs)
        except Exception as exc:
            if state.events:
                raise PartialAgentResponseError(str(exc), state.events) from exc
            raise

    def _emit_intermediate_content(
        self,
        *,
        content: str,
        config: AgentExecutionConfig,
        state: AgentExecutionState,
        visible_content_handler: VisibleContentSink | None,
    ) -> tuple[str, bool]:
        """@brief 投递带工具调用的中间文本 / Emit intermediate text accompanying tool calls.

        @param content assistant 中间文本 / Assistant intermediate text.
        @param config AgentLoop 运行配置 / AgentLoop runtime configuration.
        @param state 当前可恢复状态 / Current resumable state.
        @return 回填模型的文本和是否完成 / Model-feedback text and completion flag.
        """
        if visible_content_handler is None or not content.strip():
            return content, True
        visible_result = emit_visible_content(
            visible_content_handler,
            content,
            provider_name=config.provider_name,
        )
        if visible_result.content:
            state.events.append({"type": "assistant_visible", "content": visible_result.content})
            return visible_result.content, visible_result.completed
        return content, visible_result.completed

    def _consume_tool_calls(
        self,
        *,
        tool_calls: list[dict[str, Any]],
        assistant_message: dict[str, Any],
        config: AgentExecutionConfig,
        state: AgentExecutionState,
        visible_content_handler: VisibleContentSink | None,
    ) -> None:
        """@brief 提交并消费本轮工具调用 / Submit and consume this turn's tool calls.

        @param tool_calls 已归一化工具调用 / Normalized tool calls.
        @param assistant_message 要持久化的 assistant 调用消息 / Assistant call message to persist.
        @param config AgentLoop 运行配置 / AgentLoop runtime configuration.
        @param state 当前可恢复状态 / Current resumable state.
        """
        assistant_message_logged = False
        for tool_call in tool_calls:
            function_payload = tool_call.get("function") or {}
            function_name = function_payload.get("name")
            if not function_name:
                logging.warning("%s 返回的工具调用缺少函数名: %s", config.provider_name, tool_call)
                continue
            if function_name in config.skip_tools:
                continue
            task_result = self._execute_tool_task(
                tool_name=function_name,
                raw_arguments=function_payload.get("arguments"),
                invocation_id=tool_call.get("id"),
                config=config,
                visible_content_handler=visible_content_handler,
            )
            assistant_message_logged = self._append_task_events(
                task_result=task_result,
                assistant_message=assistant_message,
                assistant_message_logged=assistant_message_logged,
                state=state,
            )

    def _execute_tool_task(
        self,
        *,
        tool_name: str,
        raw_arguments: Any,
        invocation_id: str | None,
        config: AgentExecutionConfig,
        visible_content_handler: VisibleContentSink | None,
    ) -> Any:
        """@brief 在 Runtime 中执行一个能力任务 / Execute one capability task in the Runtime.

        @param tool_name 能力名称 / Capability name.
        @param raw_arguments provider 原始参数 / Raw provider arguments.
        @param invocation_id provider 调用标识 / Provider invocation identifier.
        @param config AgentLoop 运行配置 / AgentLoop runtime configuration.
        @return Runtime 任务结果 / Runtime task result.
        """
        handle = self._runtime.submit(
            ToolTask(
                name=tool_name,
                arguments=self._parse_tool_arguments(raw_arguments, provider_name=config.provider_name),
                invocation_id=invocation_id,
                producer_name=config.provider_name,
            )
        )
        return self._runtime.consume(handle, visible_content_handler=visible_content_handler)

    @staticmethod
    def _append_task_events(
        *,
        task_result: Any,
        assistant_message: dict[str, Any],
        assistant_message_logged: bool,
        state: AgentExecutionState,
    ) -> bool:
        """@brief 记录任务事件并回填模型结果 / Record task events and feed result back to the model.

        @param task_result Runtime 任务结果 / Runtime task result.
        @param assistant_message assistant 调用消息 / Assistant call message.
        @param assistant_message_logged 是否已记录调用消息 / Whether the call message was logged.
        @param state 当前可恢复状态 / Current resumable state.
        @return 更新后的调用消息记录状态 / Updated call-message logging state.
        """
        call_event: RuntimeEvent = {
            "type": "assistant_tool_call",
            "tool_name": task_result.name,
            "arguments": task_result.logged_arguments,
            "tool_call_id": task_result.invocation_id,
        }
        if task_result.validation_error is not None:
            call_event["validation_error"] = task_result.validation_error
        if not assistant_message_logged:
            call_event["assistant_message"] = assistant_message
            assistant_message_logged = True
        state.events.append(call_event)
        state.messages.append(
            {
                "role": "tool",
                "tool_call_id": task_result.invocation_id,
                "name": task_result.name,
                "content": json.dumps(task_result.public_result, ensure_ascii=False),
            }
        )
        result_event: RuntimeEvent = {
            "type": "tool_result",
            "tool_name": task_result.name,
            "arguments": task_result.arguments,
            "result": task_result.public_result,
            "tool_call_id": task_result.invocation_id,
            "internal_result": task_result.internal_result,
        }
        if task_result.media_sent:
            result_event["media_sent"] = True
            result_event["sent_message_count"] = task_result.sent_message_count
        state.events.append(result_event)
        return assistant_message_logged

    @staticmethod
    def _parse_tool_arguments(raw_arguments: Any, *, provider_name: str) -> Any:
        """@brief 解析 Agent 工具参数 / Parse Agent tool arguments.

        @param raw_arguments provider 返回的原始参数 / Raw provider arguments.
        @param provider_name provider 名称 / Provider name.
        @return 可提交给 Runtime 的参数 / Arguments suitable for Runtime submission.
        """
        if isinstance(raw_arguments, (dict, list)):
            return raw_arguments
        try:
            return json.loads(raw_arguments or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            logging.error("%s 工具参数解析失败: %s", provider_name, exc)
            return {}

    @staticmethod
    def _final_response(
        *,
        config: AgentExecutionConfig,
        state: AgentExecutionState,
        content_text: str,
        visible_content_handler: VisibleContentSink | None,
    ) -> AgentResponse:
        """@brief 处理 Agent 最终文本 / Handle final Agent text.

        @param config AgentLoop 运行配置 / AgentLoop runtime configuration.
        @param state 当前可恢复状态 / Current resumable state.
        @param content_text Agent 最终文本 / Final Agent text.
        @return 回复文本和事件 / Reply text and events.
        """
        if content_text.strip():
            if visible_content_handler:
                visible_result = emit_visible_content(
                    visible_content_handler,
                    content_text,
                    provider_name=config.provider_name,
                )
                if visible_result.content:
                    state.events.append({"type": "assistant_visible", "content": visible_result.content})
                    return AgentResponse("", state.events)
                if not visible_result.completed:
                    return AgentResponse("", state.events)
            return AgentResponse(content_text, state.events)
        if state.events:
            logging.warning("%s 工具调用后最终回复为空。", config.provider_name)
        return AgentResponse(content_text, state.events)


DEFAULT_AGENT_LOOP = AgentLoop(
    runtime=DEFAULT_AGENT_RUNTIME,
    completion_client=create_chat_completion,
)
"""@brief 进程共享 Agent Loop / Process-shared Agent Loop."""
