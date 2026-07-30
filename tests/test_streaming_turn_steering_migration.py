"""@brief 0073 流式 steer 数据迁移静态契约 / Static contracts for migration 0073 streaming steer data."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root."""


def test_0073_preserves_rows_and_fences_every_generation_owned_effect() -> None:
    """@brief 0073 原位回填 revision zero，并同步约束 claim/checkpoint / 0073 backfills revision zero in place and jointly constrains claims/checkpoints."""

    migration = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0073_streaming_turn_steering.sql"
    ).read_text(encoding="utf-8")
    snapshot = (PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )

    for contract in (
        "ADD COLUMN input_revision BIGINT NOT NULL DEFAULT 0",
        "ADD COLUMN retry_budget_used INTEGER NOT NULL DEFAULT 0",
        "inference_activities_retry_budget_used_ck",
        "retry_budget_used <= attempt_count",
        "status <> 'processing'",
        "retry_budget_used < attempt_count",
        "'steer_pending'",
        "WHERE status IN ('pending', 'steer_pending', 'retry')",
        "ADD COLUMN generation BIGINT NOT NULL DEFAULT 0",
        "PRIMARY KEY (\n    turn_id,\n    generation,\n    step_no\n  )",
        "CREATE UNIQUE INDEX conversation_messages_steer_source_uq",
        "content ->> 'input_kind' = 'steer'",
        "count(*) FILTER (WHERE status IN ('pending', 'steer_pending'))",
        "WHERE status IN ('pending', 'steer_pending', 'retry')",
    ):
        assert contract in migration

    assert "DELETE FROM" not in migration.upper()
    assert "TRUNCATE " not in migration.upper()
    assert "DROP TABLE" not in migration.upper()
    assert (
        "cannot downgrade 0073: inference activities contain durable steer revisions"
        in migration
    )
    assert (
        "cannot downgrade 0073: agent checkpoints contain nonzero generations"
        in migration
    )
    assert (
        "cannot downgrade 0073: inference activities contain durable retry-budget usage"
        in migration
    )
    assert "WHERE retry_budget_used <> 0" in migration
    assert "DROP COLUMN retry_budget_used" in migration

    for contract in (
        "input_revision BIGINT NOT NULL DEFAULT 0",
        "retry_budget_used INTEGER NOT NULL DEFAULT 0",
        "inference_activities_retry_budget_used_ck",
        "retry_budget_used <= attempt_count",
        "status <> 'processing'",
        "retry_budget_used < attempt_count",
        "'steer_pending'",
        "generation BIGINT NOT NULL DEFAULT 0",
        "PRIMARY KEY (turn_id, generation, step_no)",
        "conversation_messages_steer_source_uq",
        "count(*) FILTER (WHERE status IN ('pending', 'steer_pending'))",
    ):
        assert contract in snapshot


def test_0073_gives_every_legacy_activity_a_fresh_retry_budget() -> None:
    """@brief 0073 不从有损快照猜测历史预算 / 0073 does not infer historical budget from lossy snapshots."""

    migration = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0073_streaming_turn_steering.sql"
    ).read_text(encoding="utf-8")

    assert (
        "UPDATE conversation.inference_activities\nSET retry_budget_used"
        not in migration
    )
    assert "ALTER COLUMN retry_budget_used SET DEFAULT" not in migration
    assert "ALTER COLUMN retry_budget_used SET NOT NULL" not in migration
    assert "last_error NOT LIKE" not in migration
    assert "THEN attempt_count" not in migration
    assert "every existing activity receives a fresh budget" in migration
