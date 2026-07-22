"""@brief 个人 RPG PostgreSQL 存储契约测试 / PostgreSQL storage-contract tests for the personal RPG."""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""


def test_personal_rpg_migration_and_snapshot_match_adapter_storage_contract() -> None:
    """@brief 迁移和快照覆盖角色、材料、探索、图鉴和不可变回执 /
    Migration and snapshot cover characters, materials, explorations, collections, and immutable receipts.
    """

    migration = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0052_personal_rpg.sql"
    ).read_text(encoding="utf-8")
    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )
    version = (
        _PROJECT_ROOT / "src/fogmoe_dbctl/migrations/versions/0052_personal_rpg.py"
    ).read_text(encoding="utf-8")

    required_statements = (
        "CREATE SCHEMA IF NOT EXISTS personal_rpg",
        "CREATE TABLE personal_rpg.characters",
        "CREATE TABLE personal_rpg.materials",
        "PRIMARY KEY (user_id, material_kind)",
        "CREATE TABLE personal_rpg.explorations",
        "CONSTRAINT personal_rpg_explorations_user_day_uq UNIQUE (user_id, exploration_day)",
        "CONSTRAINT personal_rpg_explorations_utc_day_ck",
        "CREATE TABLE personal_rpg.collections",
        "craft_id UUID NOT NULL UNIQUE",
        "CONSTRAINT personal_rpg_collections_recipe_output_ck",
        "CREATE TABLE personal_rpg.operation_receipts",
        "'personal_rpg.create_character'",
        "'personal_rpg.explore_daily'",
        "'personal_rpg.craft_recipe'",
        "CREATE FUNCTION personal_rpg.forbid_append_only_mutation",
        "CREATE TRIGGER personal_rpg_explorations_append_only_tr",
        "CREATE TRIGGER personal_rpg_collections_append_only_tr",
        "CREATE TRIGGER personal_rpg_operation_receipts_append_only_tr",
    )
    for statement in required_statements:
        assert statement in migration
        assert statement in snapshot

    for storage in (migration, snapshot):
        receipt_table = storage.split(
            "CREATE TABLE personal_rpg.operation_receipts", 1
        )[1].split("CREATE INDEX personal_rpg_operation_receipts", 1)[0]
        assert "actor_id BIGINT NOT NULL CHECK (actor_id > 0)" in receipt_table
        assert "REFERENCES identity.users(id)" not in receipt_table
    assert 'down_revision = "0051_verifiable_chance"' in version
    # 个人 RPG 的存储契约应持续存在于当前完整快照，而非固定在其后的某一旧 head。
    assert re.search(r"^-- Alembic head: \S+$", snapshot, flags=re.MULTILINE)
