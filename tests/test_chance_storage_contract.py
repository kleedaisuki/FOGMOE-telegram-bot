"""@brief 可验证随机活动的 PostgreSQL 存储契约测试 / PostgreSQL storage-contract tests for verifiable chance activities."""

from __future__ import annotations

from pathlib import Path
import re


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""


def test_chance_migration_and_snapshot_enforce_private_commitment_lifecycle() -> None:
    """@brief 迁移与快照都冻结承诺、清空 settled seed 并追加回执 /
    Migration and snapshot both freeze commitments, clear settled seed, and append receipts.
    """

    migration = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0051_verifiable_chance.sql"
    ).read_text(encoding="utf-8")
    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )
    version = (
        _PROJECT_ROOT / "src/fogmoe_dbctl/migrations/versions/0051_verifiable_chance.py"
    ).read_text(encoding="utf-8")

    required_statements = (
        "CREATE SCHEMA IF NOT EXISTS chance",
        "CREATE TABLE chance.rounds",
        "server_seed BYTEA NULL",
        "CONSTRAINT chance_rounds_state_shape_ck",
        "status = 'committed'",
        "status = 'settled'",
        "AND server_seed IS NULL",
        "CREATE FUNCTION chance.enforce_round_lifecycle",
        "CREATE TRIGGER chance_rounds_one_way_settlement_tr",
        "BEFORE INSERT OR UPDATE OR DELETE ON chance.rounds",
        "CREATE TABLE chance.operation_receipts",
        "request_fingerprint CHAR(64)",
        "CREATE FUNCTION chance.forbid_receipt_mutation",
        "CREATE TRIGGER chance_operation_receipts_append_only_tr",
    )
    for statement in required_statements:
        assert statement in migration
        assert statement in snapshot

    assert 'down_revision = "0050_group_town"' in version
    # 后续迁移会合法推进 snapshot head；唯一 head 的精确一致性由
    # test_migration_sql_runner 覆盖，这里只确认 snapshot 仍是有效迁移图产物。
    assert re.search(r"^-- Alembic head: \S+$", snapshot, flags=re.MULTILINE)


def test_schema_snapshot_includes_the_preceding_group_town_contract() -> None:
    """@brief 升至 0051 的快照也包含 0050 group-town DDL /
    Snapshot upgraded through 0051 also includes preceding 0050 group-town DDL.
    """

    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )

    for statement in (
        "CREATE SCHEMA IF NOT EXISTS town",
        "CREATE TABLE town.towns",
        "CREATE TABLE town.projects",
        "CREATE TABLE town.contributions",
        "CREATE TABLE town.operation_receipts",
        "CREATE TRIGGER town_operation_receipts_append_only_tr",
    ):
        assert statement in snapshot
