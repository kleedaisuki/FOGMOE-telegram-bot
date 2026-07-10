"""@brief 状态化 Agent 回合编排 / Stateful Agent turn orchestration."""

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from fogmoe_bot.domain.agent_runtime import AgentRuntime, DEFAULT_AGENT_RUNTIME, ToolTask
from fogmoe_bot.domain.agent_runtime.events import RuntimeEvent
from fogmoe_bot.domain.agent_runtime.protocol import (
    assistant_message_to_plain,
    normalise_tool_calls,
)
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
class AgentRunRequest:
    """@brief 一次 Agent 回合的不可变输入 / Immutable input for one Agent turn.

    可见内容输出端口是进程内依赖，故不会被 ``AgentRunState`` 持久化；恢复检查点时
    由调用方重新注入。
    / The visible-content sink is an in-process dependency. It is excluded from
    ``AgentRunState`` persistence and must be injected again when restoring.
    """

    provider: str
    model: str
    messages: list[dict[str, Any]]
    provider_name: str = "AI"
    tool_choice: str | dict[str, object] = "auto"
    max_tokens: int = 4096
    max_iterations: int = 10
    skip_tools: frozenset[str] = field(default_factory=frozenset)
    completion_kwargs: dict[str, Any] | None = None
    visible_content_handler: VisibleContentSink | None = None


@dataclass
class AgentRunState:
    """@brief 可检查点化的 Agent 执行状态 / Checkpointable Agent execution state.

    仅保存 JSON 数据：运行配置、已扩展的模型消息、Runtime 事件与迭代进度。因此状态
    可以直接落进 JSON/JSONB 字段，也可以在进程故障后恢复。回调等运行时对象不进入
    快照。
    / Only JSON data is stored: execution configuration, expanded model messages,
    Runtime events and progress. The state can therefore be saved in JSON/JSONB
    and restored after process failure. Runtime objects such as callbacks are not
    included in a snapshot.
    """

    request: AgentRunRequest
    messages: list[dict[str, Any]]
    events: list[RuntimeEvent] = field(default_factory=list)
    iteration: int = 0

    @classmethod
    def from_request(cls, request: AgentRunRequest) -> "AgentRunState":
        """@brief 从请求创建初始状态 / Create initial state from a request.

        @param request Agent 回合输入 / Agent turn input.
        @return 尚未执行的持久化状态 / Unexecuted, persistable state.
        """
        return cls(
            request=request,
            messages=[
                message
                for message in request.messages
                if message.get("content") is not None or message.get("tool_calls")
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        """@brief 导出 JSON 可持久化快照 / Export a JSON-persistable snapshot.

        @return 可直接编码为 JSON 的状态字典 / State dictionary directly encodable as JSON.
        @raises TypeError 状态包含非 JSON 数据时抛出 / Raised for non-JSON state data.
        """
        request = self.request
        payload = {
            "request": {
                "provider": request.provider,
                "model": request.model,
                "provider_name": request.provider_name,
                "tool_choice": request.tool_choice,
                "max_tokens": request.max_tokens,
                "max_iterations": request.max_iterations,
                "skip_tools": sorted(request.skip_tools),
                "completion_kwargs": request.completion_kwargs,
            },
            "messages": self.messages,
            "events": self.events,
            "iteration": self.iteration,
        }
        return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        visible_content_handler: VisibleContentSink | None = None,
    ) -> "AgentRunState":
        """@brief 从持久化快照恢复状态 / Restore state from a persisted snapshot.

        @param payload ``to_dict`` 生成的状态字典 / State dictionary from ``to_dict``.
        @param visible_content_handler 恢复后重新注入的输出端口 / Sink reinjected after restore.
        @return 可继续执行的 Agent 状态 / Agent state ready to resume.
        @raises ValueError 快照结构不完整时抛出 / Raised for incomplete snapshots.
        """
        request_data = payload.get("request")
        messages = payload.get("messages")
        events = payload.get("events")
        if not isinstance(request_data, Mapping) or not isinstance(messages, list) or not isinstance(events, list):
            raise ValueError("Invalid AgentRunState snapshot")
        try:
            request = AgentRunRequest(
                provider=str(request_data["provider"]),
                model=str(request_data["model"]),
                messages=messages,
                provider_name=str(request_data.get("provider_name", "AI")),
                tool_choice=request_data.get("tool_choice", "auto"),
                max_tokens=int(request_data.get("max_tokens", 4096)),
                max_iterations=int(request_data.get("max_iterations", 10)),
                skip_tools=frozenset(str(name) for name in request_data.get("skip_tools", ())),
                completion_kwargs=request_data.get("completion_kwargs"),
                visible_content_handler=visible_content_handler,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid AgentRunState snapshot") from exc
        return cls(request=request, messages=messages, events=events, iteration=int(payload.get("iteration", 0)))


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

    def run(self, request: AgentRunRequest, *, state: AgentRunState | None = None) -> AgentResponse:
        """@brief 驱动一个 Agent 回合 / Drive an Agent turn.

        @param request 一次回合的不可变输入 / Immutable input for the turn.
        @param state 可选的可恢复执行状态 / Optional resumable execution state.
        @return 文本回复和 Runtime 事件 / Text reply and Runtime events.
        """
        state = state or AgentRunState.from_request(request)
        if state.request.provider != request.provider or state.request.model != request.model:
            raise ValueError("AgentRunState does not belong to this AgentRunRequest")
        state.request = request

        for iteration in range(state.iteration, request.max_iterations):
            state.iteration = iteration + 1
            response = self._request_with_partial_events(
                request=request,
                state=state,
                tools=self._runtime.tool_definitions,
                tool_choice=request.tool_choice,
            )
            assistant_message = response.choices[0].message
            raw_tool_calls = getattr(assistant_message, "tool_calls", None)
            assistant_content = assistant_message.content or ""
            if not raw_tool_calls:
                logging.info("%s 第 %s 轮：无工具调用，直接返回答案", request.provider_name, iteration + 1)
                return self._final_response(request=request, state=state, content_text=assistant_content)

            tool_calls = normalise_tool_calls(raw_tool_calls)
            logging.info("%s 第 %s 轮：检测到 %s 个工具调用", request.provider_name, iteration + 1, len(tool_calls))
            assistant_content_for_model, completed = self._emit_intermediate_content(
                content=assistant_content,
                request=request,
                state=state,
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
                request=request,
                state=state,
            )

        logging.warning("%s 工具调用次数超限（%s轮）", request.provider_name, request.max_iterations)
        response = self._request_with_partial_events(
            request=request,
            state=state,
        )
        assistant_message = response.choices[0].message
        if getattr(assistant_message, "tool_calls", None):
            logging.warning("%s 工具调用超限后的最终回复仍包含工具调用，忽略工具调用。", request.provider_name)
        return self._final_response(request=request, state=state, content_text=assistant_message.content or "")

    def _request_with_partial_events(
        self,
        *,
        request: AgentRunRequest,
        state: AgentRunState,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
    ) -> Any:
        """@brief 请求模型并保留部分事件 / Request a model while preserving partial events.

        @param request Agent 回合输入 / Agent turn input.
        @param state 当前可恢复状态 / Current resumable state.
        @param tools 可选工具定义 / Optional tool definitions.
        @param tool_choice 可选工具选择策略 / Optional tool-choice policy.
        @return provider 响应 / Provider response.
        """
        request_kwargs: dict[str, Any] = {
            "messages": state.messages,
            "max_tokens": request.max_tokens,
            **(request.completion_kwargs or {}),
        }
        if tools is not None:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = tool_choice
        try:
            return self._completion_client(request.provider, request.model, **request_kwargs)
        except Exception as exc:
            if state.events:
                raise PartialAgentResponseError(str(exc), state.events) from exc
            raise

    def _emit_intermediate_content(
        self,
        *,
        content: str,
        request: AgentRunRequest,
        state: AgentRunState,
    ) -> tuple[str, bool]:
        """@brief 投递带工具调用的中间文本 / Emit intermediate text accompanying tool calls.

        @param content assistant 中间文本 / Assistant intermediate text.
        @param request Agent 回合输入 / Agent turn input.
        @param state 当前可恢复状态 / Current resumable state.
        @return 回填模型的文本和是否完成 / Model-feedback text and completion flag.
        """
        if request.visible_content_handler is None or not content.strip():
            return content, True
        visible_result = emit_visible_content(
            request.visible_content_handler,
            content,
            provider_name=request.provider_name,
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
        request: AgentRunRequest,
        state: AgentRunState,
    ) -> None:
        """@brief 提交并消费本轮工具调用 / Submit and consume this turn's tool calls.

        @param tool_calls 已归一化工具调用 / Normalized tool calls.
        @param assistant_message 要持久化的 assistant 调用消息 / Assistant call message to persist.
        @param request Agent 回合输入 / Agent turn input.
        @param state 当前可恢复状态 / Current resumable state.
        """
        assistant_message_logged = False
        for tool_call in tool_calls:
            function_payload = tool_call.get("function") or {}
            function_name = function_payload.get("name")
            if not function_name:
                logging.warning("%s 返回的工具调用缺少函数名: %s", request.provider_name, tool_call)
                continue
            if function_name in request.skip_tools:
                continue
            task_result = self._execute_tool_task(
                tool_name=function_name,
                raw_arguments=function_payload.get("arguments"),
                invocation_id=tool_call.get("id"),
                request=request,
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
        request: AgentRunRequest,
    ) -> Any:
        """@brief 在 Runtime 中执行一个能力任务 / Execute one capability task in the Runtime.

        @param tool_name 能力名称 / Capability name.
        @param raw_arguments provider 原始参数 / Raw provider arguments.
        @param invocation_id provider 调用标识 / Provider invocation identifier.
        @param request Agent 回合输入 / Agent turn input.
        @return Runtime 任务结果 / Runtime task result.
        """
        handle = self._runtime.submit(
            ToolTask(
                name=tool_name,
                arguments=self._parse_tool_arguments(raw_arguments, provider_name=request.provider_name),
                invocation_id=invocation_id,
                producer_name=request.provider_name,
            )
        )
        return self._runtime.consume(handle, visible_content_handler=request.visible_content_handler)

    @staticmethod
    def _append_task_events(
        *,
        task_result: Any,
        assistant_message: dict[str, Any],
        assistant_message_logged: bool,
        state: AgentRunState,
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
        request: AgentRunRequest,
        state: AgentRunState,
        content_text: str,
    ) -> AgentResponse:
        """@brief 处理 Agent 最终文本 / Handle final Agent text.

        @param request Agent 回合输入 / Agent turn input.
        @param state 当前可恢复状态 / Current resumable state.
        @param content_text Agent 最终文本 / Final Agent text.
        @return 回复文本和事件 / Reply text and events.
        """
        if content_text.strip():
            if request.visible_content_handler:
                visible_result = emit_visible_content(
                    request.visible_content_handler,
                    content_text,
                    provider_name=request.provider_name,
                )
                if visible_result.content:
                    state.events.append({"type": "assistant_visible", "content": visible_result.content})
                    return AgentResponse("", state.events)
                if not visible_result.completed:
                    return AgentResponse("", state.events)
            return AgentResponse(content_text, state.events)
        if state.events:
            logging.warning("%s 工具调用后最终回复为空。", request.provider_name)
        return AgentResponse(content_text, state.events)


DEFAULT_AGENT_LOOP = AgentLoop(
    runtime=DEFAULT_AGENT_RUNTIME,
    completion_client=create_chat_completion,
)
"""@brief 进程共享 Agent Loop / Process-shared Agent Loop."""
