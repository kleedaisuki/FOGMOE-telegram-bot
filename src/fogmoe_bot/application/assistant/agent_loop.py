"""@brief 可恢复的异步 Agent 状态机 / Resumable asynchronous Agent state machine.

每一个 provider response 都会在工具副作用之前形成 checkpoint。重启后同一 Turn 读取
该 checkpoint，再通过 effect receipt 重放工具结果，因此不会重规划已经发生的 mutation。
本模块只使用 canonical message V2；OpenAI 或 Anthropic wire JSON 不会穿过这里。/
Every provider response is checkpointed before tool effects. On restart, the same Turn reads the
checkpoint and replays each tool result through effect receipts, so already-applied mutations are
never replanned. This module uses only canonical message V2; OpenAI or Anthropic wire JSON never
crosses this boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from fogmoe_bot.application.memory.ports import (
    WorkingMemoryQuery,
    WorkingMemoryReader,
)
from fogmoe_bot.application.memory.rendering import compose_model_messages
from fogmoe_bot.application.observability.telemetry import SpanScope, Telemetry
from fogmoe_bot.application.workspace.errors import WorkspaceRuntimeUnavailableError
from fogmoe_bot.domain.assistant.messages import (
    CanonicalMessage,
    FrozenJsonValue,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from fogmoe_bot.domain.assistant.request_metadata import (
    RequestMeta,
    normalize_request_meta,
    request_meta_to_json,
)
from fogmoe_bot.domain.assistant.routing.models import (
    PromptCachePolicy,
    PromptCacheRetention,
    ProviderRoute,
)
from fogmoe_bot.domain.context import ContextState
from fogmoe_bot.domain.conversation.errors import StaleClaimError
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.memory.models import (
    MAX_WORKING_MEMORY_MESSAGES,
    GroupMemoryScope,
    PersonalMemoryScope,
)
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind

from .completion import (
    AgentCheckpointConflictError,
    AgentCheckpointPersistence,
    AgentStepCheckpoint,
    AssistantCompletion,
    AssistantCompletionStreamEvent,
    AssistantCompletionPort,
    AssistantStreamingCompletionPort,
    CompletionFinished,
    CompletionTextDelta,
    InferenceGenerationFencePort,
    PromptCacheDirective,
    PromptCacheKey,
)
from .errors import (
    ResumableAgentInterruptedError,
    is_deterministic_agent_failure,
)
from .progress import (
    AssistantProgressPersistence,
    commentary_progress_item,
    tool_progress_item,
    tool_started_progress_item,
)
from .streaming import AssistantStreamSession
from .tool_runtime import (
    AgentRuntime,
    AssistantToolCallEvent,
    RuntimeEvent,
    ToolExecutionContext,
    ToolResultEvent,
    ToolRuntimeResult,
    tool_invocation_id,
)
from .tools.catalog import ToolDefinition, ToolResultResidency

_STREAM_GENERATION_FENCE_INTERVAL_SECONDS = 0.2
"""@brief 流中代际检查的最大间隔 / Maximum interval between in-stream generation-fence checks."""


async def _close_completion_stream(events: object) -> None:
    """@brief 立即关闭支持 aclose 的 provider 流 / Promptly close a provider stream that supports aclose.

    @param events provider 返回的异步 iterator / Async iterator returned by the provider.
    @return None / None.
    @note 关闭失败只影响资源回收，不能覆盖原始 generation/protocol 异常。/
        A close failure affects resource cleanup only and must not mask the original
        generation or protocol error.
    """

    close = getattr(events, "aclose", None)
    if not callable(close):
        return
    try:
        await cast(Awaitable[object], close())
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.warning("Provider completion stream close failed", exc_info=True)


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """@brief Agent 回合输出 / Agent-turn output.

    @param text 最终文本 / Final text.
    @param events receipt-backed 事件 / Receipt-backed events.
    @param context_state 已更新 attempt-local 上下文 / Updated attempt-local context.
    @param history_messages 可进入未来 Conversation 的新增 canonical 消息 /
        New canonical messages allowed into future Conversation context.
    """

    text: str
    events: Sequence[RuntimeEvent]
    context_state: ContextState | None = None
    history_messages: Sequence[CanonicalMessage] = ()


@dataclass(frozen=True, slots=True)
class AgentExecutionConfig:
    """@brief Agent 状态机的单次 route/model 配置 / One route/model configuration for the Agent state machine.

    @param route 自包含的 provider route / Self-contained provider route.
    @param model route 内选中的模型 / Model selected inside the route.
    @param tool_choice provider-neutral 工具选择 / Provider-neutral tool choice.
    @param max_tokens 输出 token 上限 / Output-token limit.
    @param max_iterations 允许的有工具模型 step 数 / Number of tool-enabled model steps allowed.
    @param skip_tools 本 route 禁用的目录工具 / Catalog tools disabled by this route.
    @param allow_tools 本 Turn 是否暴露工具 / Whether tools are exposed in this Turn.
    @param timeout_seconds 单次 completion 总 deadline / Total deadline for one completion.
    @param request_meta 调用方明确附加的 metadata / Explicit caller metadata.
    @param working_memory_limit 每次检索的消息数上限 / Max retrieved messages per query.
    @param working_memory_max_tokens 注入 Memory token 上限 / Working-memory injection token ceiling.
    @param working_memory_enabled 是否注入 WorkingMemory / Whether WorkingMemory is injected.
    @param prompt_cache_policy 当前模型经 route 门控的缓存策略 /
        Cache policy gated by the selected route model.
    @param prompt_cache_retention 显式缓存保留期 / Explicit-cache retention.
    """

    route: ProviderRoute
    model: str
    tool_choice: str | JsonObject | None = "auto"
    max_tokens: int = 4096
    max_iterations: int = 10
    skip_tools: frozenset[str] = field(default_factory=frozenset)
    allow_tools: bool = True
    timeout_seconds: float | None = None
    request_meta: RequestMeta = field(
        default_factory=lambda: normalize_request_meta({})
    )
    working_memory_limit: int = 64
    working_memory_max_tokens: int = 16_384
    working_memory_enabled: bool = True
    prompt_cache_policy: PromptCachePolicy = "automatic"
    prompt_cache_retention: PromptCacheRetention | None = None

    def __post_init__(self) -> None:
        """@brief 校验显式容量、route 与请求边界 / Validate explicit bounds, route, and request boundary.

        @return None / None.
        """

        if not isinstance(self.route, ProviderRoute):
            raise TypeError("route must be ProviderRoute")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model cannot be empty")
        if self.max_tokens < 1 or self.max_iterations < 1:
            raise ValueError("max_tokens and max_iterations must be positive")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0.0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if not 1 <= self.working_memory_limit <= MAX_WORKING_MEMORY_MESSAGES:
            raise ValueError(
                "working_memory_limit must be between 1 and "
                f"{MAX_WORKING_MEMORY_MESSAGES}"
            )
        if self.working_memory_max_tokens < 256:
            raise ValueError("working_memory_max_tokens must be at least 256")
        if (
            self.prompt_cache_policy == "explicit"
            and self.prompt_cache_retention is None
        ):
            raise ValueError(
                "explicit prompt_cache_policy requires prompt_cache_retention"
            )
        if (
            self.prompt_cache_policy != "explicit"
            and self.prompt_cache_retention is not None
        ):
            raise ValueError(
                "prompt_cache_retention requires explicit prompt_cache_policy"
            )
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(
            self, "request_meta", normalize_request_meta(self.request_meta)
        )


class AgentExecutionState:
    """@brief 单 attempt 的封闭过程管理器 / Closed process manager for one attempt.

    transient 模型历史、可持久化历史、runtime events 与 provider step 必须一起推进；
    它们不是可由 orchestration 随意改写的独立列表。/ Transient model history,
    persistable history, runtime events, and the provider step advance together; they are not
    independent lists for orchestration code to mutate arbitrarily.
    """

    __slots__ = (
        "_base_messages",
        "_config",
        "_context",
        "_events",
        "_messages",
        "_persistable_messages",
        "_step",
    )

    def __init__(
        self,
        *,
        context: ContextState,
        config: AgentExecutionConfig,
        base_messages: tuple[CanonicalMessage, ...],
    ) -> None:
        """@brief 建立初始 attempt 状态 / Establish initial attempt state.

        @param context attempt-local 上下文 / Attempt-local context.
        @param config 已验证 route/model 配置 / Validated route/model configuration.
        @param base_messages attempt 起始历史 / History at attempt start.
        @return None / None.
        @note 调用方应使用 ``from_context``，避免分别派生 context 与 base history。/
            Callers should use ``from_context`` so context and base history cannot be derived
            separately.
        """

        if not isinstance(context, ContextState):
            raise TypeError("context must be a ContextState")
        if not isinstance(config, AgentExecutionConfig):
            raise TypeError("config must be an AgentExecutionConfig")
        if base_messages != context.messages:
            raise ValueError("base_messages must be the current ContextState history")
        self._context = context
        self._config = config
        self._base_messages = base_messages
        self._messages = list(base_messages)
        self._persistable_messages: list[CanonicalMessage] = []
        self._events: list[RuntimeEvent] = []
        self._step = 0

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

        return cls(
            context=context,
            config=config,
            base_messages=context.messages,
        )

    @property
    def context(self) -> ContextState:
        """@brief 返回 attempt-local 上下文 / Return the attempt-local context.

        @return 由本过程管理器拥有的 ContextState / ContextState owned by this process manager.
        """

        return self._context

    @property
    def config(self) -> AgentExecutionConfig:
        """@brief 返回固定 route/model 配置 / Return the fixed route/model configuration.

        @return 本 attempt 配置 / Configuration for this attempt.
        """

        return self._config

    @property
    def messages(self) -> tuple[CanonicalMessage, ...]:
        """@brief 返回当前 transient 模型历史 / Return current transient model history.

        @return 不可变消息快照 / Immutable message snapshot.
        """

        return tuple(self._messages)

    @property
    def events(self) -> tuple[RuntimeEvent, ...]:
        """@brief 返回已记录 runtime events / Return recorded runtime events.

        @return 不可变事件快照 / Immutable event snapshot.
        """

        return tuple(self._events)

    @property
    def step(self) -> int:
        """@brief 返回当前 provider step / Return the current provider step.

        @return 从零开始的 step 序号 / Zero-based step ordinal.
        """

        return self._step

    def record_tool_plan(self, completion: AssistantCompletion) -> None:
        """@brief 记录将被执行的 Assistant 工具计划 / Record an Assistant tool plan to execute.

        @param completion 已 checkpoint 且包含工具调用的 completion /
            Checkpointed completion containing tool calls.
        @return None / None.
        """

        if not completion.tool_calls:
            raise ValueError("tool plan must contain at least one tool call")
        self._messages.append(completion.message)

    def record_tool_result(
        self,
        *,
        completion: AssistantCompletion,
        result: ToolRuntimeResult,
        first: bool,
    ) -> None:
        """@brief 原子记录一个 receipt-backed 工具交换 / Atomically record one receipt-backed tool exchange.

        @param completion 调用来源消息 / Source completion.
        @param result durable tool receipt 的规范结果 / Canonical result of the durable tool receipt.
        @param first 是否为来源消息的首个调用 / Whether this is the source message's first call.
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
            call_event["assistant_message"] = completion.message.to_json()
        if result.validation_error is not None:
            call_event["validation_error"] = result.validation_error
        ephemeral = result.result_residency is ToolResultResidency.AGENT_TURN
        is_error = not _tool_result_succeeded(result)
        if ephemeral:
            call_event["ephemeral"] = True
        self._events.append(call_event)
        self._messages.append(
            CanonicalMessage(
                MessageRole.TOOL,
                (
                    ToolResultPart(
                        result.provider_call_id,
                        result.name,
                        cast(FrozenJsonValue, result.public_result),
                        is_error=is_error,
                    ),
                ),
            )
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
            "is_error": is_error,
        }
        if ephemeral:
            result_event["ephemeral"] = True
        self._events.append(result_event)

    def retain_conversation_tool_exchange(
        self,
        *,
        completion: AssistantCompletion,
        results: tuple[ToolRuntimeResult, ...],
    ) -> None:
        """@brief 保留仅可进入未来 Conversation 的工具交换 / Retain only tool exchanges eligible for future Conversation history.

        @param completion 原始 Assistant 工具计划 / Original Assistant tool plan.
        @param results 按调用顺序排列的 receipt 结果 / Receipt results in call order.
        @return None / None.
        """

        persistent = tuple(
            result
            for result in results
            if result.result_residency is ToolResultResidency.CONVERSATION
        )
        persistent_ids = {result.provider_call_id for result in persistent}
        retained_parts = tuple(
            part
            for part in completion.message.parts
            if (
                (isinstance(part, TextPart) and part.text.strip())
                or (isinstance(part, ToolCallPart) and part.call_id in persistent_ids)
            )
        )
        if retained_parts:
            self._persistable_messages.append(
                CanonicalMessage(
                    MessageRole.ASSISTANT,
                    retained_parts,
                    completion.message.policy,
                    completion.message.meta,
                )
            )
        for result in persistent:
            self._persistable_messages.append(
                CanonicalMessage(
                    MessageRole.TOOL,
                    (
                        ToolResultPart(
                            result.provider_call_id,
                            result.name,
                            cast(FrozenJsonValue, result.public_result),
                            is_error=not _tool_result_succeeded(result),
                        ),
                    ),
                )
            )

    def advance_after_tools(self) -> None:
        """@brief 在完整工具 step 后推进 provider 序号 / Advance the provider ordinal after a complete tool step.

        @return None / None.
        @raise ValueError 已达到配置上限时抛出 / Raised when the configured limit is exhausted.
        """

        if self._step >= self._config.max_iterations:
            raise ValueError("cannot advance beyond the tool iteration limit")
        self._step += 1

    def finish(self, completion: AssistantCompletion) -> AgentResponse:
        """@brief 提交最终 Assistant 消息并更新 ContextState / Commit the final Assistant message and update ContextState.

        @param completion 不含工具调用的最终 completion / Final completion without tool calls.
        @return 封闭且可持久化的 Agent 响应 / Closed, persistable Agent response.
        """

        if completion.tool_calls:
            raise ValueError("final completion cannot contain tool calls")
        self._messages.append(completion.message)
        self._persistable_messages.append(completion.message)
        self._context.record_agent_history(
            [*self._base_messages, *self._persistable_messages]
        )
        return AgentResponse(
            completion.content,
            tuple(self._events),
            self._context,
            tuple(self._persistable_messages),
        )


_LOGGER = logging.getLogger(__name__)
"""@brief Agent 工具执行诊断日志器 / Diagnostic logger for Agent tool execution."""

_PROMPT_CACHE_POLICY_REVISION = "assistant-system-memory-tools-v1"
"""@brief 静态 system/memory/tool policy cache namespace / Static cache namespace for system, memory, and tool policies."""

_PROMPT_CACHE_DEPLOYMENT_NAMESPACE = "fogmoe-bot"
"""@brief 跨 route 隔离的静态部署 namespace / Static deployment namespace isolating routes."""


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
        generation_fence: InferenceGenerationFencePort | None = None,
        progress: AssistantProgressPersistence | None = None,
    ) -> None:
        """@brief 注入全部外部端口 / Inject every external port.

        @param runtime 无状态工具协调器 / Stateless tool coordinator.
        @param completion 异步 provider port / Async provider port.
        @param checkpoints durable step store / Durable step store.
        @param memory 每次模型 Query fresh retrieve 的 WorkingMemory /
            WorkingMemory freshly retrieved for each model query.
        @param telemetry 进程 typed telemetry / Process typed telemetry.
        @param generation_fence checkpoint 与工具副作用前的跨进程 revision fence /
            Cross-process revision fence before checkpoints and tool effects.
        @param progress checkpoint/receipt 后追加稳定过程消息的 durable outbox port /
            Durable outbox port appending stable progress messages after checkpoints/receipts.
        @return None / None.
        """

        self._runtime = runtime
        self._completion = completion
        self._checkpoints = checkpoints
        self._memory = memory
        self._telemetry = telemetry
        self._generation_fence = generation_fence
        self._progress = progress
        """@brief append-only Agent 过程项持久化端口 / Append-only Agent progress-item persistence."""

    async def run(
        self,
        context: ContextState,
        config: AgentExecutionConfig,
        *,
        tool_context: ToolExecutionContext | None = None,
        stream: AssistantStreamSession | None = None,
        state: AgentExecutionState | None = None,
    ) -> AgentResponse:
        """@brief 运行或恢复一个 Agent Turn / Run or resume one Agent Turn.

        @param context attempt-local 规范上下文 / Attempt-local canonical context.
        @param config route 配置 / Route configuration.
        @param tool_context durable 工具身份；禁用工具时可省略 /
            Durable tool identity; optional when tools are disabled.
        @param stream 可选 provider 文本 delta 投影 / Optional provider text-delta projection.
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
                current,
                tool_context=tool_context,
                expose_tools=config.allow_tools,
                stream=stream,
            )
            if completion.tool_calls:
                try:
                    await self._publish_commentary_progress(
                        step=current.step,
                        completion=completion,
                        tool_context=tool_context,
                        stream=stream,
                    )
                except Exception as error:
                    if isinstance(
                        error,
                        (ResumableAgentInterruptedError, StaleClaimError),
                    ) or is_deterministic_agent_failure(error):
                        raise
                    raise ResumableAgentInterruptedError(
                        str(error) or error.__class__.__name__
                    ) from error
            if not completion.tool_calls:
                await self._assert_current_generation(tool_context)
                return _final_response(current, completion)
            if not config.allow_tools:
                raise ValueError(
                    "provider returned tool calls while tools were disabled"
                )
            current.record_tool_plan(completion)
            try:
                await self._execute_calls(
                    current,
                    completion=completion,
                    tool_context=cast(ToolExecutionContext, tool_context),
                    stream=stream,
                )
            except Exception as error:
                if isinstance(
                    error,
                    (ResumableAgentInterruptedError, StaleClaimError),
                ) or is_deterministic_agent_failure(error):
                    raise
                raise ResumableAgentInterruptedError(
                    str(error) or error.__class__.__name__
                ) from error
            current.advance_after_tools()

        completion = await self._complete_step(
            current,
            tool_context=tool_context,
            expose_tools=False,
            stream=stream,
        )
        if completion.tool_calls:
            raise ValueError(
                "provider returned tool calls after the tool iteration limit"
            )
        await self._assert_current_generation(tool_context)
        return _final_response(current, completion)

    async def _complete_step(
        self,
        state: AgentExecutionState,
        *,
        tool_context: ToolExecutionContext | None,
        expose_tools: bool,
        stream: AssistantStreamSession | None,
    ) -> AssistantCompletion:
        """@brief 读取 checkpoint 或先调用 provider 再保存 / Load a checkpoint or call and then persist the provider.

        @param state 当前状态 / Current state.
        @param tool_context durable identity / Durable identity.
        @param expose_tools 是否暴露目录 / Whether to expose the catalog.
        @param stream 可选文本 delta 投影 / Optional text-delta projection.
        @return 规范完成 / Canonical completion.
        """

        route_key = _route_key(state.config)
        allowed_tools = None if tool_context is None else tool_context.allowed_tools
        request_hash = _completion_request_hash(
            state,
            expose_tools=expose_tools,
            allowed_tools=allowed_tools,
        )
        generation = (
            0
            if tool_context is None or tool_context.generation_fence is None
            else int(tool_context.generation_fence.input_revision)
        )
        if tool_context is not None:
            await self._assert_current_generation(tool_context)
            existing = await self._checkpoints.load_step(
                tool_context.turn_id,
                state.step,
                generation=generation,
            )
            if existing is not None:
                _validate_checkpoint(
                    existing,
                    request_hash=request_hash,
                    route_key=route_key,
                )
                return existing.completion

        model_messages: tuple[CanonicalMessage, ...] = tuple(state.messages)
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
                    "memory.result.count",
                    len(working_memory.messages),
                )
                memory_span.set_attribute(
                    "memory.availability",
                    working_memory.availability.value,
                )
            model_messages = compose_model_messages(
                state.messages,
                working_memory,
                maximum_tokens=state.config.working_memory_max_tokens,
                stable_prefix_message_count=(state.context.stable_prefix_message_count),
            )

        definitions = (
            tuple(
                definition
                for definition in self._runtime.tool_definitions
                if definition.name not in state.config.skip_tools
                and (allowed_tools is None or definition.name in allowed_tools)
            )
            if expose_tools
            else ()
        )
        prompt_cache = _prompt_cache_directive(
            state,
            message_count=len(model_messages),
        )
        try:
            completion = await self._request_completion(
                state,
                messages=model_messages,
                definitions=definitions,
                expose_tools=expose_tools,
                tool_context=tool_context,
                stream=stream,
                prompt_cache=prompt_cache,
            )
        except StaleClaimError:
            raise
        except Exception as error:
            if is_deterministic_agent_failure(error):
                raise
            if state.step > 0 or state.events:
                raise ResumableAgentInterruptedError(
                    str(error) or error.__class__.__name__
                ) from error
            raise
        if tool_context is None:
            return completion
        await self._assert_current_generation(tool_context)
        checkpoint = AgentStepCheckpoint(
            turn_id=tool_context.turn_id,
            step_no=state.step,
            request_hash=request_hash,
            route_key=route_key,
            completion=completion,
            generation=generation,
            generation_fence=tool_context.generation_fence,
        )
        canonical = await self._checkpoints.save_step(checkpoint)
        _validate_checkpoint(canonical, request_hash=request_hash, route_key=route_key)
        await self._assert_current_generation(tool_context)
        return canonical.completion

    async def _request_completion(
        self,
        state: AgentExecutionState,
        *,
        messages: Sequence[CanonicalMessage],
        definitions: Sequence[ToolDefinition],
        expose_tools: bool,
        tool_context: ToolExecutionContext | None,
        stream: AssistantStreamSession | None,
        prompt_cache: PromptCacheDirective | None,
    ) -> AssistantCompletion:
        """@brief 选择流式或普通 provider 端口并收敛为完整 completion / Select a streaming or ordinary provider port and converge on one complete completion.

        @param state 当前 Agent 状态 / Current Agent state.
        @param messages 已注入动态 WorkingMemory 的模型消息 / Model messages including dynamic WorkingMemory.
        @param definitions 当前 step 暴露的 typed tools / Typed tools exposed for this step.
        @param expose_tools 是否允许工具 / Whether tools are exposed.
        @param tool_context 当前 durable generation fence / Current durable generation fence.
        @param stream 可选用户可见 delta session / Optional user-visible delta session.
        @param prompt_cache 稳定前缀缓存指令 / Stable-prefix cache directive.
        @return 完整且可 checkpoint 的 completion / Complete checkpointable completion.
        @raise ResumableAgentInterruptedError 已输出可见 delta 后流失败 /
            Raised when a stream fails after emitting a visible delta.
        """

        tool_choice = state.config.tool_choice if expose_tools else None
        stream_method = getattr(self._completion, "stream", None)
        if stream is None or not callable(stream_method):
            if prompt_cache is None:
                return await self._completion.complete(
                    route=state.config.route,
                    model=state.config.model,
                    messages=messages,
                    tools=definitions,
                    tool_choice=tool_choice,
                    max_tokens=state.config.max_tokens,
                    timeout_seconds=state.config.timeout_seconds,
                    request_meta=state.config.request_meta,
                )
            return await self._completion.complete(
                route=state.config.route,
                model=state.config.model,
                messages=messages,
                tools=definitions,
                tool_choice=tool_choice,
                max_tokens=state.config.max_tokens,
                timeout_seconds=state.config.timeout_seconds,
                request_meta=state.config.request_meta,
                prompt_cache=prompt_cache,
            )

        streaming = cast(AssistantStreamingCompletionPort, self._completion)
        events = streaming.stream(
            route=state.config.route,
            model=state.config.model,
            messages=messages,
            tools=definitions,
            tool_choice=tool_choice,
            max_tokens=state.config.max_tokens,
            timeout_seconds=state.config.timeout_seconds,
            request_meta=state.config.request_meta,
            prompt_cache=prompt_cache,
        )
        generation_watch = (
            asyncio.create_task(
                self._watch_stream_generation(tool_context),
                name=f"assistant-stream-generation-fence-{state.step}",
            )
            if (
                tool_context is not None
                and tool_context.generation_fence is not None
                and self._generation_fence is not None
            )
            else None
        )
        """@brief 在 provider 静默输出时仍按上限周期检查 steer / Check steering at a bounded cadence even while the provider is silent."""
        consume_task: asyncio.Task[AssistantCompletion] | None = None
        """@brief 在同一 Task/Context 中拥有 provider generator 全生命周期的消费任务 /
        Consumption task owning the provider generator's complete lifetime in one Task/Context.
        """
        try:
            if generation_watch is None:
                return await self._consume_completion_stream(events, stream=stream)
            consume_task = asyncio.create_task(
                self._consume_completion_stream(events, stream=stream),
                name=f"assistant-stream-consume-{state.step}",
            )
            done, _ = await asyncio.wait(
                (consume_task, generation_watch),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if generation_watch in done:
                consume_task.cancel()
                await asyncio.gather(consume_task, return_exceptions=True)
                consume_task = None
                await generation_watch
                raise RuntimeError(
                    "generation watch stopped without invalidating the stream"
                )
            return await consume_task
        finally:
            if consume_task is not None and not consume_task.done():
                consume_task.cancel()
                await asyncio.gather(consume_task, return_exceptions=True)
            if generation_watch is not None:
                generation_watch.cancel()
                await asyncio.gather(generation_watch, return_exceptions=True)

    async def _consume_completion_stream(
        self,
        events: AsyncIterator[AssistantCompletionStreamEvent],
        *,
        stream: AssistantStreamSession,
    ) -> AssistantCompletion:
        """@brief 在单一 asyncio Task 中消费并关闭 provider 流 /
        Consume and close a provider stream within one asyncio Task.

        @param events provider-neutral 异步事件流 / Provider-neutral asynchronous event stream.
        @param stream 用户可见流投影会话 / User-visible stream projection session.
        @return 唯一完整 completion / The single completed completion.
        @raise ResumableAgentInterruptedError 可见 delta 后 provider 中断时抛出 /
            Raised when the provider fails after a visible delta.
        @note ``ContextVar.Token`` 只能在创建它的 Context 中 reset。异步 generator
            不得用“每个 ``anext`` 新建一个 Task”的方式逐块推进。/
            A ``ContextVar.Token`` can only be reset in the Context that created it. An async
            generator must not be advanced by creating a fresh Task for every ``anext`` call.
        """

        emitted_visible_delta = False
        finished: AssistantCompletion | None = None
        try:
            async for event in events:
                if isinstance(event, CompletionTextDelta):
                    if finished is not None:
                        raise ValueError(
                            "provider stream emitted a delta after its terminal completion"
                        )
                    await stream.append(event.text, emitted_at=datetime.now(UTC))
                    emitted_visible_delta = True
                    continue
                if not isinstance(event, CompletionFinished):
                    raise TypeError(
                        "provider stream emitted an unsupported completion event"
                    )
                if finished is not None:
                    raise ValueError(
                        "provider stream emitted more than one terminal completion"
                    )
                finished = event.completion
        except StaleClaimError:
            raise
        except Exception as error:
            if is_deterministic_agent_failure(error):
                raise
            if emitted_visible_delta:
                raise ResumableAgentInterruptedError(
                    str(error) or error.__class__.__name__
                ) from error
            raise
        finally:
            await _close_completion_stream(events)
        if finished is None:
            missing_terminal = ValueError(
                "provider stream ended without a terminal completion"
            )
            if emitted_visible_delta:
                raise ResumableAgentInterruptedError(
                    str(missing_terminal)
                ) from missing_terminal
            raise missing_terminal
        return finished

    async def _watch_stream_generation(
        self,
        tool_context: ToolExecutionContext,
    ) -> None:
        """@brief provider 流存活期间定期验证 generation / Periodically validate the generation while a provider stream is alive.

        @param tool_context 当前 durable generation identity / Current durable generation identity.
        @return 正常情况下不返回 / Does not return normally.
        @raise StaleClaimError steer/reset 已取代该 generation / Raised when steer/reset supersedes this generation.
        """

        while True:
            await asyncio.sleep(_STREAM_GENERATION_FENCE_INTERVAL_SECONDS)
            await self._assert_current_generation(tool_context)

    async def _execute_calls(
        self,
        state: AgentExecutionState,
        *,
        completion: AssistantCompletion,
        tool_context: ToolExecutionContext,
        stream: AssistantStreamSession | None,
    ) -> None:
        """@brief 顺序执行一个 checkpoint 中的工具调用 / Sequentially execute calls from one checkpoint.

        @param state 当前状态 / Current state.
        @param completion 已持久化完成 / Persisted completion.
        @param tool_context durable identity / Durable identity.
        @param stream 可选高层活动投影 / Optional high-level activity projection.
        @return None / None.
        """

        results: list[ToolRuntimeResult] = []
        for ordinal, call in enumerate(completion.tool_calls):
            if call.name in state.config.skip_tools:
                raise ValueError(f"provider called a route-disabled tool: {call.name}")
            invocation_id = tool_invocation_id(
                tool_context,
                step=state.step,
                ordinal=ordinal,
            )
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
                await self._assert_current_generation(tool_context)
                await self._publish_tool_started_progress(
                    tool_context,
                    invocation_id=invocation_id,
                    tool_name=call.name,
                )
                try:
                    if stream is not None:
                        await stream.tool_started(
                            invocation_id,
                            call.name,
                            emitted_at=datetime.now(UTC),
                        )
                    result = await self._runtime.execute(
                        context=tool_context,
                        step=state.step,
                        ordinal=ordinal,
                        provider_call_id=call.provider_call_id,
                        tool_name=call.name,
                        raw_arguments=call.arguments,
                    )
                except StaleClaimError:
                    raise
                except Exception as error:
                    if stream is not None:
                        await stream.tool_finished(
                            invocation_id,
                            call.name,
                            succeeded=False,
                            emitted_at=datetime.now(UTC),
                        )
                    await self._publish_failed_tool_progress(
                        tool_context,
                        invocation_id=invocation_id,
                        tool_name=call.name,
                        error=error,
                    )
                    _annotate_workspace_failure(span, error)
                    _LOGGER.exception(
                        "Assistant tool execution failed tool_name=%s step=%s ordinal=%s",
                        call.name,
                        state.step,
                        ordinal,
                    )
                    self._telemetry.counter(
                        MetricName.TOOL_OUTCOMES,
                        attributes={
                            "outcome": Outcome.FAILURE,
                            "gen_ai.tool.name": call.name,
                        },
                    )
                    raise
                succeeded = _tool_result_succeeded(result)
                await self._publish_tool_progress(
                    tool_context,
                    invocation_id=invocation_id,
                    tool_name=call.name,
                    succeeded=succeeded,
                )
                if stream is not None:
                    await stream.tool_finished(
                        invocation_id,
                        call.name,
                        succeeded=succeeded,
                        emitted_at=datetime.now(UTC),
                    )
                span.set_attribute("fogmoe.tool.replayed", result.replayed)
                self._telemetry.counter(
                    MetricName.TOOL_OUTCOMES,
                    attributes={
                        "outcome": Outcome.SUCCESS,
                        "gen_ai.tool.name": call.name,
                        "fogmoe.tool.replayed": result.replayed,
                    },
                )
            state.record_tool_result(
                completion=completion,
                result=result,
                first=ordinal == 0,
            )
            results.append(result)
        state.retain_conversation_tool_exchange(
            completion=completion,
            results=tuple(results),
        )

    async def _publish_commentary_progress(
        self,
        *,
        step: int,
        completion: AssistantCompletion,
        tool_context: ToolExecutionContext | None,
        stream: AssistantStreamSession | None,
    ) -> None:
        """@brief checkpoint 后追加自然 commentary 稳定块 / Append a natural commentary block after checkpointing.

        @param step 当前 Agent 模型步骤 / Current Agent model step.
        @param completion 已 checkpoint 且包含工具调用的完成 /
            Checkpointed completion containing tool calls.
        @param tool_context durable Turn 与 generation 身份 / Durable Turn and generation identity.
        @param stream 可选瞬时当前动作投影 / Optional ephemeral current-action projection.
        @return None / None.
        @note 只有模型在工具调用前实际给出的文本才成为 commentary；绝不根据固定 workflow
            阶段伪造“正在分析”等台词。/ Only text actually emitted by the model before its tool
            call becomes commentary; fixed workflow phases never fabricate narration.
        """

        text = completion.content.strip()
        if not text:
            return
        emitted_at = datetime.now(UTC)
        item = commentary_progress_item(
            step=step,
            text=text,
            created_at=emitted_at,
        )
        if self._progress is not None:
            if tool_context is None:
                raise ValueError("Durable commentary requires tool_context")
            await self._progress.publish_progress(tool_context, item)
        if stream is not None:
            await stream.commentary(
                item.item_id,
                item.text,
                emitted_at=emitted_at,
            )

    async def _publish_tool_progress(
        self,
        tool_context: ToolExecutionContext,
        *,
        invocation_id: str,
        tool_name: str,
        succeeded: bool,
    ) -> None:
        """@brief receipt 后追加工具终态稳定块 / Append a stable tool terminal block after its receipt.

        @param tool_context durable Turn 与 generation 身份 / Durable Turn and generation identity.
        @param invocation_id 稳定工具调用 ID / Stable tool invocation ID.
        @param tool_name 目录工具名 / Catalog tool name.
        @param succeeded 是否形成可用结果 / Whether the call produced a usable result.
        @return None / None.
        """

        if self._progress is None:
            return
        await self._progress.publish_progress(
            tool_context,
            tool_progress_item(
                invocation_id=invocation_id,
                tool_name=tool_name,
                succeeded=succeeded,
                created_at=datetime.now(UTC),
            ),
        )

    async def _publish_tool_started_progress(
        self,
        tool_context: ToolExecutionContext,
        *,
        invocation_id: str,
        tool_name: str,
    ) -> None:
        """@brief 工具执行前追加不可变开始块 / Append an immutable start block before tool execution.

        @param tool_context durable Turn 与 generation 身份 / Durable Turn and generation identity.
        @param invocation_id 稳定工具调用 ID / Stable tool invocation ID.
        @param tool_name 目录工具名 / Catalog tool name.
        @return None / None.
        @note 开始块是独立真实消息，完成时追加新块而不编辑它，避免客户端重绘。/
            The start block is an independent real message. Completion appends another block
            instead of editing it, avoiding client-side redraw.
        """

        if self._progress is None:
            return
        await self._progress.publish_progress(
            tool_context,
            tool_started_progress_item(
                invocation_id=invocation_id,
                tool_name=tool_name,
                created_at=datetime.now(UTC),
            ),
        )

    async def _publish_failed_tool_progress(
        self,
        tool_context: ToolExecutionContext,
        *,
        invocation_id: str,
        tool_name: str,
        error: Exception,
    ) -> None:
        """@brief 不掩盖原始工具异常地尝试固化失败块 /
        Try to persist a failed tool block without masking the original tool exception.

        @param tool_context durable Turn 与 generation 身份 / Durable Turn and generation identity.
        @param invocation_id 稳定工具调用 ID / Stable tool invocation ID.
        @param tool_name 目录工具名 / Catalog tool name.
        @param error 即将传播的原始工具异常 / Original tool exception about to propagate.
        @return None / None.
        @note StaleClaimError 必须正常传播以停止旧 generation；其他 progress 写失败只记录，
            保留更有诊断价值的原始工具错误。/ StaleClaimError propagates to stop an old
            generation. Other progress-write failures are logged while preserving the more useful
            original tool failure.
        """

        if self._progress is None:
            return
        try:
            await self._publish_tool_progress(
                tool_context,
                invocation_id=invocation_id,
                tool_name=tool_name,
                succeeded=False,
            )
        except StaleClaimError:
            raise
        except Exception:
            _LOGGER.warning(
                "Could not persist failed Assistant tool progress "
                "tool_name=%s original_error=%s",
                tool_name,
                error.__class__.__name__,
                exc_info=True,
            )

    async def _assert_current_generation(
        self,
        tool_context: ToolExecutionContext | None,
    ) -> None:
        """@brief 在 checkpoint/final/tool 边界验证 generation / Validate the generation at checkpoint, final, and tool boundaries.

        @param tool_context 当前 durable tool context / Current durable tool context.
        @return None / None.
        """

        if (
            tool_context is None
            or tool_context.generation_fence is None
            or self._generation_fence is None
        ):
            return
        await self._generation_fence.assert_current_generation(
            tool_context.generation_fence
        )


def _completion_request_hash(
    state: AgentExecutionState,
    *,
    expose_tools: bool,
    allowed_tools: frozenset[str] | None = None,
) -> str:
    """@brief 摘要不含瞬时 WorkingMemory 的稳定模型 step / Digest a stable model step excluding ephemeral WorkingMemory.

    @param state 当前状态 / Current state.
    @param expose_tools 是否暴露工具 / Whether tools are exposed.
    @param allowed_tools 可选 Turn 工具 allowlist / Optional turn tool allowlist.
    @return SHA-256 / SHA-256.
    """

    payload: JsonObject = {
        "messages": [message.to_json() for message in state.messages],
        "route": _route_fingerprint(state.config.route),
        "model": state.config.model,
        "max_tokens": state.config.max_tokens,
        "tool_choice": state.config.tool_choice if expose_tools else None,
        "expose_tools": expose_tools,
        "skip_tools": cast(list[JsonValue], sorted(state.config.skip_tools)),
        "allowed_tools": (
            None
            if allowed_tools is None
            else cast(list[JsonValue], sorted(allowed_tools))
        ),
        "timeout_seconds": state.config.timeout_seconds,
        "request_meta": cast(
            JsonObject,
            request_meta_to_json(state.config.request_meta),
        ),
        "prompt_cache_policy": state.config.prompt_cache_policy,
        "prompt_cache_retention": state.config.prompt_cache_retention,
        "stable_prefix_message_count": (state.context.stable_prefix_message_count),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _tool_result_succeeded(result: ToolRuntimeResult) -> bool:
    """@brief 判断工具结果是否适合显示为完成 / Decide whether a tool result is suitable for a completed activity state.

    @param result 已规范化工具结果 / Normalized tool result.
    @return 无校验错误且公共结果不含顶层 error 时为 True /
        True when no validation error exists and the public result has no top-level error.
    @note 只检查结构化错误信号，不读取或展示结果内容 / Only structured error signals are
        inspected; result content is neither read for narration nor displayed.
    """

    return result.validation_error is None and not (
        isinstance(result.public_result, dict) and "error" in result.public_result
    )


def _prompt_cache_directive(
    state: AgentExecutionState,
    *,
    message_count: int,
) -> PromptCacheDirective | None:
    """@brief 从模型 capability 与稳定前缀边界构造 cache 指令 / Build a cache directive from model capability and the stable-prefix boundary.

    @param state 当前 Agent 状态 / Current Agent state.
    @param message_count 注入 WorkingMemory 后的完整消息数 / Full message count after WorkingMemory injection.
    @return 已 capability-gated 指令；禁用或没有可缓存前缀时为 None /
        Capability-gated directive, or None when disabled or no reusable prefix exists.
    @note cache key 只来自静态 deployment/route/model/policy namespace；Turn、用户文本与
        WorkingMemory 永远不参与 key。provider 仍会逐字节验证实际 prefix。/
        The cache key comes only from static deployment/route/model/policy namespaces. Turn,
        user text, and WorkingMemory never enter the key; the provider still byte-validates the
        actual prefix.
    """

    if message_count < 0:
        raise ValueError("message_count cannot be negative")
    policy = state.config.prompt_cache_policy
    boundary = state.context.stable_prefix_message_count
    if policy == "disabled" or boundary is None or boundary < 1:
        return None
    if boundary > message_count:
        raise ValueError(
            "stable prompt-cache prefix exceeds the rendered message count"
        )
    cache_key = PromptCacheKey.for_route_model(
        deployment_namespace=_PROMPT_CACHE_DEPLOYMENT_NAMESPACE,
        route_id=state.config.route.route_id,
        model=state.config.model,
        policy_revision=_PROMPT_CACHE_POLICY_REVISION,
    )
    if policy == "automatic":
        return PromptCacheDirective(
            stable_prefix_message_count=boundary,
            cache_key=cache_key,
            mode="automatic",
        )
    retention = state.config.prompt_cache_retention
    if (
        retention is None
    ):  # AgentExecutionConfig validates this; retain fail-closed locality.
        raise ValueError("explicit prompt caching lost its retention")
    return PromptCacheDirective(
        stable_prefix_message_count=boundary,
        cache_key=cache_key,
        mode="explicit",
        ttl=retention,
    )


def _annotate_workspace_failure(span: SpanScope, error: Exception) -> None:
    """@brief 把受控 Workspace 诊断加入当前工具 span / Add controlled Workspace diagnostics to the current tool span.

    @param span 当前 ``agent.tool.execute`` span / Current ``agent.tool.execute`` span.
    @param error 工具执行异常 / Tool execution exception.
    @return None / None.
    @note 只记录 Workspace 异常显式暴露的安全字段，不读取命令、stdin、参数或任意
        ``str(error)``。/ Only safe fields explicitly exposed by Workspace exceptions are
        recorded; commands, stdin, arguments, and arbitrary ``str(error)`` values are never read.
    """

    if not isinstance(error, WorkspaceRuntimeUnavailableError):
        return
    if error.diagnostic_code is not None:
        span.set_attribute("workspace.error.code", error.diagnostic_code)
    if error.diagnostic_message is not None:
        span.set_attribute("workspace.error.message", error.diagnostic_message)


def _route_fingerprint(route: ProviderRoute) -> JsonObject:
    """@brief 生成不含认证秘密的 route 摘要输入 / Build a route digest input without auth secrets.

    @param route 自包含 provider route / Self-contained provider route.
    @return 可稳定 JSON 序列化的 route 投影 / Stably JSON-serializable route projection.
    """

    return {
        "route_id": route.route_id,
        "provider_id": route.provider_id,
        "style": route.style,
        "endpoint": route.endpoint,
        "headers": dict(route.headers),
        "api_version": route.api_version,
        "supports_tools": route.supports_tools,
        "strict_tools": route.strict_tools,
        "disabled_tools": list(route.disabled_tools),
        "meta": dict(route.meta),
    }


def _route_key(config: AgentExecutionConfig) -> str:
    """@brief 生成 checkpoint 的稳定 route/model 键 / Build the stable route/model key for a checkpoint.

    @param config Agent route/model 配置 / Agent route/model configuration.
    @return route/model 键 / Route/model key.
    """

    return f"{config.route.route_id}:{config.model}"


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


def _final_response(
    state: AgentExecutionState,
    completion: AssistantCompletion,
) -> AgentResponse:
    """@brief 提交最终 canonical Assistant message / Commit the final canonical Assistant message.

    @param state 当前执行状态 / Current execution state.
    @param completion 无 tool calls 的完成 / Completion without tool calls.
    @return Agent response / Agent response.
    """

    return state.finish(completion)


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
        if message.role is MessageRole.USER and message.text.strip():
            return message.text.strip()
    raise ValueError("ContextState has no current user query for WorkingMemory")


__all__ = [
    "AgentExecutionConfig",
    "AgentExecutionState",
    "AgentLoop",
    "AgentResponse",
]
