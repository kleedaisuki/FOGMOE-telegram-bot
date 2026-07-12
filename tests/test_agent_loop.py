"""@brief 可恢复 Agent loop 测试 / Tests for the resumable Agent loop."""

import asyncio
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
from fogmoe_bot.domain.context import ContextState, ConversationScope, UserState
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)


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

    async def complete(self, **kwargs: object) -> AssistantCompletion:
        """@brief 返回下一个 response / Return the next response."""

        del kwargs
        self.order.append(f"provider:{self.calls}")
        self.calls += 1
        if not self.values:
            raise AssertionError("checkpoint replay called provider")
        return self.values.pop(0)


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
        result = {"status": "updated", "impression": request.arguments["impression"]}
        self.values[key] = result
        return PersistedToolResult(result, False)


def _context() -> ContextState:
    """@brief 构造模型上下文 / Build model context."""

    return ContextState(
        scope=ConversationScope(user_id=42),
        user_state=UserState(coins=10, plan="free", permission=0, impression="unknown"),
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
                                    "name": "update_impression",
                                    "arguments": '{"impression":"curious"}',
                                },
                            }
                        ],
                    },
                    (
                        CompletionToolCall(
                            "provider-call-a",
                            "update_impression",
                            {"impression": "curious"},
                        ),
                    ),
                ),
                AssistantCompletion("done", {"role": "assistant", "content": "done"}),
            ],
            order,
        )
        runtime = AgentRuntime(catalog=DEFAULT_TOOL_CATALOG, persistence=receipts)
        first_loop = AgentLoop(
            runtime=runtime,
            completion=first_completion,
            checkpoints=checkpoints,
            telemetry=make_telemetry(),
        )
        config = AgentExecutionConfig(provider="test", model="model", allow_tools=True)
        first = await first_loop.run(
            _context(), config, tool_context=_tool_context(turn_id)
        )

        assert first.text == "done"
        assert order.index("checkpoint:0") < order.index("effect:step:0:call:0")
        assert receipts.mutation_count == 1

        replay_completion = _Completion([], order)
        replay_loop = AgentLoop(
            runtime=runtime,
            completion=replay_completion,
            checkpoints=checkpoints,
            telemetry=make_telemetry(),
        )
        replay = await replay_loop.run(
            _context(), config, tool_context=_tool_context(turn_id)
        )

        assert replay.text == "done"
        assert replay_completion.calls == 0
        assert receipts.mutation_count == 1
        results = [event for event in replay.events if event["type"] == "tool_result"]
        assert results[0]["replayed"] is True

    asyncio.run(scenario())
