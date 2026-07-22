"""@brief Canonical Assistant message V2 数据迁移的静态契约 / Static contract for the canonical Assistant-message V2 data migration."""

from __future__ import annotations

from pathlib import Path

from fogmoe_dbctl.migrations import runner

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""

_SQL_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0068_canonical_assistant_messages.sql"
)
"""@brief 0068 PostgreSQL 数据迁移 / 0068 PostgreSQL data migration."""

_VERSION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/versions/0068_canonical_assistant_messages.py"
)
"""@brief 0068 Alembic revision / 0068 Alembic revision."""


def test_0068_canonical_message_migration_is_atomic_and_fail_closed() -> None:
    """@brief 锁定 0068 的转换范围、drain gate 与不可逆语义 / Lock 0068 conversion scope, drain gates, and irreversibility.

    @return None / None.
    """

    version = _VERSION_PATH.read_text(encoding="utf-8")
    sections = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)
    upgrade = sections["up"]
    downgrade = sections["down"]

    assert 'revision = "0068_canonical_assistant_messages"' in version
    assert 'down_revision = "0067_close_schema_creator_and_default_gaps"' in version
    assert "conversation.inference_activities" in upgrade
    assert "assistant.tool_effect_receipts" in upgrade
    assert "context_window.compactions" in upgrade
    assert "requires a drained inference queue" in upgrade
    assert "requires drained tool effects" in upgrade
    assert "requires a drained compaction queue" in upgrade
    assert "history_messages" in upgrade
    assert "model_message" in upgrade
    assert "runtime_events" in upgrade
    assert "assistant_message" in upgrade
    assert "conversation.canonical_row_message_v2" in upgrade
    assert "conversation.require_canonical_message_v2" in upgrade
    assert "IF legacy_message ? 'schema_version' THEN" in upgrade
    assert "WHERE message.role <> 'system'" in upgrade
    assert "row_fallback" in upgrade
    assert "history_format', 'canonical-v2'" in upgrade
    assert "projection_version = 2" not in upgrade
    assert "SET compaction_id =" not in upgrade
    assert "WHERE compaction.compaction_id = converted.compaction_id" in upgrade
    assert "context_window.canonical_json_v2" in upgrade
    assert "malformed legacy tool arguments" in upgrade
    assert "must decode to a JSON object" in upgrade
    assert "normalized_arguments := arguments_value" in upgrade
    assert "jsonb_typeof(normalized_arguments) IS DISTINCT FROM 'object'" in upgrade
    assert "assistant.tool_agent_steps" not in upgrade
    assert "irreversible" in downgrade


def test_0068_sql_is_splitter_safe_for_plpgsql_and_canonical_json() -> None:
    """@brief 验证 0068 的函数体不会被 SQL splitter 错拆 / Verify 0068 function bodies are not split incorrectly by the SQL splitter.

    @return None / None.
    """

    sections = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)
    statements = runner._split_sql_statements(sections["up"])

    assert len(statements) >= 17
    assert any(
        "CREATE FUNCTION conversation.canonical_message_v2" in statement
        for statement in statements
    )
    assert any(
        "CREATE FUNCTION conversation.require_canonical_message_v2" in statement
        for statement in statements
    )
    assert any(
        "CREATE FUNCTION context_window.canonical_json_v2" in statement
        for statement in statements
    )
    assert any(
        "UPDATE conversation.conversation_messages" in statement
        for statement in statements
    )


def test_0068_tool_arguments_fail_closed_unless_they_decode_to_objects() -> None:
    """@brief 无论旧参数是 JSONB 还是 JSON 字符串，0068 都只接受 object / Require objects from either legacy JSONB or JSON-string tool arguments.

    @return None / None.
    """

    upgrade = runner._sections(_SQL_PATH.read_text(encoding="utf-8"), _SQL_PATH)["up"]
    helper_start = upgrade.index(
        "CREATE FUNCTION conversation.canonical_tool_arguments_v2"
    )
    helper_end = upgrade.index(
        "CREATE FUNCTION conversation.canonical_content_parts_v2",
        helper_start,
    )
    helper = upgrade[helper_start:helper_end]

    assert "IF jsonb_typeof(arguments_value) = 'string' THEN" in helper
    assert "normalized_arguments := raw_arguments::JSONB" in helper
    assert "ELSE\n    normalized_arguments := arguments_value;" in helper
    assert (
        "IF jsonb_typeof(normalized_arguments) IS DISTINCT FROM 'object' THEN"
        in helper
    )
    assert "must decode to a JSON object" in helper
    assert "USING ERRCODE = '22023'" in helper
    assert "RETURN normalized_arguments" in helper
