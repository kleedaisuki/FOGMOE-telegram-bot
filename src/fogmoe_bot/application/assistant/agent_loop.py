"""@brief 可恢复的异步 Agent 状态机 / Resumable asynchronous Agent state machine.

每个 provider response 在执行其 tool calls 前 checkpoint。重启后相同 Turn 从 checkpoint
恢复，再由 effect receipt 重放每个工具结果，因此不会重新规划已发生的 mutation。/
Every provider response is checkpointed before its tool calls execute. After restart the same Turn
resumes from that checkpoint and replays every result through effect receipts, so already-applied
mutations are never replanned.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from fogmoe_bot.application.memory.ports import (
    WorkingMemoryQuery,
    WorkingMemoryReader,
)
from fogmoe_bot.application.memory.rendering import compose_model_messages
from fogmoe_bot.domain.memory.models import (
    MAX_WORKING_MEMORY_MESSAGES,
    GroupMemoryScope,
    PersonalMemoryScope,
)
from fogmoe_bot.domain.context import ContextState
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)

from .completion import (
    AgentCheckpointConflictError,
    AgentCheckpointPersistence,
    AgentStepCheckpoint,
    AssistantCompletion,
    AssistantCompletionPort,
)
from .errors import ResumableAgentInterruptedError
from .tool_runtime import (
    AgentRuntime,
    AssistantToolCallEvent,
    RuntimeEvent,
    ToolExecutionContext,
    ToolResultEvent,
    ToolRuntimeResult,
)
from .tools.catalog import ToolResultResidency
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """@brief Agent 回合输出 / Agent-turn output.

    @param text 最终文本 / Final text.
    @param events receipt-backed 事件 / Receipt-backed events.
    @param context_state 已更新 attempt-local 上下文 / Updated attempt-local context.
    @param history_messages 可进入未来 Conversation 的新增消息 / New messages allowed into future Conversation context.
    """

    text: str
    events: Sequence[RuntimeEvent]
    context_state: ContextState | None = None
    history_messages: Sequence[JsonObject] = ()


@dataclass(frozen=True, slots=True)
class AgentExecutionConfig:
    """@brief Agent 状态机配置 / Agent-state-machine configuration."""

    provider: str
    model: str
    provider_name: str = "AI"
    tool_choice: str | JsonObject | None = "auto"
    max_tokens: int = 4096
    max_iterations: int = 10
    skip_tools: frozenset[str] = field(default_factory=frozenset)
    allow_tools: bool = True
    completion_options: Mapping[str, JsonValue] = field(default_factory=dict)
    working_memory_limit: int = 64
    working_memory_max_tokens: int = 16_384
    working_memory_enabled: bool = True

    def __post_init__(self) -> None:
        """@brief 校验显式容量 / Validate explicit bounds.

        @return None / None.
        """

        if not self.provider.strip() or not self.model.strip():
            raise ValueError("provider and model cannot be empty")
        if self.max_tokens < 1 or self.max_iterations < 1:
            raise ValueError("max_tokens and max_iterations must be positive")
        if not 1 <= self.working_memory_limit <= MAX_WORKING_MEMORY_MESSAGES:
            raise ValueError(
                "working_memory_limit must be between 1 and "
                f"{MAX_WORKING_MEMORY_MESSAGES}"
            )
        if self.working_memory_max_tokens < 256:
            raise ValueError("working_memory_max_tokens must be at least 256")


@dataclass(slots=True)
class AgentExecutionState:
    """@brief 单 attempt 的可重建执行状态 / Rebuildable execution state for one attempt."""

    context: ContextState
    config: AgentExecutionConfig
    base_messages: tuple[JsonObject, ...]
    messages: list[JsonObject]
    persistable_messages: list[JsonObject] = field(default_factory=list)
    events: list[RuntimeEvent] = field(default_factory=list)
    step: int = 0

    @classmethod
    def from_context(
        cls,
        context: ContextState,
        config: AgentExecutionConfig,
    ) -> AgentExecutionState:
        """@brief 从规范上下文建立 attempt 状态 / Build attempt state from canonical context.

        @param context attempt-local 上下文 / Attempt-local context.
        @param config 配置 / Configuration.
        @return 新状态 / New state.
        """

        messages = tuple(_message(value) for value in context.messages)
        return cls(
            context=context,
            config=config,
            base_messages=messages,
            messages=list(messages),
        )


class AgentLoop:
    """@brief Provider completion 与 durable tools 的异步状态机 / Async state machine for provider completion and durable tools."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        completion: AssistantCompletionPort,
        checkpoints: AgentCheckpointPersistence,
        memory: WorkingMemoryReader,
        telemetry: Telemetry,
    ) -> None:
        """@brief 注入全部外部端口 / Inject every external port.

        @param runtime 无状态工具协调器 / Stateless tool coordinator.
        @param completion 异步 provider port / Async provider port.
        @param checkpoints durable step store / Durable step store.
        @param memory 每次模型 Query fresh retrieve 的 WorkingMemory / WorkingMemory freshly retrieved for each model query.
        @param telemetry 进程 typed telemetry / Process typed telemetry.
        @return None / None.
        """

        self._runtime = runtime
        self._completion = completion
        self._checkpoints = checkpoints
        self._memory = memory
        self._telemetry = telemetry

    async def run(
        self,
        context: ContextState,
        config: AgentExecutionConfig,
        *,
        tool_context: ToolExecutionContext | None = None,
        state: AgentExecutionState | None = None,
    ) -> AgentResponse:
        """@brief 运行或恢复一个 Agent Turn / Run or resume one Agent Turn.

        @param context attempt-local 规范上下文 / Attempt-local canonical context.
        @param config route 配置 / Route configuration.
        @param tool_context durable 工具身份；禁用工具时可省略 / Durable tool identity; optional when tools are disabled.
        @param state 测试用可选状态 / Optional state for tests.
        @return 最终响应 / Final response.
        """

        current = state or AgentExecutionState.from_context(context, config)
        if current.context is not context or current.config != config:
            raise ValueError("AgentExecutionState belongs to another context/config")
        if config.allow_tools and tool_context is None:
            raise ValueError("tool_context is required when tools are enabled")
        if config.working_memory_enabled and tool_context is None:
            raise ValueError("tool_context is required when WorkingMemory is enabled")
        while current.step < config.max_iterations:
            completion = await self._complete_step(
                current, tool_context=tool_context, expose_tools=config.allow_tools
            )
            if not completion.tool_calls:
                return _final_response(current, completion)
            if not config.allow_tools:
                raise ValueError(
                    "provider returned tool calls while tools were disabled"
                )
            current.messages.append(dict(completion.message))
            await self._execute_calls(
                current,
                completion=completion,
                tool_context=cast(ToolExecutionContext, tool_context),
            )
            current.step += 1

        completion = await self._complete_step(
            current, tool_context=tool_context, expose_tools=False
        )
        return _final_response(current, completion)

    async def _complete_step(
        self,
        state: AgentExecutionState,
        *,
        tool_context: ToolExecutionContext | None,
        expose_tools: bool,
    ) -> AssistantCompletion:
        """@brief 读取 checkpoint 或先调用 provider 再保存 / Load a checkpoint or call and then persist the provider.

        @param state 当前状态 / Current state.
        @param tool_context durable identity / Durable identity.
        @param expose_tools 是否暴露目录 / Whether to expose the catalog.
        @return 规范完成 / Canonical completion.
        """

        route_key = f"{state.config.provider}:{state.config.model}"
        request_hash = _completion_request_hash(state, expose_tools=expose_tools)
        if tool_context is not None:
            existing = await self._checkpoints.load_step(
                tool_context.turn_id, state.step
            )
            if existing is not None:
                _validate_checkpoint(
                    existing, request_hash=request_hash, route_key=route_key
                )
                return existing.completion
        model_messages: tuple[JsonObject, ...] = tuple(state.messages)
        if state.config.working_memory_enabled:
            memory_context = cast(ToolExecutionContext, tool_context)
            with self._telemetry.span(
                "memory.working.retrieve",
                kind=SpanKind.INTERNAL,
                attributes={
                    "memory.scope.kind": (
                        "group" if memory_context.is_group else "personal"
                    ),
                    "memory.result.limit": state.config.working_memory_limit,
                },
            ) as memory_span:
                working_memory = await self._memory.retrieve(
                    WorkingMemoryQuery(
                        scope=_memory_scope(memory_context),
                        text=_current_query(state.context),
                        limit=state.config.working_memory_limit,
                    )
                )
                memory_span.set_attribute(
                    "memory.result.count", len(working_memory.messages)
                )
            model_messages = compose_model_messages(
                state.messages,
                working_memory,
                maximum_tokens=state.config.working_memory_max_tokens,
            )
        definitions = (
            tuple(
                definition
                for definition in self._runtime.tool_definitions
                if definition.name not in state.config.skip_tools
            )
            if expose_tools
            else ()
        )
        try:
            completion = await self._completion.complete(
                provider=state.config.provider,
                model=state.config.model,
                messages=model_messages,
                tools=definitions,
                tool_choice=(state.config.tool_choice if expose_tools else None),
                max_tokens=state.config.max_tokens,
                request_options=state.config.completion_options,
            )
        except Exception as error:
            if state.step > 0 or state.events:
                raise ResumableAgentInterruptedError(
                    str(error) or error.__class__.__name__
                ) from error
            raise
        if tool_context is None:
            return completion
        checkpoint = AgentStepCheckpoint(
            turn_id=tool_context.turn_id,
            step_no=state.step,
            request_hash=request_hash,
            route_key=route_key,
            completion=completion,
        )
        canonical = await self._checkpoints.save_step(checkpoint)
        _validate_checkpoint(canonical, request_hash=request_hash, route_key=route_key)
        return canonical.completion

    async def _execute_calls(
        self,
        state: AgentExecutionState,
        *,
        completion: AssistantCompletion,
        tool_context: ToolExecutionContext,
    ) -> None:
        """@brief 顺序执行一个 checkpoint 中的工具调用 / Sequentially execute calls from one checkpoint.

        @param state 当前状态 / Current state.
        @param completion 已持久化完成 / Persisted completion.
        @param tool_context durable identity / Durable identity.
        @return None / None.
        """

        results: list[ToolRuntimeResult] = []
        for ordinal, call in enumerate(completion.tool_calls):
            if call.name in state.config.skip_tools:
                continue
            with self._telemetry.span(
                "agent.tool.execute",
                kind=SpanKind.INTERNAL,
                attributes={
                    "fogmoe.turn.id": str(tool_context.turn_id),
                    "gen_ai.tool.name": call.name,
                    "gen_ai.tool.step": state.step,
                    "gen_ai.tool.ordinal": ordinal,
                },
            ) as span:
                try:
                    result = await self._runtime.execute(
                        context=tool_context,
                        step=state.step,
                        ordinal=ordinal,
                        provider_call_id=call.provider_call_id,
                        tool_name=call.name,
                        raw_arguments=_parse_arguments(call.arguments),
                    )
                except Exception:
                    self._telemetry.counter(
                        MetricName.TOOL_OUTCOMES,
                        attributes={
                            "outcome": Outcome.FAILURE,
                            "gen_ai.tool.name": call.name,
                        },
                    )
                    raise
                span.set_attribute("fogmoe.tool.replayed", result.replayed)
                self._telemetry.counter(
                    MetricName.TOOL_OUTCOMES,
                    attributes={
                        "outcome": Outcome.SUCCESS,
                        "gen_ai.tool.name": call.name,
                        "fogmoe.tool.replayed": result.replayed,
                    },
                )
            self._append_call(
                state, completion=completion, result=result, first=ordinal == 0
            )
            results.append(result)
        _append_persistable_tool_exchange(
            state,
            completion=completion,
            results=tuple(results),
        )

    @staticmethod
    def _append_call(
        state: AgentExecutionState,
        *,
        completion: AssistantCompletion,
        result: ToolRuntimeResult,
        first: bool,
    ) -> None:
        """@brief 追加事件与 provider tool message / Append events and a provider tool message.

        @param state 当前状态 / Current state.
        @param completion 调用来源消息 / Source message.
        @param result receipt 结果 / Receipt result.
        @param first 是否本消息第一调用 / Whether this is the first call in the message.
        @return None / None.
        """

        call_event: AssistantToolCallEvent = {
            "type": "assistant_tool_call",
            "tool_name": result.name,
            "arguments": cast(JsonValue, result.arguments),
            "tool_call_id": result.provider_call_id,
            "invocation_id": result.invocation_id,
        }
        if first:
            call_event["assistant_message"] = dict(completion.message)
        if result.validation_error is not None:
            call_event["validation_error"] = result.validation_error
        ephemeral = result.result_residency is ToolResultResidency.AGENT_TURN
        if ephemeral:
            call_event["ephemeral"] = True
        state.events.append(call_event)
        state.messages.append(
            {
                "role": "tool",
                "tool_call_id": result.provider_call_id,
                "name": result.name,
                "content": json.dumps(
                    result.public_result, ensure_ascii=False, separators=(",", ":")
                ),
            }
        )
        result_event: ToolResultEvent = {
            "type": "tool_result",
            "tool_name": result.name,
            "arguments": result.arguments,
            "result": result.public_result,
            "tool_call_id": result.provider_call_id,
            "invocation_id": result.invocation_id,
            "effect_kind": result.effect_kind,
            "replayed": result.replayed,
        }
        if ephemeral:
            result_event["ephemeral"] = True
        state.events.append(result_event)


def _completion_request_hash(state: AgentExecutionState, *, expose_tools: bool) -> str:
    """@brief 摘要不含瞬时 WorkingMemory 的稳定模型 step / Digest a stable model step excluding ephemeral WorkingMemory.

    @param state 当前状态 / Current state.
    @param expose_tools 是否暴露工具 / Whether tools are exposed.
    @return SHA-256 / SHA-256.
    """

    payload = {
        "messages": state.messages,
        "provider": state.config.provider,
        "model": state.config.model,
        "max_tokens": state.config.max_tokens,
        "tool_choice": state.config.tool_choice if expose_tools else None,
        "expose_tools": expose_tools,
        "skip_tools": sorted(state.config.skip_tools),
        "options": dict(state.config.completion_options),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_checkpoint(
    checkpoint: AgentStepCheckpoint,
    *,
    request_hash: str,
    route_key: str,
) -> None:
    """@brief 拒绝 checkpoint identity drift / Reject checkpoint identity drift.

    @param checkpoint 规范 checkpoint / Canonical checkpoint.
    @param request_hash 期望输入摘要 / Expected input digest.
    @param route_key 期望 route / Expected route.
    @return None / None.
    """

    if checkpoint.request_hash != request_hash or checkpoint.route_key != route_key:
        raise AgentCheckpointConflictError(
            f"Agent checkpoint conflict at step {checkpoint.step_no}"
        )


def _parse_arguments(value: JsonValue) -> object:
    """@brief 解码 provider arguments / Decode provider arguments.

    @param value JSON 字符串或树 / JSON string or tree.
    @return 参数对象 / Argument object.
    """

    if isinstance(value, str):
        try:
            decoded: object = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return decoded
    return value


def _message(value: Mapping[str, object]) -> JsonObject:
    """@brief 校验 ContextState message 为 JSON / Validate a ContextState message as JSON.

    @param value 原始消息 / Raw message.
    @return 独立 JSON 对象 / Independent JSON object.
    """

    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("Assistant message must be a JSON object")
    return cast(JsonObject, decoded)


def _final_response(
    state: AgentExecutionState, completion: AssistantCompletion
) -> AgentResponse:
    """@brief 提交最终 Assistant message / Commit the final Assistant message.

    @param state 当前状态 / Current state.
    @param completion 无 tool calls 的完成 / Completion without tool calls.
    @return Agent response / Agent response.
    """

    final_message = dict(completion.message)
    state.messages.append(final_message)
    state.persistable_messages.append(final_message)
    context_messages = [
        *state.base_messages,
        *state.persistable_messages,
    ]
    state.context.messages = cast(list[dict[str, object]], context_messages)
    return AgentResponse(
        completion.content,
        tuple(state.events),
        state.context,
        tuple(state.persistable_messages),
    )


def _memory_scope(
    context: ToolExecutionContext,
) -> PersonalMemoryScope | GroupMemoryScope:
    """@brief 从可信工具上下文派生唯一 Memory 域 / Derive the sole Memory scope from trusted tool context.

    @param context durable 授权上下文 / Durable authorization context.
    @return 个人或当前群聊域 / Personal or current-group scope.
    @raise ValueError 群聊上下文缺少 group_id / Group context lacks a group identifier.
    """

    if not context.is_group:
        return PersonalMemoryScope(context.user_id)
    if context.group_id is None:
        raise ValueError("Group Memory requires group_id")
    return GroupMemoryScope(context.group_id)


def _current_query(context: ContextState) -> str:
    """@brief 原样提取当前用户 Query，不做 rewrite / Extract the current user query verbatim without rewriting.

    @param context 当前 ContextState / Current ContextState.
    @return 原始文本 Query / Raw text query.
    @raise ValueError ContextState 不含可嵌入用户文本 / ContextState has no embeddable user text.
    """

    if context.current_user_text is not None:
        return context.current_user_text.strip()
    for message in reversed(context.messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(part.get("text")).strip()
                for part in content
                if isinstance(part, Mapping)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
                and str(part.get("text")).strip()
            ]
            if parts:
                return "\n".join(parts)
    raise ValueError("ContextState has no current user query for WorkingMemory")


def _append_persistable_tool_exchange(
    state: AgentExecutionState,
    *,
    completion: AssistantCompletion,
    results: tuple[ToolRuntimeResult, ...],
) -> None:
    """@brief 仅保留 Conversation 驻留的工具交换 / Retain only conversation-resident tool exchanges.

    @param state 当前执行状态 / Current execution state.
    @param completion 原始 Assistant tool-call 消息 / Original Assistant tool-call message.
    @param results 已执行结果 / Executed results.
    @return None / None.
    """

    persistent = tuple(
        result
        for result in results
        if result.result_residency is ToolResultResidency.CONVERSATION
    )
    persistent_ids = {result.provider_call_id for result in persistent}
    message = dict(completion.message)
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        message["tool_calls"] = [
            dict(call)
            for call in calls
            if isinstance(call, Mapping) and call.get("id") in persistent_ids
        ]
    content = message.get("content")
    has_content = isinstance(content, str) and bool(content.strip())
    if persistent or has_content:
        state.persistable_messages.append(message)
    for result in persistent:
        state.persistable_messages.append(
            {
                "role": "tool",
                "tool_call_id": result.provider_call_id,
                "name": result.name,
                "content": json.dumps(
                    result.public_result,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )


__all__ = [
    "AgentExecutionConfig",
    "AgentExecutionState",
    "AgentLoop",
    "AgentResponse",
]
