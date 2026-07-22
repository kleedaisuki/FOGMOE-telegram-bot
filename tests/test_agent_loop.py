"""@brief 可恢复 Agent loop 测试 / Tests for the resumable Agent loop."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import cast
from uuid import uuid4

import pytest
from observability_testkit import make_telemetry

from fogmoe_bot.application.assistant.agent_loop import AgentExecutionConfig, AgentLoop
from fogmoe_bot.application.assistant.completion import (
    AgentCheckpointConflictError,
    AgentStepCheckpoint,
    AssistantCompletion,
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
    TurnId,
)
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

        self.values: dict[tuple[TurnId, int], AgentStepCheckpoint] = {}
        self.order = order

    async def load_step(
        self, turn_id: TurnId, step_no: int
    ) -> AgentStepCheckpoint | None:
        """@brief 读取 checkpoint / Load a checkpoint.

        @param turn_id 回合标识 / Turn identifier.
        @param step_no 模型步骤序号 / Model step number.
        @return checkpoint 或 None / Checkpoint or None.
        """

        return self.values.get((turn_id, step_no))

    async def save_step(self, checkpoint: AgentStepCheckpoint) -> AgentStepCheckpoint:
        """@brief 保存 checkpoint / Save a checkpoint.

        @param checkpoint 待保存 checkpoint / Checkpoint to save.
        @return 已保存 checkpoint / Saved checkpoint.
        """

        self.order.append(f"checkpoint:{checkpoint.step_no}")
        return self.values.setdefault(
            (checkpoint.turn_id, checkpoint.step_no), checkpoint
        )


class _Completion:
    """@brief 队列 completion port / Queue-backed completion port."""

    def __init__(self, values: list[AssistantCompletion], order: list[str]) -> None:
        """@brief 保存 responses / Store responses.

        @param values 按调用顺序返回的 completion / Completions returned in call order.
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
            }
        )
        self.order.append(f"provider:{self.calls}")
        self.calls += 1
        if not self.values:
            raise AssertionError("checkpoint replay called provider")
        return self.values.pop(0)


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
) -> ToolExecutionContext:
    """@brief 构造 durable tool context / Build durable tool context.

    @param turn_id durable Turn ID / Durable turn identifier.
    @param allowed_tools 可选 Turn 工具 allowlist / Optional turn tool allowlist.
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
                "WorkingMemory is freshly retrieved" in message.text
                for message in cast(
                    tuple[CanonicalMessage, ...], request["messages"]
                )
            )
            == 1
            for request in first_completion.requests
        )

        checkpoint_completion = checkpoints.values[(turn_id, 0)].completion
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
            message for message in post_tool_messages if message.role is MessageRole.TOOL
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

        assert response.history_messages == (text_message(MessageRole.ASSISTANT, "answer"),)
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
            and part.to_json()["result"] == {"results": [{"content": "private recalled text"}]}
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
        assert dict(cast(RequestMeta, request["request_meta"])) == {
            "trace_id": "first"
        }

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
