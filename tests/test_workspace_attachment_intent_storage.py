"""@brief Workspace 附件 intent 数据库类型适配测试 / Tests for Workspace attachment-intent database type adaptation."""

from uuid import UUID

from fogmoe_bot.infrastructure.database.workspace_attachment_intents import (
    _database_text,
)


def test_database_text_normalizes_postgresql_uuid_values() -> None:
    """@brief 将 asyncpg UUID 结果规范化为文本 / Normalize an asyncpg UUID result to text.

    @return None / None.
    @note PostgreSQL 的 UUID 列不会保证通过 asyncpg 以字符串返回；该回归测试保护 intent
        从数据库读回后的 aggregate 重建路径。/ PostgreSQL UUID columns are not guaranteed
        to arrive as strings through asyncpg; this regression test protects aggregate restoration.
    """

    value = UUID("d7078fd2-6fbd-5328-bdba-77883cc0b50f")

    assert _database_text(value, label="source_message_id") == str(value)
