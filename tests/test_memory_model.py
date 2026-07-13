"""@brief 长期记忆领域模型测试 / Long-term-memory domain-model tests."""

from datetime import UTC, datetime
from uuid import UUID

from fogmoe_bot.domain.conversation.identity import ConversationId
from fogmoe_bot.domain.memory.models import (
    MemoryId,
    MemoryProvenance,
    MemoryRecord,
    MemorySourceKind,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 确定性测试时刻 / Deterministic test time."""


def test_empty_legacy_archive_and_external_identity_are_lossless() -> None:
    """@brief 空旧 archive 与数字 ID 可无损表达 / Empty legacy archives and numeric IDs remain lossless."""

    memory_id = MemoryId(
        UUID("00000000-0000-0000-0000-000000000009"),
        legacy_value=9,
    )
    record = MemoryRecord(
        memory_id=memory_id,
        owner_user_id=7,
        provenance=MemoryProvenance(
            conversation_id=ConversationId("assistant-user:7"),
            source_kind=MemorySourceKind.LEGACY_ARCHIVE,
            source_id=memory_id.value,
            source_digest="a" * 64,
        ),
        snapshot=(),
        summary=None,
        created_at=NOW,
    )

    assert record.snapshot == ()
    assert record.memory_id.external_value == 9
