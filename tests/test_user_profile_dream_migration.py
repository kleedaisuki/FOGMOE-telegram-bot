"""@brief User Profile Dream 五态迁移与 schema 契约 / User Profile Dream five-state migration and schema contracts."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""

MIGRATION_PATH = (
    PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/sql/postgresql/0075_user_profile_dream_state.sql"
)
"""@brief Dream 五态补强 SQL / Dream five-state hardening SQL."""

VERSION_PATH = (
    PROJECT_ROOT
    / "src/fogmoe_dbctl/migrations/versions/0075_user_profile_dream_state.py"
)
"""@brief 0075 Alembic wrapper / 0075 Alembic wrapper."""

SNAPSHOT_PATH = PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql"
"""@brief PostgreSQL schema 快照 / PostgreSQL schema snapshot."""


def test_0075_locks_then_preflights_before_validating_exhaustive_checks() -> None:
    """@brief 0075 在有界窗口预取最终 DDL 锁，再分类预检和验证 / 0075 pre-acquires the final DDL lock in a bounded window before preflight and validation.

    @return None / None.
    """

    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    up = migration.split("-- migrate:down", maxsplit=1)[0]
    down = migration.split("-- migrate:down", maxsplit=1)[1]
    lock_index = up.index("LOCK TABLE user_profile.dreams IN ACCESS EXCLUSIVE MODE")
    preflight_index = up.index("DO $dream_state_preflight$")
    add_index = up.index("ADD CONSTRAINT user_profile_dreams_counter_ck")
    validate_index = up.index("VALIDATE CONSTRAINT user_profile_dreams_counter_ck")
    obsolete_drop_index = up.index("DROP CONSTRAINT user_profile_dreams_ready_ck")

    assert (
        lock_index < preflight_index < add_index < validate_index < obsolete_drop_index
    )
    assert "SET LOCAL lock_timeout = '10s'" in up
    assert "SET LOCAL statement_timeout = '5min'" in up
    assert up.count("SELECT count(*) INTO incompatible_count") >= 18
    assert up.count("RAISE EXCEPTION USING") >= 18
    assert up.count("HINT =") >= 18
    assert up.count("NOT VALID") == 6
    assert up.count("VALIDATE CONSTRAINT") == 6
    assert "SHARE ROW EXCLUSIVE" not in up
    assert "SET LOCAL lock_timeout = '10s'" in down
    assert "SET LOCAL statement_timeout = '5min'" in down
    assert "LOCK TABLE user_profile.dreams IN ACCESS EXCLUSIVE MODE" in down
    for constraint in (
        "user_profile_dreams_owner_ck",
        "user_profile_dreams_timestamp_range_ck",
        "user_profile_dreams_counter_ck",
        "user_profile_dreams_metadata_ck",
        "user_profile_dreams_state_ck",
        "user_profile_dreams_result_payload_ck",
    ):
        assert f"ADD CONSTRAINT {constraint}" in up
        assert f"VALIDATE CONSTRAINT {constraint}" in up


def test_0075_preflights_every_domain_restore_state_invariant() -> None:
    """@brief 分类预检覆盖 counter、五态字段和单调时间 / Categorized preflight covers counters, five-state fields, and monotonic time.

    @return None / None.
    """

    up = MIGRATION_PATH.read_text(encoding="utf-8").split(
        "-- migrate:down", maxsplit=1
    )[0]
    for audited_shape in (
        "user_id <= 0",
        "outside the Python datetime range",
        "created_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'",
        "created_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'",
        "next_attempt_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'",
        "lease_expires_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'",
        "completed_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'",
        "version <> attempt_count",
        "status = 'pending'\n    AND (version <> 0 OR attempt_count <> 0)",
        "status <> 'pending' AND attempt_count < 1",
        "created_at > updated_at",
        "status IN ('pending', 'retry_wait')",
        "claim_token IS NULL OR lease_expires_at IS NULL",
        "status IN ('completed', 'failed_final')",
        "result_patch IS NULL",
        "result_patch IS NOT NULL OR route_key IS NOT NULL",
        "jsonb_typeof(result_patch -> 'operations') = 'array'",
        "jsonb_typeof(result_patch -> 'prompt_version') = 'number'",
        "jsonb_array_length(result_patch -> 'operations') <= 64",
        "jsonb_path_exists(",
        '@.type() != "object"',
        '@.key like_regex "^[a-z][a-z0-9_.-]{0,79}$"',
        "@ != @.floor()",
        "@ > 9223372036854775807",
        "jsonb_path_query_array(",
        "$integer_ids$^\\[([1-9][0-9]*",
        "malformed result operation",
        "metadata that cannot hydrate",
        "char_length(btrim(metadata ->> 'display_name', E' ",
        "(metadata ->> 'provider') ~ '^[a-z][a-z0-9_.-]{0,31}$'",
        '@.statement like_regex ".*[^\\u0009\\u000A',
        "char_length(route_key) NOT BETWEEN 1 AND 300",
        "char_length(btrim(route_key, E' ",
        "char_length(last_error) NOT BETWEEN 1 AND 1000",
        "char_length(btrim(last_error, E' ",
        "status IN ('retry_wait', 'failed_final')",
        "status IN ('pending', 'processing', 'completed')",
        "next_attempt_at IS DISTINCT FROM created_at",
        "lease_expires_at <= updated_at",
        "next_attempt_at < updated_at",
        "completed_at IS DISTINCT FROM updated_at",
    ):
        assert audited_shape in up


def test_0075_never_rewrites_or_deletes_dream_business_data() -> None:
    """@brief 0075 up 段没有任何业务 DML / The 0075 up section contains no business DML.

    @return None / None.
    """

    up = MIGRATION_PATH.read_text(encoding="utf-8").split(
        "-- migrate:down", maxsplit=1
    )[0]
    assert re.search(r"\bUPDATE\b", up) is None
    assert re.search(r"\bDELETE\s+FROM\b", up) is None
    assert re.search(r"\bINSERT\s+INTO\b", up) is None
    assert re.search(r"\bMERGE\s+INTO\b", up) is None
    assert "SELECT dream_id" not in up


def test_0075_chain_and_schema_snapshot_expose_the_same_state_matrix() -> None:
    """@brief Alembic 链与快照共同声明五态约束 / The Alembic chain and snapshot declare the same five-state constraints.

    @return None / None.
    """

    version = VERSION_PATH.read_text(encoding="utf-8")
    snapshot = SNAPSHOT_PATH.read_text(encoding="utf-8")
    table = snapshot.split("CREATE TABLE user_profile.dreams (", maxsplit=1)[1].split(
        ");", maxsplit=1
    )[0]

    assert 'revision = "0075_user_profile_dream_state"' in version
    assert 'down_revision = "0074_retrieval_vector_job_state"' in version
    assert "-- Alembic head: 0075_user_profile_dream_state" in snapshot
    assert "user_profile_dreams_owner_ck" in table
    assert "user_id > 0" in table
    assert "user_profile_dreams_timestamp_range_ck" in table
    assert "created_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00'" in table
    assert "created_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'" in table
    assert "lease_expires_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'" in table
    assert "completed_at < TIMESTAMPTZ '10000-01-01 00:00:00+00'" in table
    assert "user_profile_dreams_counter_ck" in table
    assert "version = attempt_count" in table
    assert "user_profile_dreams_metadata_ck" in table
    assert "jsonb_typeof(metadata -> 'display_name') = 'string'" in table
    assert "char_length(btrim(metadata ->> 'display_name', E' " in table
    assert "jsonb_typeof(metadata -> 'username') = 'string'" in table
    assert "char_length(metadata ->> 'personal_info') <= 500" in table
    assert "jsonb_typeof(metadata -> 'provider') = 'string'" in table
    assert "(metadata ->> 'provider') ~ '^[a-z][a-z0-9_.-]{0,31}$'" in table
    assert "char_length(btrim(metadata ->> 'username', E' " in table
    assert "char_length(btrim(metadata ->> 'provider', E' " in table
    assert "user_profile_dreams_state_ck" in table
    assert "user_profile_dreams_result_payload_ck" in table
    for status in (
        "pending",
        "processing",
        "retry_wait",
        "completed",
        "failed_final",
    ):
        assert f"status = '{status}'" in table
    for invariant in (
        "next_attempt_at = created_at",
        "updated_at = created_at",
        "lease_expires_at > updated_at",
        "next_attempt_at >= updated_at",
        "char_length(btrim(last_error, E' ",
        "char_length(last_error) BETWEEN 1 AND 1000",
        "char_length(route_key) BETWEEN 1 AND 300",
        "char_length(btrim(route_key, E' ",
        "completed_at = updated_at",
        "(result_patch ->> 'prompt_version') ~ '^[1-9][0-9]*$'",
        "jsonb_array_length(result_patch -> 'operations') <= 64",
        "jsonb_path_exists(",
        '@.op == "delete" || @.op == "upsert"',
        '@.statement like_regex "^.{1,250}(.{1,250})?$" flag "s"',
        '@.statement like_regex ".*[^\\u0009\\u000A',
        "@ > 9223372036854775807",
        "jsonb_path_query_array(",
        "$integer_ids$^\\[([1-9][0-9]*",
    ):
        assert invariant in table
    for obsolete in (
        "user_profile_dreams_ready_ck",
        "user_profile_dreams_lease_ck",
        "user_profile_dreams_terminal_ck",
        "user_profile_dreams_result_ck",
    ):
        assert obsolete not in table
