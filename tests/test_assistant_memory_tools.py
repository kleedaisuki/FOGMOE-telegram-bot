"""@brief Retention-backed Assistant memory tool tests / Tests for retention-backed Assistant memory tools."""

import asyncio
from datetime import UTC, datetime

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.memory.queries import (
    MemoryPageQuery,
    MemorySearchQuery,
    MemorySearchResult,
)
from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.memory.models import (
    MemoryId,
    MemoryProvenance,
    MemoryRecord,
    MemorySearchHit,
    MemorySourceKind,
)
from fogmoe_bot.infrastructure.assistant.tool_operations.dispatcher import (
    AssistantToolOperationDispatcher,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)
from fogmoe_bot.infrastructure.database.group_message_projection import (
    PostgresGroupMessageProjection,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 确定性测试时刻 / Deterministic test instant."""


class _Memory:
    """@brief 已应用 quota 的 memory reader fake / Memory-reader fake with quota already applied."""

    def __init__(self, records: tuple[MemoryRecord, ...]) -> None:
        """@brief 保存可见 records / Store visible records."""

        self.records = records

    async def count_summaries(self, owner_user_id: int) -> int:
        """@brief 返回可见摘要数 / Return the visible summary count."""

        assert owner_user_id == 7
        return sum(record.summary is not None for record in self.records)

    async def read_page(self, query: MemoryPageQuery) -> tuple[MemoryRecord, ...]:
        """@brief 返回 quota 内 record window / Return a quota-visible record window."""

        assert query.owner_user_id == 7
        records = tuple(
            record
            for record in self.records
            if not query.summaries_only or record.summary is not None
        )
        ordered = records if query.newest_first else tuple(reversed(records))
        return ordered[query.offset : query.offset + query.limit]

    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        """@brief 返回固定测试命中 / Return a deterministic test hit."""

        assert query.owner_user_id == 7
        record = self.records[0]
        return MemorySearchResult(
            (
                MemorySearchHit(
                    memory_id=record.memory_id,
                    created_at=record.created_at,
                    excerpt='[{"role": "user", "content": "secret durable fact"}]',
                ),
            )
        )


class _UnusedAdapters:
    """@brief 本测试不会触发的外部 adapters / External adapters unused by this test."""

    async def execute(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 拒绝意外 external read / Reject an unexpected external read."""

        raise AssertionError(request.tool_name)

    async def generate(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 拒绝意外 media generation / Reject unexpected media generation."""

        raise AssertionError(request.tool_name)

    async def list_packs(self, pack_name: str | None) -> JsonValue:
        """@brief 拒绝意外 sticker read / Reject an unexpected sticker read."""

        raise AssertionError(pack_name)


def _record() -> MemoryRecord:
    """@brief 构造保留 legacy ID 的 memory record / Build a memory record preserving its legacy ID."""

    memory_id = MemoryId(TurnId.new().value, legacy_value=41)
    return MemoryRecord(
        memory_id=memory_id,
        owner_user_id=7,
        provenance=MemoryProvenance(
            conversation_id=ConversationId("assistant-user:7"),
            source_kind=MemorySourceKind.LEGACY_ARCHIVE,
            source_id=memory_id.value,
            source_digest="a" * 64,
        ),
        snapshot=({"role": "user", "content": "secret durable fact"},),
        summary="durable fact",
        created_at=NOW,
    )


def _request(name: str, arguments: dict[str, JsonValue]) -> ToolEffectRequest:
    """@brief 构造只读 memory tool request / Build a read-only memory-tool request."""

    return ToolEffectRequest(
        context=ToolExecutionContext(
            turn_id=TurnId.new(),
            conversation_id=ConversationId("assistant-user:7"),
            delivery_stream_id=DeliveryStreamId("telegram:test:7"),
            user_id=7,
            chat_id=7,
            is_group=False,
            group_id=None,
            message_id=1,
        ),
        invocation_id="step:0:call:0",
        provider_call_id="provider-memory-call",
        tool_name=name,
        effect_kind=f"read.{name}",
        mutating=False,
        arguments=arguments,
        request_hash="a" * 64,
    )


def test_memory_tools_read_quota_view_and_preserve_legacy_record_identity() -> None:
    """@brief 摘要与搜索只读 retention view，legacy 数字 ID 不破坏 / Summary and search tools read the retention view while preserving legacy numeric IDs."""

    async def scenario() -> None:
        """@brief 执行两个 memory reads / Execute both memory reads."""

        record = _record()
        unused = _UnusedAdapters()
        operations = AssistantToolOperationDispatcher(
            help_text="help",
            external_reads=unused,
            generated_media=unused,
            stickers=unused,
            outbox=PostgresOutboxRepository(),
            memory=_Memory((record,)),
            groups=PostgresGroupMessageProjection(),
        )

        summaries = await operations.execute(
            _request("fetch_permanent_summaries", {"start": 1, "end": 5}),
            connection=None,
        )
        assert summaries == {
            "user_id": 7,
            "total": 1,
            "records": [
                {
                    "record_id": 41,
                    "summary": "durable fact",
                    "created_at": NOW.isoformat(),
                }
            ],
        }

        searched = await operations.execute(
            _request(
                "search_permanent_records",
                {"pattern": "secret", "limit": 5, "oldest_first": False},
            ),
            connection=None,
        )
        assert isinstance(searched, dict)
        assert searched["results"] == [
            {
                "record_id": 41,
                "created_at": NOW.isoformat(),
                "excerpt": '[{"role": "user", "content": "secret durable fact"}]',
            }
        ]

    asyncio.run(scenario())
