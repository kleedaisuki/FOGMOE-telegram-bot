"""@brief Passage 向量任务持久化状态约束测试 / Passage-vector job persistence-state constraint tests."""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""

MIGRATION_PATH = (
    PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0074_retrieval_vector_job_state.sql"
)
"""@brief 向量任务状态补强迁移 / Vector-job state-hardening migration."""

SNAPSHOT_PATH = PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql"
"""@brief PostgreSQL schema 快照 / PostgreSQL schema snapshot."""


def test_vector_state_migration_locks_preflights_then_validates_in_order() -> None:
    """@brief 迁移阻塞 writer、预检、唯一清理，再 NOT VALID/VALIDATE / Migration blocks writers, preflights, uniquely cleans, then uses NOT VALID/VALIDATE."""

    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    for constraint in (
        "retrieval_passage_vectors_lease_ck",
        "retrieval_passage_vectors_result_ck",
        "retrieval_passage_vectors_error_ck",
    ):
        assert f"ADD CONSTRAINT {constraint}" in migration
        assert f"VALIDATE CONSTRAINT {constraint}" in migration
    assert migration.count("NOT VALID") == 3
    up = migration.split("-- migrate:down", maxsplit=1)[0]
    lock_index = up.index(
        "LOCK TABLE retrieval.passage_vectors IN SHARE ROW EXCLUSIVE MODE"
    )
    preflight_index = up.index("DO $vector_job_preflight$")
    cleanup_index = up.index("UPDATE retrieval.passage_vectors")
    alter_index = up.index("ALTER TABLE retrieval.passage_vectors")
    validate_index = up.index("VALIDATE CONSTRAINT")
    assert lock_index < preflight_index < cleanup_index < alter_index < validate_index
    assert "SET LOCAL lock_timeout" in up
    assert "SET LOCAL statement_timeout" in up
    assert "SELECT count(*) INTO incompatible_count" in up
    assert "RAISE EXCEPTION USING" in up
    assert "HINT =" in up
    assert "SELECT passage_id" not in up
    for audited_shape in (
        "status = 'processing'",
        "claim_token IS NULL OR lease_expires_at IS NULL",
        "status = 'completed'",
        "embedding IS NULL OR completed_at IS NULL",
        "status <> 'completed'",
        "vector_norm(embedding) = 0",
        "passage.format_version <> space.passage_format_version",
        "status IN ('retry_wait', 'failed_final')",
        "status IN ('pending', 'processing', 'completed')",
        "status = 'pending' AND attempt_count <> 0",
        "version < attempt_count",
        "created_at > updated_at",
        "next_attempt_at IS DISTINCT FROM created_at",
        "lease_expires_at <= updated_at",
        "next_attempt_at < updated_at",
        "completed_at IS DISTINCT FROM updated_at",
    ):
        assert audited_shape in up


def test_vector_state_migration_only_normalizes_inactive_lease_residue() -> None:
    """@brief Up migration 唯一 UPDATE 只清 non-processing lease residue / The sole up-migration UPDATE only clears non-processing lease residue."""

    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    up = migration.split("-- migrate:down", maxsplit=1)[0]
    updates = re.findall(
        r"UPDATE retrieval\.passage_vectors\s+SET\s+(.+?)\s+WHERE\s+(.+?);",
        up,
        flags=re.DOTALL,
    )
    assert len(updates) == 1
    set_clause, where_clause = updates[0]
    assert set_clause.strip() == "claim_token = NULL, lease_expires_at = NULL"
    assert "status <> 'processing'" in where_clause
    for forbidden in (
        "status =",
        "embedding =",
        "completed_at =",
        "last_error =",
        "attempt_count =",
        "version =",
        "created_at =",
        "updated_at =",
    ):
        assert forbidden not in set_clause
    assert "DELETE FROM retrieval.passage_vectors" not in up
    assert "2 * attempt_count" not in up
    assert "% 2" not in up


def test_schema_snapshot_has_exhaustive_nullable_state_shapes() -> None:
    """@brief 快照禁止半 lease、半 result 与跨状态 error / Snapshot forbids partial leases, partial results, and cross-state errors."""

    snapshot = SNAPSHOT_PATH.read_text(encoding="utf-8")
    table = snapshot.split("CREATE TABLE retrieval.passage_vectors (", maxsplit=1)[
        1
    ].split(");", maxsplit=1)[0]
    assert "status <> 'processing'" in table
    assert "claim_token IS NULL" in table
    assert "lease_expires_at IS NULL" in table
    assert "status <> 'completed'" in table
    assert "embedding IS NULL" in table
    assert "completed_at IS NULL" in table
    assert "status IN ('retry_wait', 'failed_final')" in table
    assert "char_length(btrim(last_error)) > 0" in table
    assert "status IN ('pending', 'processing', 'completed')" in table
