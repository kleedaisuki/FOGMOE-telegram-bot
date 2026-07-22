"""@brief 遥测资源存活性的迁移契约 / Migration contracts for telemetry-resource liveness."""

from __future__ import annotations

from pathlib import Path

from fogmoe_dbctl.migrations import runner

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""

_MIGRATION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0063_observability_resource_liveness.sql"
)
"""@brief 0063 PostgreSQL migration / 0063 PostgreSQL migration."""

_VERSION_PATH = (
    _PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/versions/0063_observability_resource_liveness.py"
)
"""@brief 0063 Alembic version / 0063 Alembic version."""

_SNAPSHOT_PATH = _PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql"
"""@brief 当前 DDL snapshot / Current DDL snapshot."""


def test_0063_backfills_every_signal_family_before_enforcing_liveness() -> None:
    """@brief 0063 先完整回填三类信号再强制不变式 / 0063 completely backfills all three signal families before enforcing invariants."""

    version = _VERSION_PATH.read_text(encoding="utf-8")
    sections = runner._sections(
        _MIGRATION_PATH.read_text(encoding="utf-8"),
        _MIGRATION_PATH,
    )
    upgrade = sections["up"]
    statements = runner._split_sql_statements(upgrade)

    assert 'revision = "0063_observability_resource_liveness"' in version
    assert 'down_revision = "0062_retire_identity_mirrors_and_legacy_media"' in version
    for marker in (
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL statement_timeout = '120s'",
        "ADD COLUMN last_seen_at TIMESTAMPTZ",
        "FROM observability.log_records",
        "FROM observability.spans",
        "FROM observability.metric_points",
        "MAX(GREATEST(occurred_at, observed_at))",
        "MAX(GREATEST(started_at, ended_at))",
        "COALESCE(resource.stopped_at, resource.started_at)",
        "observability_resources_liveness_ck",
        ") NOT VALID",
        "VALIDATE CONSTRAINT observability_resources_liveness_ck",
        "ALTER COLUMN last_seen_at SET NOT NULL",
    ):
        assert marker in upgrade

    add_column_index = next(
        index
        for index, statement in enumerate(statements)
        if "ADD COLUMN last_seen_at" in statement
    )
    backfill_index = next(
        index
        for index, statement in enumerate(statements)
        if "WITH signal_observations" in statement
    )
    validate_index = next(
        index
        for index, statement in enumerate(statements)
        if "VALIDATE CONSTRAINT" in statement
    )
    not_null_index = next(
        index
        for index, statement in enumerate(statements)
        if "ALTER COLUMN last_seen_at SET NOT NULL" in statement
    )
    assert add_column_index < backfill_index < validate_index < not_null_index
    assert "DEFAULT" not in statements[add_column_index]
    assert "last_seen_at" not in " ".join(
        statement for statement in statements if "CREATE INDEX" in statement
    )


def test_0063_snapshot_exposes_one_typed_heartbeat_contract() -> None:
    """@brief snapshot 仅保留一个非空心跳列与时间不变式 / The snapshot exposes one non-null heartbeat column and its temporal invariant."""

    snapshot = _SNAPSHOT_PATH.read_text(encoding="utf-8")

    assert "-- Alembic head: 0067_close_schema_creator_and_default_gaps" in snapshot
    assert snapshot.count("last_seen_at TIMESTAMPTZ NOT NULL") == 1
    assert snapshot.count("observability_resources_liveness_ck") == 1
    assert "last_seen_at >= started_at" in snapshot
    assert "last_seen_at >= stopped_at" in snapshot
