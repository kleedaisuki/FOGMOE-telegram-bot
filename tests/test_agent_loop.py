"""@brief 可恢复 Agent loop 测试 / Tests for the resumable Agent loop."""

import asyncio
from uuid import uuid4
from observability_testkit import make_telemetry

from fogmoe_bot.application.assistant.agent_loop import AgentExecutionConfig, AgentLoop
from fogmoe_bot.application.assistant.completion import (
    AgentStepCheckpoint,
    AssistantCompletion,
    CompletionToolCall,
)
from fogmoe_bot.application.assistant.tool_runtime import (
    AgentRuntime,
    PersistedToolResult,
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.assistant.tools.catalog import DEFAULT_TOOL_CATALOG
from fogmoe_bot.application.memory.ports import WorkingMemoryQuery
from fogmoe_bot.domain.context import ContextState, ConversationScope, UserState
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.memory import WorkingMemory


class _Checkpoints:
    """@brief 内存 checkpoint port / In-memory checkpoint port."""

    def __init__(self, order: list[str]) -> None:
        """@brief 保存共享顺序日志 / Store a shared order log."""

        self.values: dict[tuple[TurnId, int], AgentStepCheckpoint] = {}
        self.order = order

    async def load_step(
        self, turn_id: TurnId, step_no: int
    ) -> AgentStepCheckpoint | None:
        """@brief 读取 checkpoint / Load a checkpoint."""

        return self.values.get((turn_id, step_no))

    async def save_step(self, checkpoint: AgentStepCheckpoint) -> AgentStepCheckpoint:
        """@brief 保存 checkpoint / Save a checkpoint."""

        self.order.append(f"checkpoint:{checkpoint.step_no}")
        return self.values.setdefault(
            (checkpoint.turn_id, checkpoint.step_no), checkpoint
        )


class _Completion:
    """@brief 队列 completion port / Queue-backed completion port."""

    def __init__(self, values: list[AssistantCompletion], order: list[str]) -> None:
        """@brief 保存 responses / Store responses."""

        self.values = values
        self.calls = 0
        self.order = order
        self.requests: list[dict[str, object]] = []

    async def complete(self, **kwargs: object) -> AssistantCompletion:
        """@brief 返回下一个 response / Return the next response."""

        self.requests.append(kwargs)
        self.order.append(f"provider:{self.calls}")
        self.calls += 1
        if not self.values:
            raise AssertionError("checkpoint replay called provider")
        return self.values.pop(0)


class _Memory:
    """@brief 记录每次 fresh WorkingMemory 查询 / Record every fresh WorkingMemory query."""

    def __init__(self) -> None:
        """@brief 初始化查询日志 / Initialize the query log."""

        self.queries: list[WorkingMemoryQuery] = []

    async def retrieve(self, query: WorkingMemoryQuery) -> WorkingMemory:
        """@brief 返回空但有作用域的工作记忆 / Return empty scoped working memory."""

        self.queries.append(query)
        return WorkingMemory(scope=query.scope, query=query.text, messages=())


class _Receipts:
    """@brief 幂等 receipt port / Idempotent receipt port."""

    def __init__(self, order: list[str]) -> None:
        """@brief 初始化 receipt map / Initialize receipt map."""

        self.values: dict[tuple[str, str], object] = {}
        self.mutation_count = 0
        self.order = order

    async def execute(self, request: ToolEffectRequest) -> PersistedToolResult:
        """@brief 首次 mutation，随后 replay / Mutate once and replay thereafter."""

        key = (request.invocation_id, request.effect_kind)
        if key in self.values:
            return PersistedToolResult(self.values[key], True)  # type: ignore[arg-type]
        self.order.append(f"effect:{request.invocation_id}")
        self.mutation_count += 1
        result = {"status": "updated"}
        self.values[key] = result
        return PersistedToolResult(result, False)


class _FreshMemoryTool:
    """@brief 验证 Memory tool 使用非缓存执行契约 / Verify the Memory tool uses the non-cacheable execution contract."""

    def __init__(self) -> None:
        """@brief 初始化请求日志 / Initialize the request log."""

        self.requests: list[ToolEffectRequest] = []

    async def execute(self, request: ToolEffectRequest) -> PersistedToolResult:
        """@brief 返回仅当前回合可见的敏感文本 / Return sensitive text visible only in this turn."""

        self.requests.append(request)
        assert request.result_cacheable is False
        return PersistedToolResult(
            {"results": [{"content": "private recalled text"}]},
            False,
        )


def _context() -> ContextState:
    """@brief 构造模型上下文 / Build model context."""

    return ContextState(
        context_id=uuid4(),
        scope=ConversationScope(user_id=42),
        user_state=UserState(coins=10, plan="free", permission=0, profile=None),
        messages=[{"role": "user", "content": "remember me"}],
        tool_context={},
    )


def _tool_context(turn_id: TurnId) -> ToolExecutionContext:
    """@brief 构造 durable tool context / Build durable tool context."""

    return ToolExecutionContext(
        turn_id=turn_id,
        conversation_id=ConversationId("assistant-user:42"),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
        user_id=42,
        chat_id=42,
        is_group=False,
        group_id=None,
        message_id=1,
    )


def test_checkpoint_precedes_effect_and_restart_replays_without_provider_or_mutation() -> (
    None
):
    """@brief kill-9 replay 使用相同 plan/receipt / Kill-9 replay uses the same plan and receipt."""

    async def scenario() -> None:
        """@brief 执行首次与重启场景 / Execute initial and restarted scenarios."""

        order: list[str] = []
        turn_id = TurnId.new()
        checkpoints = _Checkpoints(order)
        receipts = _Receipts(order)
        first_completion = _Completion(
            [
                AssistantCompletion(
                    "",
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "provider-call-a",
                                "type": "function",
                                "function": {
                                    "name": "user_diary",
                                    "arguments": '{"action":"append","content":"note"}',
                                },
                            }
                        ],
                    },
                    (
                        CompletionToolCall(
                            "provider-call-a",
                            "user_diary",
                            {"action": "append", "content": "note"},
                        ),
                    ),
                ),
                AssistantCompletion("done", {"role": "assistant", "content": "done"}),
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
        config = AgentExecutionConfig(provider="test", model="model", allow_tools=True)
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
        assert all(query.scope.user_id == 42 for query in first_memory.queries)
        assert all(
            sum(
                "WorkingMemory is freshly retrieved" in str(message.get("content"))
                for message in request["messages"]
            )
            == 1
            for request in first_completion.requests
        )

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
        """@brief 执行一次 Memory tool loop / Execute one Memory-tool loop."""

        order: list[str] = []
        completion = _Completion(
            [
                AssistantCompletion(
                    "",
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "memory-call",
                                "type": "function",
                                "function": {
                                    "name": "search_memory",
                                    "arguments": '{"query":"tea","limit":3}',
                                },
                            }
                        ],
                    },
                    (
                        CompletionToolCall(
                            "memory-call",
                            "search_memory",
                            {"query": "tea", "limit": 3},
                        ),
                    ),
                ),
                AssistantCompletion("answer", {"role": "assistant", "content": "answer"}),
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
            AgentExecutionConfig(provider="test", model="model", allow_tools=True),
            tool_context=_tool_context(TurnId.new()),
        )

        assert response.history_messages == ({"role": "assistant", "content": "answer"},)
        assert context.messages == [
            {"role": "user", "content": "remember me"},
            {"role": "assistant", "content": "answer"},
        ]
        assert all(event.get("ephemeral") is True for event in response.events)
        assert "private recalled text" in str(completion.requests[1]["messages"])

    asyncio.run(scenario())
