"""@brief Conversation workflow 跨 adapter 原子边界测试 / Cross-adapter atomic-boundary tests for Conversation workflow."""

import asyncio
from typing import Any

from conversation_workflow_testkit import (
    NOW,
    PROJECT_ROOT,
    _TransactionContext,
)

from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.conversation_workflow.inbox import (
    PostgresInboxRepository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)


def test_inbound_and_outbound_lease_recovery_are_isolated(monkeypatch: Any) -> None:
    """@brief inbox/outbox 租约恢复具有独立接口和事务 / Inbox and outbox lease recovery have isolated interfaces and transactions."""

    statements: list[str] = []
    connection = object()

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object | None = None,
    ) -> int:
        """@brief 捕获独立恢复更新 / Capture isolated recovery updates."""

        statements.append(sql)
        return 1

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 捕获 outbox 恢复查询 / Capture the outbox recovery query."""

        statements.append(sql)
        return []

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    inbox = PostgresInboxRepository()
    outbox = PostgresOutboxRepository()

    inbound_count = asyncio.run(inbox.recover_expired_inbound_leases(now=NOW))
    outbound_count = asyncio.run(outbox.recover_expired_outbound_leases(now=NOW))

    assert inbound_count == 1
    assert outbound_count == 0
    assert "conversation.inbound_updates" in statements[0]
    assert "outbound_messages" not in statements[0]
    assert "conversation.outbound_messages" in statements[1]
    assert "inbound_updates" not in statements[1]


def test_migration_and_schema_snapshot_expose_workflow_invariants() -> None:
    """@brief migration 与 schema snapshot 暴露同一工作流不变量 / Migration and schema snapshot expose the same workflow invariants."""

    workflow_migration = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0016_add_conversation_workflow.sql"
    ).read_text(encoding="utf-8")
    activity_migration = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0018_inference_activities.sql"
    ).read_text(encoding="utf-8")
    standalone_migration = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0019_standalone_outbox.sql"
    ).read_text(encoding="utf-8")
    migration = workflow_migration + activity_migration + standalone_migration
    snapshot = (PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )

    for table in (
        "conversation.inbound_updates",
        "conversation.conversation_turns",
        "conversation.inference_activities",
        "conversation.conversation_messages",
        "conversation.outbound_messages",
    ):
        assert f"CREATE TABLE {table}" in migration
        assert f"CREATE TABLE {table}" in snapshot
    assert "UNIQUE (conversation_id, sequence)" in migration
    assert "delivery_stream_id,\n    stream_sequence" in migration
    assert "claim_token UUID" in migration
    assert "lease_expires_at TIMESTAMPTZ" in migration
    assert "next_attempt_at TIMESTAMPTZ" in migration
    assert "ALTER COLUMN turn_id DROP NOT NULL" in standalone_migration
    assert "WHERE turn_id IS NOT NULL" in standalone_migration
    assert (
        "turn_id UUID REFERENCES conversation.conversation_turns(turn_id)" in snapshot
    )
    assert "CREATE TABLE conversation.conversation_history_resets" in snapshot
