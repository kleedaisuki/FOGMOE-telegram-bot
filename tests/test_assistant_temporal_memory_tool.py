"""@brief Assistant 独立时间记忆工具的边界、隔离与重放测试 / Boundary, isolation, and replay tests for the standalone temporal-memory tool."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from fogmoe_bot.application.assistant.temporal_memory import (
    TemporalMemoryPassage,
    TemporalMemoryQuery,
)
from fogmoe_bot.application.assistant.tool_runtime import (
    AgentRuntime,
    PersistedToolResult,
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.assistant.tools.catalog import (
    DEFAULT_TOOL_CATALOG,
    InvalidToolArguments,
    ToolResultResidency,
    ValidatedToolInvocation,
)
from fogmoe_bot.application.timekeeping.service import TimeService
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.domain.retrieval import RetrievalScope
from fogmoe_bot.domain.temporal import TimeZoneId
from fogmoe_bot.infrastructure.assistant.tool_operations.temporal_memory import (
    search_memory_by_time,
)

ANCHOR = datetime(2032, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 定点检索的确定性 UTC 锚 / Deterministic UTC anchor for point retrieval."""


class _Memory:
    """@brief 记录可信时间 Query 并返回固定 passages / Record trusted temporal queries and return fixed passages."""

    def __init__(self, passages: tuple[TemporalMemoryPassage, ...]) -> None:
        """@brief 保存返回值并初始化调用日志 / Store results and initialize the call log.

        @param passages 固定时间检索结果 / Fixed temporal-retrieval results.
        """

        self.passages = passages
        self.queries: list[TemporalMemoryQuery] = []

    async def search(
        self, query: TemporalMemoryQuery
    ) -> tuple[TemporalMemoryPassage, ...]:
        """@brief 记录 Query 并保序返回 passages / Record the query and return passages without reordering.

        @param query 已验证的时间记忆 Query / Validated temporal-memory query.
        @return 固定 passages / Fixed passages.
        """

        self.queries.append(query)
        return self.passages


def _request(
    arguments: dict[str, object],
    *,
    group_id: int | None = None,
) -> ToolEffectRequest:
    """@brief 构造独立时间记忆工具请求 / Build a standalone temporal-memory tool request.

    @param arguments 已通过 catalog 的参数 / Catalog-validated arguments.
    @param group_id 可选当前群 ID / Optional current group identifier.
    @return 完整工具请求 / Complete tool request.
    """

    return ToolEffectRequest(
        context=ToolExecutionContext(
            turn_id=TurnId.new(),
            conversation_id=ConversationId(
                "assistant-user:7"
                if group_id is None
                else f"assistant-group:{group_id}"
            ),
            delivery_stream_id=DeliveryStreamId("telegram:user:7"),
            user_id=7,
            chat_id=7 if group_id is None else group_id,
            is_group=group_id is not None,
            group_id=group_id,
            message_id=9,
        ),
        invocation_id="step:0:call:0",
        provider_call_id="provider-temporal-memory-call",
        tool_name="search_memory_by_time",
        effect_kind="read.search_memory_by_time",
        mutating=False,
        arguments=cast(dict[str, JsonValue], arguments),
        request_hash="d" * 64,
    )


def _time_service() -> TimeService:
    """@brief 构造以上海为默认时区的解析服务 / Build a parser defaulting to the Shanghai time zone.

    @return 时间服务 / Time service.
    """

    return TimeService(default_time_zone=TimeZoneId("Asia/Shanghai"))


def test_interval_search_is_half_open_scoped_and_provenance_bearing() -> None:
    """@brief 区间检索使用 [start,end) 且个人域只来自上下文 / Interval retrieval uses [start,end) and derives personal scope only from context."""

    async def scenario() -> None:
        """@brief 解析上海本地日期区间并检查下游 Query / Parse a Shanghai-local interval and inspect the downstream query."""

        passage = TemporalMemoryPassage(
            passage_id=UUID("00000000-0000-0000-0000-000000000040"),
            source_kind="conversation.turn",
            source_id=UUID("00000000-0000-0000-0000-000000000041"),
            occurred_at=ANCHOR,
            content="User: 我喜欢红茶\nAssistant: 记住啦。",
        )
        memory = _Memory((passage,))
        result = await search_memory_by_time(
            _request(
                {
                    "start_time": "2032-01-02T00:00:00",
                    "end_time": "2032-01-03T00:00:00",
                    "timezone": "Asia/Shanghai",
                    "limit": 3,
                }
            ),
            memory=memory,
            time=_time_service(),
        )

        assert len(memory.queries) == 1
        query = memory.queries[0]
        assert query.scope == RetrievalScope("personal", 7)
        assert query.limit == 3
        assert query.nearest_to is None
        assert query.occurred_within.start == datetime(2032, 1, 1, 16, tzinfo=UTC)
        assert query.occurred_within.end == datetime(2032, 1, 2, 16, tzinfo=UTC)
        assert query.occurred_within.contains(query.occurred_within.start)
        assert not query.occurred_within.contains(query.occurred_within.end)
        assert result == {
            "scope": {"kind": "personal", "id": 7},
            "temporal": {
                "kind": "interval",
                "semantics": "[start,end)",
                "timezone": "Asia/Shanghai",
                "start_utc": "2032-01-01T16:00:00Z",
                "end_utc": "2032-01-02T16:00:00Z",
            },
            "ranking": "latest",
            "trust": "untrusted_historical_data",
            "results": [
                {
                    "passage_id": "00000000-0000-0000-0000-000000000040",
                    "source_kind": "conversation.turn",
                    "source_id": "00000000-0000-0000-0000-000000000041",
                    "occurred_at": "2032-01-02T03:04:00Z",
                    "content": "User: 我喜欢红茶\nAssistant: 记住啦。",
                }
            ],
        }

    asyncio.run(scenario())


def test_around_search_derives_current_group_and_preserves_nearest_order() -> None:
    """@brief 定点检索派生当前群域并保留稳定最近优先顺序 / Point retrieval derives the current group and preserves stable nearest-first order."""

    async def scenario() -> None:
        """@brief 解析本地锚点并检查结果距离 / Resolve a local anchor and inspect result distances."""

        passages = (
            TemporalMemoryPassage(
                passage_id=UUID("00000000-0000-0000-0000-000000000050"),
                source_kind="conversation.turn",
                source_id=UUID("00000000-0000-0000-0000-000000000051"),
                occurred_at=ANCHOR + timedelta(minutes=1),
                content="after anchor",
                temporal_distance_seconds=60,
            ),
            TemporalMemoryPassage(
                passage_id=UUID("00000000-0000-0000-0000-000000000052"),
                source_kind="conversation.turn",
                source_id=UUID("00000000-0000-0000-0000-000000000053"),
                occurred_at=ANCHOR - timedelta(minutes=1),
                content="before anchor",
                temporal_distance_seconds=60,
            ),
        )
        memory = _Memory(passages)
        result = await search_memory_by_time(
            _request(
                {
                    "around_time": "2032-01-02T11:04:00",
                    "around_radius_minutes": 15,
                    "timezone": "Asia/Shanghai",
                    "limit": 2,
                },
                group_id=-10042,
            ),
            memory=memory,
            time=_time_service(),
        )

        assert len(memory.queries) == 1
        query = memory.queries[0]
        assert query.scope == RetrievalScope("group", -10042)
        assert query.nearest_to == ANCHOR
        assert query.occurred_within.start == ANCHOR - timedelta(minutes=15)
        assert query.occurred_within.end == ANCHOR + timedelta(minutes=15)
        assert result["scope"] == {"kind": "group", "id": -10042}
        assert result["ranking"] == "nearest"
        assert result["temporal"] == {
            "kind": "around",
            "semantics": "[start,end)",
            "timezone": "Asia/Shanghai",
            "start_utc": "2032-01-02T02:49:00Z",
            "end_utc": "2032-01-02T03:19:00Z",
            "anchor_utc": "2032-01-02T03:04:00Z",
            "radius_minutes": 15,
        }
        assert [item["passage_id"] for item in result["results"]] == [
            "00000000-0000-0000-0000-000000000050",
            "00000000-0000-0000-0000-000000000052",
        ]
        assert [item["temporal_distance_seconds"] for item in result["results"]] == [
            60.0,
            60.0,
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"start_time": "2032-01-02T00:00:00Z"},
        {"end_time": "2032-01-03T00:00:00Z"},
        {
            "start_time": "2032-01-02T00:00:00Z",
            "end_time": "2032-01-03T00:00:00Z",
            "around_time": "2032-01-02T12:00:00Z",
        },
        {
            "start_time": "2032-01-02T00:00:00Z",
            "end_time": "2032-01-03T00:00:00Z",
            "around_radius_minutes": 15,
        },
        {"around_radius_minutes": 15},
        {"around_time": "2032-01-02T12:00:00Z", "around_radius_minutes": 0},
        {
            "around_time": "2032-01-02T12:00:00Z",
            "around_radius_minutes": 10_081,
        },
        {
            "around_time": "2032-01-02T12:00:00Z",
            "query": "this belongs to semantic search_memory",
        },
        {
            "around_time": "2032-01-02T12:00:00Z",
            "scope": {"kind": "personal", "id": 999},
        },
    ),
)
def test_catalog_rejects_ambiguous_or_authority_bearing_arguments(
    arguments: dict[str, object],
) -> None:
    """@brief Catalog 拒绝含糊模式、越界半径和模型伪造 scope / The catalog rejects ambiguous modes, radius violations, and model-supplied scope."""

    assert isinstance(
        DEFAULT_TOOL_CATALOG.validate("search_memory_by_time", arguments),
        InvalidToolArguments,
    )


@pytest.mark.parametrize(
    "arguments",
    (
        {
            "start_time": "2032-01-02T00:00:00",
            "end_time": "2032-01-03T00:00:00",
            "timezone": "Asia/Shanghai",
            "limit": 5,
        },
        {"around_time": "2032-01-02T12:00:00Z"},
        {
            "around_time": "2032-01-02T12:00:00Z",
            "around_radius_minutes": 10_080,
            "limit": 128,
        },
    ),
)
def test_catalog_exposes_two_bounded_turn_local_search_modes(
    arguments: dict[str, object],
) -> None:
    """@brief Catalog 暴露两种有界只读模式且结果可 receipt 重放 / The catalog exposes two bounded read modes with receipt-replayable results."""

    invocation = DEFAULT_TOOL_CATALOG.validate("search_memory_by_time", arguments)

    assert isinstance(invocation, ValidatedToolInvocation)
    assert invocation.mutating is False
    assert invocation.effect_kind == "read.search_memory_by_time"
    assert invocation.result_residency is ToolResultResidency.AGENT_TURN
    assert invocation.result_cacheable is True
    if "around_time" in arguments and "around_radius_minutes" not in arguments:
        assert invocation.arguments.model_dump()["around_radius_minutes"] == 60


def test_temporal_memory_invocation_replays_but_remains_agent_turn_local() -> None:
    """@brief 同一 invocation 从 receipt 重放且不会驻留后续对话 / One invocation replays from its receipt while remaining local to the Agent turn."""

    class _Receipts:
        """@brief 模拟一次执行后稳定重放的 receipt port / Simulate a receipt port that executes once and then replays."""

        def __init__(self) -> None:
            """@brief 初始化 receipt 状态 / Initialize receipt state."""

            self.requests: list[ToolEffectRequest] = []
            self.result: JsonValue | None = None

        async def execute(self, request: ToolEffectRequest) -> PersistedToolResult:
            """@brief 按稳定请求哈希返回首次或重放结果 / Return an initial or replayed result by stable request hash.

            @param request 工具效果请求 / Tool-effect request.
            @return 首次或重放结果 / Initial or replayed result.
            """

            self.requests.append(request)
            assert request.result_cacheable is True
            if self.result is not None:
                return PersistedToolResult(self.result, True)
            self.result = {"results": [{"content": "stable historical fact"}]}
            return PersistedToolResult(self.result, False)

    async def scenario() -> None:
        """@brief 以不同 provider ID 重放同一 ordinal / Replay one ordinal under different provider identifiers."""

        receipts = _Receipts()
        runtime = AgentRuntime(catalog=DEFAULT_TOOL_CATALOG, persistence=receipts)
        context = _request(
            {
                "start_time": "2032-01-02T00:00:00Z",
                "end_time": "2032-01-03T00:00:00Z",
            }
        ).context
        raw_arguments = {
            "start_time": "2032-01-02T00:00:00Z",
            "end_time": "2032-01-03T00:00:00Z",
            "limit": 3,
        }
        first = await runtime.execute(
            context=context,
            step=2,
            ordinal=1,
            provider_call_id="provider-temporal-a",
            tool_name="search_memory_by_time",
            raw_arguments=raw_arguments,
        )
        replay = await runtime.execute(
            context=context,
            step=2,
            ordinal=1,
            provider_call_id="provider-temporal-b",
            tool_name="search_memory_by_time",
            raw_arguments=raw_arguments,
        )

        assert first.invocation_id == replay.invocation_id == "step:2:call:1"
        assert len(receipts.requests) == 2
        assert receipts.requests[0].request_hash == receipts.requests[1].request_hash
        assert first.replayed is False and replay.replayed is True
        assert first.public_result == replay.public_result
        assert (
            first.result_residency
            is replay.result_residency
            is ToolResultResidency.AGENT_TURN
        )

    asyncio.run(scenario())
