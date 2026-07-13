"""@brief Memory reader 的有界检索测试 / Bounded-search tests for the memory reader."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fogmoe_bot.application.memory.queries import MemoryPageQuery, MemorySearchQuery
from fogmoe_bot.domain.conversation.identity import ConversationId
from fogmoe_bot.domain.memory.models import (
    MemoryId,
    MemoryProvenance,
    MemoryRecord,
    MemorySourceKind,
)
from fogmoe_bot.infrastructure.database.memory import PostgresMemoryReader


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 确定性测试时间 / Deterministic test time."""

MEMORY_ID = MemoryId(UUID("00000000-0000-0000-0000-000000000041"))
"""@brief 测试 Memory ID / Test memory identity."""


class _Reader(PostgresMemoryReader):
    """@brief 以固定 record 替代数据库 page read / Replace database page reads with a fixed record."""

    async def read_page(self, query: MemoryPageQuery) -> tuple[MemoryRecord, ...]:
        """@brief 返回包含 literal bracket 的 record / Return a record containing a literal bracket.

        @param query 已校验查询 / Validated query.
        @return 单条 record / One record.
        """

        assert query.owner_user_id == 7
        return (
            MemoryRecord(
                memory_id=MEMORY_ID,
                owner_user_id=7,
                provenance=MemoryProvenance(
                    conversation_id=ConversationId("assistant-user:7"),
                    source_kind=MemorySourceKind.LEGACY_ARCHIVE,
                    source_id=MEMORY_ID.value,
                    source_digest="a" * 64,
                ),
                snapshot=({"role": "user", "content": "literal [ value"},),
                summary="summary",
                created_at=NOW,
            ),
        )


def test_invalid_regex_preserves_literal_fallback_contract() -> None:
    """@brief 非法 regex 降级为 literal 并返回稳定 warning / Invalid regex falls back to a literal with a stable warning."""

    result = asyncio.run(
        _Reader().search(MemorySearchQuery(owner_user_id=7, pattern="[", limit=5))
    )

    assert result.warning == "Invalid regex; matched as literal text"
    assert [hit.memory_id for hit in result.hits] == [MEMORY_ID]
