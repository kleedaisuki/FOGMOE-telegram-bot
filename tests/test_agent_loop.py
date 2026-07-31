"""@brief 可恢复 Agent loop 测试 / Tests for the resumable Agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from observability_testkit import make_telemetry

from fogmoe_bot.application.assistant.agent_loop import AgentExecutionConfig, AgentLoop
from fogmoe_bot.application.assistant.completion import (
    AgentCheckpointConflictError,
    AgentStepCheckpoint,
    AssistantCompletion,
    CompletionFinished,
    CompletionTextDelta,
    PromptCacheDirective,
)
from fogmoe_bot.application.assistant.errors import (
    ProviderFailure,
    ProviderFailureKind,
    ResumableAgentInterruptedError,
)
from fogmoe_bot.application.assistant.progress import (
    AssistantProgressItem,
    AssistantProgressKind,
)
from fogmoe_bot.application.assistant.streaming import (
    AssistantStreamSession,
    AssistantStreamTarget,
)
from fogmoe_bot.domain.assistant.streaming import (
    AssistantActivityKind,
    AssistantActivityStatus,
    AssistantStreamFrame,
    AssistantStreamKind,
    AssistantStreamState,
)
from fogmoe_bot.application.assistant.tool_runtime import (
    AgentRuntime,
    PersistedToolResult,
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.assistant.tools.catalog import (
    DEFAULT_TOOL_CATALOG,
    ToolDefinition,
)
from fogmoe_bot.application.memory.ports import WorkingMemoryQuery
from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.domain.assistant.messages import (
    CanonicalMessage,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    text_message,
)
from fogmoe_bot.domain.assistant.request_metadata import RequestMeta
from fogmoe_bot.domain.assistant.routing.models import (
    ProviderAuth,
    ProviderRoute,
    RouteModel,
)
from fogmoe_bot.domain.context import ContextState, ConversationScope, UserState
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    InferenceActivityId,
    LeaseToken,
    TurnId,
    TurnRevision,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceGenerationCause,
    InferenceGenerationFence,
)
from fogmoe_bot.domain.conversation.errors import StaleClaimError
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.memory import (
    PersonalMemoryScope,
    WorkingMemory,
    WorkingMemoryAvailability,
)
from fogmoe_bot.domain.observability.signals import SpanSignal


class _Checkpoints:
    """@brief 内存 checkpoint port / In-memory checkpoint port."""

    def __init__(self, order: list[str]) -> None:
        """@brief 保存共享顺序日志 / Store a shared order log.

        @param order 供断言的事件顺序 / Event ordering used by assertions.
        """

        self.values: dict[tuple[TurnId, int, int], AgentStepCheckpoint] = {}
        self.order = order

    async def load_step(
        self,
        turn_id: TurnId,
        step_no: int,
        *,
        generation: int = 0,
    ) -> AgentStepCheckpoint | None:
        """@brief 读取 checkpoint / Load a checkpoint.

        @param turn_id 回合标识 / Turn identifier.
        @param step_no 模型步骤序号 / Model step number.
        @param generation input-revision effect generation / Input-revision effect generation.
        @return checkpoint 或 None / Checkpoint or None.
        """

        return self.values.get((turn_id, generation, step_no))

    async def save_step(self, checkpoint: AgentStepCheckpoint) -> AgentStepCheckpoint:
        """@brief 保存 checkpoint / Save a checkpoint.

        @param checkpoint 待保存 checkpoint / Checkpoint to save.
        @return 已保存 checkpoint / Saved checkpoint.
        """

        self.order.append(f"checkpoint:{checkpoint.step_no}")
        return self.values.setdefault(
            (checkpoint.turn_id, checkpoint.generation, checkpoint.step_no),
            checkpoint,
        )


class _Completion:
    """@brief 队列 completion port / Queue-backed completion port."""

    def __init__(
        self,
        values: list[AssistantCompletion | Exception],
        order: list[str],
    ) -> None:
        """@brief 保存 responses / Store responses.

        @param values 按调用顺序返回的 completion 或异常 /
            Completions or failures returned in call order.
        @param order 供断言的事件顺序 / Event ordering used by assertions.
        """

        self.values = values
        self.calls = 0
        self.order = order
        self.requests: list[dict[str, object]] = []

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
        prompt_cache: PromptCacheDirective | None = None,
    ) -> AssistantCompletion:
        """@brief 返回下一个 response / Return the next response.

        @param route 自包含 provider route / Self-contained provider route.
        @param model route 中的模型 / Model selected in the route.
        @param messages canonical V2 输入 / Canonical V2 input messages.
        @param tools 暴露给模型的工具 / Tools exposed to the model.
        @param tool_choice provider-neutral tool choice / Provider-neutral tool choice.
        @param max_tokens 输出 token 上限 / Output token limit.
        @param timeout_seconds 单请求超时 / Per-request timeout.
        @param request_meta 显式请求 metadata / Explicit request metadata.
        @param prompt_cache 可选稳定前缀缓存指令 / Optional stable-prefix cache directive.
        @return 下一个 completion / Next completion.
        """

        self.requests.append(
            {
                "route": route,
                "model": model,
                "messages": tuple(messages),
                "tools": tuple(tools),
                "tool_choice": tool_choice,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
                "request_meta": request_meta,
                "prompt_cache": prompt_cache,
            }
        )
        self.order.append(f"provider:{self.calls}")
        self.calls += 1
        if not self.values:
            raise AssertionError("checkpoint replay called provider")
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _VisibleThenFailingCompletion:
    """@brief 发出可见文本后失败的流式 provider / Streaming provider that fails after visible text."""

    def __init__(self) -> None:
        """@brief 初始化调用次数 / Initialize call count."""

        self.calls = 0

    async def complete(self, **kwargs: object) -> AssistantCompletion:
        """@brief 非流路径在本测试中不可达 / The non-stream path is unreachable in this test."""

        del kwargs
        raise AssertionError("visible-delta test used non-stream completion")

    async def stream(
        self,
        **kwargs: object,
    ) -> AsyncIterator[CompletionTextDelta]:
        """@brief 发出 old 后模拟 transport failure / Emit old, then simulate a transport failure.

        @return provider-neutral 异步事件流 / Provider-neutral asynchronous event stream.
        """

        del kwargs
        self.calls += 1
        yield CompletionTextDelta("old")
        raise RuntimeError("provider A disconnected")


class _SteeredStreamingCompletion:
    """@brief 在两个 delta 间保留流以验证 steer 取消 / Keep a stream open between two deltas to verify steer cancellation."""

    def __init__(self) -> None:
        """@brief 初始化关闭观察值 / Initialize the close observation."""

        self.closed = False

    async def complete(self, **kwargs: object) -> AssistantCompletion:
        """@brief 非流路径不可达 / The non-stream path is unreachable."""

        del kwargs
        raise AssertionError("steer cancellation test used non-stream completion")

    async def stream(
        self,
        **kwargs: object,
    ) -> AsyncIterator[CompletionTextDelta | CompletionFinished]:
        """@brief 发出 old，跨过 fence 周期，再尝试发出迟到文本 / Emit old, cross one fence interval, then attempt a late delta.

        @return provider-neutral 异步事件流 / Provider-neutral asynchronous event stream.
        """

        del kwargs
        try:
            yield CompletionTextDelta("old")
            await asyncio.sleep(0.21)
            yield CompletionTextDelta("late")
            yield CompletionFinished(_assistant_text("oldlate"))
        finally:
            self.closed = True


class _ContextBoundStreamingCompletion:
    """@brief 要求整个异步 generator 由同一 Task 推进的 completion /
    Completion requiring one Task to advance the complete async generator.
    """

    def __init__(self) -> None:
        """@brief 初始化观察到的消费任务 / Initialize observed consumer tasks."""

        self.consumer_tasks: list[asyncio.Task[object] | None] = []

    async def complete(self, **kwargs: object) -> AssistantCompletion:
        """@brief 非流路径不可达 / The non-stream path is unreachable."""

        del kwargs
        raise AssertionError("context-bound test used non-stream completion")

    async def stream(
        self,
        **kwargs: object,
    ) -> AsyncIterator[CompletionTextDelta | CompletionFinished]:
        """@brief 在 yield 两侧断言 Task identity 稳定 / Assert stable Task identity across yields.

        @return provider-neutral 异步事件流 / Provider-neutral asynchronous event stream.
        """

        del kwargs
        owner = asyncio.current_task()
        self.consumer_tasks.append(owner)
        yield CompletionTextDelta("stable")
        self.consumer_tasks.append(asyncio.current_task())
        if asyncio.current_task() is not owner:
            raise RuntimeError("provider generator crossed asyncio Task contexts")
        yield CompletionFinished(_assistant_text("stable"))
        self.consumer_tasks.append(asyncio.current_task())
        if asyncio.current_task() is not owner:
            raise RuntimeError(
                "provider generator closed in another asyncio Task context"
            )


class _StaleOnSecondFence:
    """@brief 第二次读取时模拟 steer 的 generation fence / Generation fence simulating a steer on its second read."""

    def __init__(self) -> None:
        """@brief 初始化读取次数 / Initialize the read count."""

        self.calls = 0

    async def assert_current_generation(
        self,
        fence: InferenceGenerationFence,
    ) -> None:
        """@brief 第二次验证拒绝旧 generation / Reject the old generation on the second validation.

        @param fence 当前 claim fence / Current claim fence.
        @return 当前 generation 时为 None / None while current.
        @raise StaleClaimError 第二次及以后读取时抛出 / Raised on and after the second read.
        """

        del fence
        self.calls += 1
        if self.calls >= 2:
            raise StaleClaimError("steered")


class _StaleAtFenceCall:
    """@brief 在指定 generation-fence 读取处模拟 steer / Simulate steering at a selected generation-fence read."""

    def __init__(self, call: int) -> None:
        """@brief 保存触发读取序号 / Store the triggering read ordinal.

        @param call 从一开始的触发序号 / One-based triggering ordinal.
        """

        self.calls = 0
        self.call = call

    async def assert_current_generation(
        self,
        fence: InferenceGenerationFence,
    ) -> None:
        """@brief 到达指定序号后拒绝旧 generation / Reject the old generation at the selected ordinal.

        @param fence 当前 claim fence / Current claim fence.
        @return 触发前为 None / None before the trigger.
        @raise StaleClaimError 到达触发序号时抛出 / Raised at the trigger ordinal.
        """

        del fence
        self.calls += 1
        if self.calls >= self.call:
            raise StaleClaimError("steered before tool start")


class _StreamProjection:
    """@brief 记录 AgentLoop 用户可见 frames / Record AgentLoop user-visible frames."""

    def __init__(self) -> None:
        """@brief 初始化 frame 日志 / Initialize the frame log."""

        self.frames: list[AssistantStreamFrame] = []

    async def project(
        self,
        target: AssistantStreamTarget,
        frame: AssistantStreamFrame,
    ) -> None:
        """@brief 记录 frame / Record a frame.

        @param target 显式投影目标 / Explicit projection target.
        @param frame 当前累计流状态 / Current cumulative stream state.
        @return None / None.
        """

        assert target.chat_id == 42
        self.frames.append(frame)


class _ProgressPersistence:
    """@brief 记录 durable Agent 过程项 / Record durable Agent progress items."""

    def __init__(self) -> None:
        """@brief 初始化过程项日志 / Initialize the progress-item log."""

        self.items: list[tuple[ToolExecutionContext, AssistantProgressItem]] = []

    async def publish_progress(
        self,
        context: ToolExecutionContext,
        item: AssistantProgressItem,
    ) -> None:
        """@brief 记录一个稳定过程项 / Record one stable progress item.

        @param context 当前工具执行上下文 / Current tool execution context.
        @param item 已稳定过程项 / Stable progress item.
        @return None / None.
        """

        self.items.append((context, item))


class _Memory:
    """@brief 记录每次 fresh WorkingMemory 查询 / Record every fresh WorkingMemory query."""

    def __init__(
        self,
        availability: WorkingMemoryAvailability = WorkingMemoryAvailability.AVAILABLE,
    ) -> None:
        """@brief 初始化查询日志与可用性 / Initialize the query log and availability.

        @param availability 返回的召回可用性 / Recall availability to return.
        """

        self.queries: list[WorkingMemoryQuery] = []
        self.availability = availability

    async def retrieve(self, query: WorkingMemoryQuery) -> WorkingMemory:
        """@brief 返回空但有作用域的工作记忆 / Return empty scoped working memory.

        @param query 当前检索请求 / Current retrieval query.
        @return 作用域正确的空 WorkingMemory / Empty WorkingMemory with the right scope.
        """

        self.queries.append(query)
        return WorkingMemory(
            scope=query.scope,
            query=query.text,
            messages=(),
            availability=self.availability,
        )


class _Receipts:
    """@brief 幂等 receipt port / Idempotent receipt port."""

    def __init__(self, order: list[str]) -> None:
        """@brief 初始化 receipt map / Initialize receipt map.

        @param order 供断言的事件顺序 / Event ordering used by assertions.
        """

        self.values: dict[tuple[str, str], JsonValue] = {}
        self.mutation_count = 0
        self.order = order

    async def execute(self, request: ToolEffectRequest) -> PersistedToolResult:
        """@brief 首次 mutation，随后 replay / Mutate once and replay thereafter.

        @param request 工具副作用请求 / Tool effect request.
        @return 初次结果或重放结果 / First result or replayed result.
        """

        key = (request.invocation_id, request.effect_kind)
        if key in self.values:
            return PersistedToolResult(self.values[key], True)
        self.order.append(f"effect:{request.invocation_id}")
        self.mutation_count += 1
        result: JsonObject = {"status": "updated"}
        self.values[key] = result
        return PersistedToolResult(result, False)


class _FreshMemoryTool:
    """@brief 验证 Memory tool 使用非缓存执行契约 / Verify the Memory tool uses the non-cacheable execution contract."""

    def __init__(self) -> None:
        """@brief 初始化请求日志 / Initialize the request log."""

        self.requests: list[ToolEffectRequest] = []

    async def execute(self, request: ToolEffectRequest) -> PersistedToolResult:
        """@brief 返回仅当前回合可见的敏感文本 / Return sensitive text visible only in this turn.

        @param request 工具副作用请求 / Tool effect request.
        @return 仅当前 Agent turn 可见的结果 / Result visible only within this Agent turn.
        """

        self.requests.append(request)
        assert request.result_cacheable is False
        return PersistedToolResult(
            {"results": [{"content": "private recalled text"}]},
            False,
        )


def _route(
    *,
    route_id: str = "test",
    meta: Mapping[str, str] | None = None,
) -> ProviderRoute:
    """@brief 构造测试用自包含 OpenAI-style route / Build a self-contained OpenAI-style test route.

    @param route_id 稳定 route 标识 / Stable route identifier.
    @param meta route 级 provider metadata / Route-level provider metadata.
    @return 有效的 provider route / Valid provider route.
    """

    return ProviderRoute(
        route_id=route_id,
        provider_id=route_id,
        provider_label=f"{route_id} label",
        style="openai",
        endpoint=f"https://{route_id}.example.test/v1/chat/completions",
        auth=ProviderAuth(),
        models=(RouteModel("model"),),
        meta={} if meta is None else meta,
    )


def _assistant_text(text: str) -> AssistantCompletion:
    """@brief 构造纯文本 canonical completion / Build a text-only canonical completion.

    @param text Assistant 文本 / Assistant text.
    @return canonical completion / Canonical completion.
    """

    return AssistantCompletion(text_message(MessageRole.ASSISTANT, text))


def _assistant_tool_call(
    text: str,
    *,
    call_id: str,
    name: str,
    arguments: JsonValue,
) -> AssistantCompletion:
    """@brief 构造含一个 canonical 工具调用的 completion / Build a completion with one canonical tool call.

    @param text Assistant 可见文本 / Assistant-visible text.
    @param call_id 非空跨协议调用 ID / Non-empty cross-protocol call identifier.
    @param name 工具目录名称 / Tool-catalog name.
    @param arguments 已解析 JSON 参数 / Parsed JSON arguments.
    @return canonical completion / Canonical completion.
    """

    return AssistantCompletion(
        CanonicalMessage(
            MessageRole.ASSISTANT,
            (TextPart(text), ToolCallPart(call_id, name, arguments)),
        )
    )


def _context() -> ContextState:
    """@brief 构造模型上下文 / Build model context.

    @return 含一条 canonical user message 的上下文 / Context with one canonical user message.
    """

    return ContextState(
        context_id=uuid4(),
        scope=ConversationScope(user_id=42),
        user_state=UserState(coins=10, plan="free", permission=0, profile=None),
        messages=[text_message(MessageRole.USER, "remember me")],
        tool_context={},
    )


def _tool_context(
    turn_id: TurnId,
    *,
    allowed_tools: frozenset[str] | None = None,
    generation_fence: InferenceGenerationFence | None = None,
) -> ToolExecutionContext:
    """@brief 构造 durable tool context / Build durable tool context.

    @param turn_id durable Turn ID / Durable turn identifier.
    @param allowed_tools 可选 Turn 工具 allowlist / Optional turn tool allowlist.
    @param generation_fence 可选 processing generation / Optional processing generation.
    @return 工具运行时上下文 / Tool runtime context.
    """

    return ToolExecutionContext(
        turn_id=turn_id,
        conversation_id=ConversationId("assistant-user:42"),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
        user_id=42,
        chat_id=42,
        is_group=False,
        group_id=None,
        message_id=1,
        allowed_tools=allowed_tools,
        generation_fence=generation_fence,
    )


def test_turn_capability_filters_provider_tool_definitions() -> None:
    """@brief Provider 只看到 Turn allowlist 与 route 支持集的交集 / The provider sees only the intersection of turn and route capabilities."""

    async def scenario() -> None:
        """@brief 执行一个无工具调用的受限回合 / Run one restricted turn without a tool call.

        @return None / None.
        """

        order: list[str] = []
        completion = _Completion([_assistant_text("done")], order)
        await AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_Receipts(order),
            ),
            completion=completion,
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
        ).run(
            _context(),
            AgentExecutionConfig(route=_route(), model="model", allow_tools=True),
            tool_context=_tool_context(
                TurnId.new(),
                allowed_tools=frozenset({"get_current_time", "search_memory_by_time"}),
            ),
        )

        tools = cast(tuple[ToolDefinition, ...], completion.requests[0]["tools"])
        assert [definition.name for definition in tools] == [
            "get_current_time",
            "search_memory_by_time",
        ]
        assert dict(cast(RequestMeta, completion.requests[0]["request_meta"])) == {}

    asyncio.run(scenario())


def test_agent_loop_appends_checkpoint_commentary_and_receipt_backed_tool_progress() -> (
    None
):
    """@brief Agent loop 追加 checkpoint commentary 与 receipt 工具终态 /
    The Agent loop appends checkpoint commentary and receipt-backed tool progress.
    """

    async def scenario() -> None:
        """@brief 执行一个单工具回合并检查活动顺序 / Run one tool turn and inspect activity ordering."""

        order: list[str] = []
        turn_id = TurnId.new()
        projection = _StreamProjection()
        progress = _ProgressPersistence()
        session = AssistantStreamSession(
            target=AssistantStreamTarget(
                chat_id=42,
                is_group=False,
                message_thread_id=None,
            ),
            state=AssistantStreamState.begin(
                turn_id=turn_id,
                generation=1,
                revision=0,
                emitted_at=datetime.now(UTC),
            ),
            projection=projection,
        )
        await session.start()
        completion = _Completion(
            [
                _assistant_tool_call(
                    "我先确认一下当前时间，再给你准确回答。",
                    call_id="provider-time-call",
                    name="get_current_time",
                    arguments={},
                ),
                _assistant_text("现在告诉你答案"),
            ],
            order,
        )
        response = await AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_Receipts(order),
            ),
            completion=completion,
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
            progress=progress,
        ).run(
            _context(),
            AgentExecutionConfig(route=_route(), model="model", allow_tools=True),
            tool_context=_tool_context(turn_id),
            stream=session,
        )

        assert response.text == "现在告诉你答案"
        activity_frames = [
            frame
            for frame in projection.frames
            if frame.kind is AssistantStreamKind.ACTIVITY
        ]
        assert len(activity_frames) == 3
        commentary = activity_frames[0].activities[-1]
        tool_started = activity_frames[1].activities[-1]
        tool_finished = activity_frames[2].activities[-1]
        assert commentary.kind is AssistantActivityKind.COMMENTARY
        assert commentary.label == "我先确认一下当前时间，再给你准确回答。"
        assert commentary.status is AssistantActivityStatus.COMPLETED
        assert tool_started.kind is AssistantActivityKind.TOOL
        assert tool_started.label == "get_current_time"
        assert tool_started.status is AssistantActivityStatus.ACTIVE
        assert tool_finished.key == tool_started.key == "tool:step:0:call:0"
        assert tool_finished.status is AssistantActivityStatus.COMPLETED
        assert all(
            "现在告诉你答案" not in frame.cumulative_text for frame in activity_frames
        )
        assert [item.kind for _, item in progress.items] == [
            AssistantProgressKind.COMMENTARY,
            AssistantProgressKind.TOOL,
            AssistantProgressKind.TOOL,
        ]
        assert [item.item_id for _, item in progress.items] == [
            "step:0:commentary",
            "tool:step:0:call:0:started",
            "tool:step:0:call:0",
        ]
        assert progress.items[0][1].text == ("我先确认一下当前时间，再给你准确回答。")
        assert progress.items[1][1].text == (
            "✦ 我确认一下现在的时间…\n  能力：get_current_time"
        )
        assert progress.items[2][1].text == ("✓ 时间确认好啦\n  能力：get_current_time")

    asyncio.run(scenario())


def test_checkpoint_precedes_effect_and_restart_replays_without_provider_or_mutation() -> (
    None
):
    """@brief kill-9 replay 使用相同 canonical plan/receipt / Kill-9 replay uses the same canonical plan and receipt."""

    async def scenario() -> None:
        """@brief 执行首次与重启场景 / Execute initial and restarted scenarios.

        @return None / None.
        """

        order: list[str] = []
        turn_id = TurnId.new()
        checkpoints = _Checkpoints(order)
        receipts = _Receipts(order)
        first_completion = _Completion(
            [
                _assistant_tool_call(
                    "",
                    call_id="provider-call-a",
                    name="user_diary",
                    arguments={"action": "append", "content": "note"},
                ),
                _assistant_text("done"),
            ],
            order,
        )
        runtime = AgentRuntime(catalog=DEFAULT_TOOL_CATALOG, persistence=receipts)
        first_memory = _Memory()
        first_loop = AgentLoop(
            runtime=runtime,
            completion=first_completion,
            checkpoints=checkpoints,
            memory=first_memory,
            telemetry=make_telemetry(),
        )
        config = AgentExecutionConfig(route=_route(), model="model", allow_tools=True)
        first = await first_loop.run(
            _context(), config, tool_context=_tool_context(turn_id)
        )

        assert first.text == "done"
        assert order.index("checkpoint:0") < order.index("effect:step:0:call:0")
        assert receipts.mutation_count == 1
        assert [query.text for query in first_memory.queries] == [
            "remember me",
            "remember me",
        ]
        assert all(
            isinstance(query.scope, PersonalMemoryScope) and query.scope.user_id == 42
            for query in first_memory.queries
        )
        assert all(
            sum(
                "<working_memory" in message.text
                for message in cast(tuple[CanonicalMessage, ...], request["messages"])
            )
            == 1
            for request in first_completion.requests
        )

        checkpoint_completion = checkpoints.values[(turn_id, 0, 0)].completion
        assert checkpoint_completion.message.to_json() == {
            "schema_version": 2,
            "role": "assistant",
            "parts": [
                {"type": "text", "text": ""},
                {
                    "type": "tool_call",
                    "call_id": "provider-call-a",
                    "name": "user_diary",
                    "arguments": {"action": "append", "content": "note"},
                },
            ],
            "policy": {"include_in_context": True},
            "meta": {},
        }
        post_tool_messages = cast(
            tuple[CanonicalMessage, ...], first_completion.requests[1]["messages"]
        )
        tool_messages = [
            message
            for message in post_tool_messages
            if message.role is MessageRole.TOOL
        ]
        assert len(tool_messages) == 1
        tool_part = tool_messages[0].parts[0]
        assert isinstance(tool_part, ToolResultPart)
        assert tool_part.call_id == "provider-call-a"
        assert tool_part.name == "user_diary"
        assert tool_part.is_error is False
        assert tool_part.to_json()["result"] == {"status": "updated"}

        replay_completion = _Completion([], order)
        replay_memory = _Memory()
        replay_loop = AgentLoop(
            runtime=runtime,
            completion=replay_completion,
            checkpoints=checkpoints,
            memory=replay_memory,
            telemetry=make_telemetry(),
        )
        replay = await replay_loop.run(
            _context(), config, tool_context=_tool_context(turn_id)
        )

        assert replay.text == "done"
        assert replay_completion.calls == 0
        assert replay_memory.queries == []
        assert receipts.mutation_count == 1
        results = [event for event in replay.events if event["type"] == "tool_result"]
        assert results[0]["replayed"] is True

    asyncio.run(scenario())


def test_contract_failure_after_tool_checkpoint_is_not_wrapped_as_resumable() -> None:
    """@brief 工具 checkpoint 后的 contract 失败不会伪装成暂态重试 /
    A contract failure after a tool checkpoint is not disguised as a transient retry.
    """

    async def scenario() -> None:
        """@brief 执行一项工具后让下一模型步骤返回 contract /
        Return a contract failure on the model step after one tool.
        """

        order: list[str] = []
        failure = ProviderFailure(
            kind=ProviderFailureKind.CONTRACT,
            status=400,
            message="invalid response contract",
        )
        loop = AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_Receipts(order),
            ),
            completion=_Completion(
                [
                    _assistant_tool_call(
                        "",
                        call_id="provider-time-call",
                        name="get_current_time",
                        arguments={},
                    ),
                    failure,
                ],
                order,
            ),
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
        )

        with pytest.raises(ProviderFailure) as captured:
            await loop.run(
                _context(),
                AgentExecutionConfig(route=_route(), model="model"),
                tool_context=_tool_context(TurnId.new()),
            )

        assert captured.value is failure
        assert order.count("provider:0") == 1
        assert order.count("provider:1") == 1
        assert len([entry for entry in order if entry.startswith("effect:")]) == 1

    asyncio.run(scenario())


def test_memory_tool_result_never_enters_context_state_or_history() -> None:
    """@brief 显式 Memory tool 只活在 AgentExecutionState / An explicit Memory tool lives only in AgentExecutionState."""

    async def scenario() -> None:
        """@brief 执行一次 Memory tool loop / Execute one Memory-tool loop.

        @return None / None.
        """

        order: list[str] = []
        completion = _Completion(
            [
                _assistant_tool_call(
                    "",
                    call_id="memory-call",
                    name="search_memory",
                    arguments={"query": "tea", "limit": 3},
                ),
                _assistant_text("answer"),
            ],
            order,
        )
        effects = _FreshMemoryTool()
        context = _context()
        response = await AgentLoop(
            runtime=AgentRuntime(catalog=DEFAULT_TOOL_CATALOG, persistence=effects),
            completion=completion,
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
        ).run(
            context,
            AgentExecutionConfig(route=_route(), model="model", allow_tools=True),
            tool_context=_tool_context(TurnId.new()),
        )

        assert response.history_messages == (
            text_message(MessageRole.ASSISTANT, "answer"),
        )
        assert context.messages == [
            text_message(MessageRole.USER, "remember me"),
            text_message(MessageRole.ASSISTANT, "answer"),
        ]
        assert all(event.get("ephemeral") is True for event in response.events)
        transient_messages = cast(
            tuple[CanonicalMessage, ...], completion.requests[1]["messages"]
        )
        assert any(
            isinstance(part, ToolResultPart)
            and part.to_json()["result"]
            == {"results": [{"content": "private recalled text"}]}
            for message in transient_messages
            for part in message.parts
        )

    asyncio.run(scenario())


def test_ephemeral_tool_filter_never_persists_empty_tool_calls() -> None:
    """@brief 过滤临时工具后保留文本但不保留空调用 / Filtering an ephemeral tool keeps text without empty calls."""

    async def scenario() -> None:
        """@brief 执行带文本的临时 Memory tool 回合 / Run an ephemeral Memory tool turn carrying text.

        @return None / None.
        """

        order: list[str] = []
        completion = _Completion(
            [
                _assistant_tool_call(
                    "I will check memory.",
                    call_id="memory-call",
                    name="search_memory",
                    arguments={"query": "tea", "limit": 3},
                ),
                _assistant_text("final answer"),
            ],
            order,
        )
        response = await AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_FreshMemoryTool(),
            ),
            completion=completion,
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
        ).run(
            _context(),
            AgentExecutionConfig(route=_route(), model="model", allow_tools=True),
            tool_context=_tool_context(TurnId.new()),
        )

        assert response.history_messages == (
            text_message(MessageRole.ASSISTANT, "I will check memory."),
            text_message(MessageRole.ASSISTANT, "final answer"),
        )
        assert all(
            not isinstance(part, ToolCallPart)
            for message in response.history_messages
            for part in message.parts
        )

    asyncio.run(scenario())


def test_unavailable_working_memory_is_optional_and_observable() -> None:
    """@brief WorkingMemory 不可用时继续推理、跳过注入并记录状态 / Unavailable WorkingMemory continues inference, skips injection, and records status."""

    async def scenario() -> None:
        """@brief 执行一次不可用召回的模型步骤 / Execute one model step with unavailable recall.

        @return None / None.
        """

        order: list[str] = []
        completion = _Completion([_assistant_text("done")], order)
        buffer = TelemetryBuffer(64)
        response = await AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_Receipts(order),
            ),
            completion=completion,
            checkpoints=_Checkpoints(order),
            memory=_Memory(WorkingMemoryAvailability.UNAVAILABLE),
            telemetry=Telemetry(buffer),
        ).run(
            _context(),
            AgentExecutionConfig(route=_route(), model="model", allow_tools=False),
            tool_context=_tool_context(TurnId.new()),
        )

        assert response.text == "done"
        model_messages = cast(
            tuple[CanonicalMessage, ...], completion.requests[0]["messages"]
        )
        assert model_messages == (text_message(MessageRole.USER, "remember me"),)
        spans = tuple(
            signal
            for signal in buffer.drain(64)
            if isinstance(signal, SpanSignal)
            and signal.name == "memory.working.retrieve"
        )
        assert len(spans) == 1
        assert spans[0].attributes["memory.availability"] == "unavailable"
        assert spans[0].attributes["memory.result.count"] == 0

    asyncio.run(scenario())


def test_route_disabled_tool_call_is_rejected_before_any_effect() -> None:
    """@brief route 禁用工具调用在执行前被拒绝 / A route-disabled tool call is rejected before any effect."""

    async def scenario() -> None:
        """@brief 验证 checkpoint 后不会执行被禁用工具 / Verify a disabled tool is not executed after checkpointing.

        @return None / None.
        """

        order: list[str] = []
        receipts = _Receipts(order)
        route = _route()
        completion = _Completion(
            [
                _assistant_tool_call(
                    "",
                    call_id="forbidden-call",
                    name="user_diary",
                    arguments={"action": "append", "content": "must not persist"},
                )
            ],
            order,
        )
        loop = AgentLoop(
            runtime=AgentRuntime(catalog=DEFAULT_TOOL_CATALOG, persistence=receipts),
            completion=completion,
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
        )

        with pytest.raises(ValueError, match="route-disabled tool"):
            await loop.run(
                _context(),
                AgentExecutionConfig(
                    route=route,
                    model="model",
                    allow_tools=True,
                    skip_tools=frozenset({"user_diary"}),
                ),
                tool_context=_tool_context(TurnId.new()),
            )

        assert receipts.mutation_count == 0
        assert all(not entry.startswith("effect:") for entry in order)

    asyncio.run(scenario())


def test_checkpoint_hash_binds_route_and_explicit_request_meta() -> None:
    """@brief checkpoint hash 绑定 route 投影和显式 request meta / The checkpoint hash binds the route projection and explicit request metadata."""

    async def scenario() -> None:
        """@brief 保存一次 completion 后验证 route/meta 漂移冲突 / Save one completion, then verify route/meta drift conflicts.

        @return None / None.
        """

        order: list[str] = []
        turn_id = TurnId.new()
        checkpoints = _Checkpoints(order)
        initial_route = _route(route_id="hash", meta={"deployment": "first"})
        initial_completion = _Completion([_assistant_text("done")], order)
        initial_config = AgentExecutionConfig(
            route=initial_route,
            model="model",
            allow_tools=False,
            working_memory_enabled=False,
            timeout_seconds=12.0,
            request_meta={"trace_id": "first"},
        )
        initial_loop = AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_Receipts(order),
            ),
            completion=initial_completion,
            checkpoints=checkpoints,
            memory=_Memory(),
            telemetry=make_telemetry(),
        )
        await initial_loop.run(
            _context(), initial_config, tool_context=_tool_context(turn_id)
        )

        request = initial_completion.requests[0]
        assert request["route"] is initial_route
        assert request["timeout_seconds"] == 12.0
        assert dict(cast(RequestMeta, request["request_meta"])) == {"trace_id": "first"}

        for conflicting_config in (
            AgentExecutionConfig(
                route=initial_route,
                model="model",
                allow_tools=False,
                working_memory_enabled=False,
                timeout_seconds=12.0,
                request_meta={"trace_id": "changed"},
            ),
            AgentExecutionConfig(
                route=_route(route_id="hash", meta={"deployment": "changed"}),
                model="model",
                allow_tools=False,
                working_memory_enabled=False,
                timeout_seconds=12.0,
                request_meta={"trace_id": "first"},
            ),
        ):
            replay_completion = _Completion([], order)
            replay_loop = AgentLoop(
                runtime=AgentRuntime(
                    catalog=DEFAULT_TOOL_CATALOG,
                    persistence=_Receipts(order),
                ),
                completion=replay_completion,
                checkpoints=checkpoints,
                memory=_Memory(),
                telemetry=make_telemetry(),
            )
            with pytest.raises(AgentCheckpointConflictError):
                await replay_loop.run(
                    _context(),
                    conflicting_config,
                    tool_context=_tool_context(turn_id),
                )
            assert replay_completion.calls == 0

    asyncio.run(scenario())


def test_visible_stream_delta_failure_interrupts_generation_before_checkpoint() -> None:
    """@brief provider 已输出 old 后失败时 generation 中断且不形成 checkpoint / A provider failure after emitting old interrupts the generation without checkpointing."""

    async def scenario() -> None:
        """@brief 执行 first-step visible partial failure / Exercise a visible partial failure on the first step."""

        order: list[str] = []
        completion = _VisibleThenFailingCompletion()
        projection = _StreamProjection()
        turn_id = TurnId.new()
        observed_at = datetime.now(UTC)
        session = AssistantStreamSession(
            target=AssistantStreamTarget(
                chat_id=42,
                is_group=False,
                message_thread_id=None,
            ),
            state=AssistantStreamState.begin(
                turn_id=turn_id,
                generation=1,
                revision=0,
                emitted_at=observed_at,
            ),
            projection=projection,
        )
        await session.start()
        loop = AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_Receipts(order),
            ),
            completion=completion,
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
        )

        with pytest.raises(
            ResumableAgentInterruptedError,
            match="provider A disconnected",
        ):
            await loop.run(
                _context(),
                AgentExecutionConfig(
                    route=_route(),
                    model="model",
                    allow_tools=False,
                    working_memory_enabled=False,
                ),
                stream=session,
            )

        assert completion.calls == 1
        assert projection.frames[-1].cumulative_text == "old"
        assert order == []

    asyncio.run(scenario())


def test_provider_stream_generator_stays_in_one_asyncio_task_context() -> None:
    """@brief provider generator 的 enter、逐块推进与 close 保持在同一 Task /
    Provider generator entry, iteration, and close remain in one Task.
    """

    async def scenario() -> None:
        """@brief 执行两事件流并检查 Task identity / Run a two-event stream and inspect Task identity."""

        order: list[str] = []
        completion = _ContextBoundStreamingCompletion()
        projection = _StreamProjection()
        turn_id = TurnId.new()
        session = AssistantStreamSession(
            target=AssistantStreamTarget(
                chat_id=42,
                is_group=False,
                message_thread_id=None,
            ),
            state=AssistantStreamState.begin(
                turn_id=turn_id,
                generation=1,
                revision=0,
                emitted_at=datetime.now(UTC),
            ),
            projection=projection,
        )
        await session.start()

        response = await AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_Receipts(order),
            ),
            completion=completion,
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
        ).run(
            _context(),
            AgentExecutionConfig(
                route=_route(),
                model="model",
                allow_tools=False,
                working_memory_enabled=False,
            ),
            stream=session,
        )

        assert response.text == "stable"
        assert len(completion.consumer_tasks) == 3
        assert len({id(task) for task in completion.consumer_tasks}) == 1

    asyncio.run(scenario())


def test_steer_fence_closes_the_old_provider_stream_within_one_poll_interval() -> None:
    """@brief steer 在流中 fence 周期内关闭旧 provider generator 且不投影迟到 delta /
    A steer closes the old provider generator within one in-stream fence interval and drops late deltas.
    """

    async def scenario() -> None:
        """@brief 执行跨 200ms fence 周期的流 / Exercise a stream crossing the 200ms fence interval."""

        order: list[str] = []
        completion = _SteeredStreamingCompletion()
        generation_store = _StaleOnSecondFence()
        projection = _StreamProjection()
        turn_id = TurnId.new()
        fence = InferenceGenerationFence(
            activity_id=InferenceActivityId.for_turn(turn_id),
            turn_id=turn_id,
            claim_token=LeaseToken.new(),
            attempt=1,
            input_revision=TurnRevision.initial(),
            cause=InferenceGenerationCause.INITIAL,
        )
        session = AssistantStreamSession(
            target=AssistantStreamTarget(
                chat_id=42,
                is_group=False,
                message_thread_id=None,
            ),
            state=AssistantStreamState.begin(
                turn_id=turn_id,
                generation=1,
                revision=0,
                emitted_at=datetime.now(UTC),
            ),
            projection=projection,
        )
        await session.start()
        loop = AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_Receipts(order),
            ),
            completion=completion,
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
            generation_fence=generation_store,
        )

        with pytest.raises(StaleClaimError, match="steered"):
            await loop.run(
                _context(),
                AgentExecutionConfig(
                    route=_route(),
                    model="model",
                    allow_tools=False,
                    working_memory_enabled=False,
                ),
                tool_context=_tool_context(
                    turn_id,
                    generation_fence=fence,
                ),
                stream=session,
            )

        assert completion.closed is True
        assert projection.frames[-1].cumulative_text == "old"
        assert all(frame.cumulative_text != "oldlate" for frame in projection.frames)
        assert order == []

    asyncio.run(scenario())


def test_steer_before_tool_start_emits_no_false_failed_tool_activity() -> None:
    """@brief tool 开始边界的 steer 不生成旧代失败卡片 /
    Steering at the tool-start boundary emits no false failed-tool activity.
    """

    async def scenario() -> None:
        """@brief 在 checkpoint 后、tool started 前使 fence 失效 /
        Invalidate the fence after checkpointing and before tool started.
        """

        order: list[str] = []
        turn_id = TurnId.new()
        fence = InferenceGenerationFence(
            activity_id=InferenceActivityId.for_turn(turn_id),
            turn_id=turn_id,
            claim_token=LeaseToken.new(),
            attempt=1,
            input_revision=TurnRevision.initial(),
            cause=InferenceGenerationCause.INITIAL,
        )
        projection = _StreamProjection()
        progress = _ProgressPersistence()
        session = AssistantStreamSession(
            target=AssistantStreamTarget(
                chat_id=42,
                is_group=False,
                message_thread_id=None,
            ),
            state=AssistantStreamState.begin(
                turn_id=turn_id,
                generation=1,
                revision=0,
                emitted_at=datetime.now(UTC),
            ),
            projection=projection,
        )
        await session.start()
        loop = AgentLoop(
            runtime=AgentRuntime(
                catalog=DEFAULT_TOOL_CATALOG,
                persistence=_Receipts(order),
            ),
            completion=_Completion(
                [
                    _assistant_tool_call(
                        "",
                        call_id="provider-time-call",
                        name="get_current_time",
                        arguments={},
                    )
                ],
                order,
            ),
            checkpoints=_Checkpoints(order),
            memory=_Memory(),
            telemetry=make_telemetry(),
            generation_fence=_StaleAtFenceCall(4),
            progress=progress,
        )

        with pytest.raises(StaleClaimError, match="before tool start"):
            await loop.run(
                _context(),
                AgentExecutionConfig(route=_route(), model="model"),
                tool_context=_tool_context(turn_id, generation_fence=fence),
                stream=session,
            )

        assert all(
            frame.kind is not AssistantStreamKind.ACTIVITY
            for frame in projection.frames
        )
        assert progress.items == []
        assert all(not entry.startswith("effect:") for entry in order)

    asyncio.run(scenario())
