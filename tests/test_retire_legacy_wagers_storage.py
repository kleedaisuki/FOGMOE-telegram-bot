"""@brief 旧经济退役迁移的存储契约测试 / Storage-contract tests for legacy economy retirement."""

from __future__ import annotations

from pathlib import Path

from fogmoe_dbctl.migrations import runner

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root directory."""


def test_0055_refunds_every_attributable_held_principal_through_bank() -> None:
    """@brief 0055 只以平衡 Bank 分录退款可归属旧本金 / 0055 refunds attributable legacy principal only through balanced Bank entries.

    @return None / None.
    @note 系统迁移分录不得伪装成任一用户操作，beneficiary 由 metadata.user_id 明示。/
        A system migration entry must not impersonate a user; metadata.user_id explicitly names the beneficiary.
    """

    path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0055_retire_legacy_wagers.sql"
    )
    migration = path.read_text(encoding="utf-8")
    sections = runner._sections(migration, path)
    statements = runner._split_sql_statements(sections["up"])

    for marker in (
        "migration:0055:btc-prediction-refund:",
        "migration:0055:stake-principal:",
        "migration:0055:gamble-refund:",
        "migration:0055:rps-principal:",
        "migration:0055:assistant-double-refund:",
        "legacy_wager_refunds",
        "LOCK TABLE",
        "IN SHARE ROW EXCLUSIVE MODE",
        "'migration_opening'",
        "'system:issuance'",
        "'legacy_refund', TRUE",
        "'user_id', prediction.user_id",
        "'user_id', stake.user_id",
        "'user_id', (bet.value ->> 'user_id')::BIGINT",
        "'user_id', player.user_id",
        "'assistant_pool_double_refund', TRUE",
        "'refund_multiplier', 2",
    ):
        assert marker in migration

    assert "UPDATE identity.users" not in migration
    assert "'migration_opening',\n  NULL,\n  refund.metadata" in migration
    assert any("SET CONSTRAINTS ALL IMMEDIATE" in statement for statement in statements)


def test_0055_requires_manual_disposition_for_unowned_pool_balance() -> None:
    """@brief 0055 要求人工处置无主池余额 / 0055 requires manual disposition for an unowned pool balance.

    @return None / None.
    """

    migration = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0055_retire_legacy_wagers.sql"
    ).read_text(encoding="utf-8")

    assert "operator-only exception" in migration
    assert "unallocated or unexpected posting requires manual disposition" in migration
    assert "nonzero cached pool balance requires manual disposition" in migration
    assert "native Assistant settlement does not match its pool posting" in migration
    assert "eager Assistant settlements do not reconcile to pool postings" in migration
    assert "cannot retire Bank staking_pool account" in migration
    assert "DROP TABLE economy.stake_pool_postings" in migration
    assert "DROP TABLE IF EXISTS game.migration_0027_character_repairs" in migration
    assert "DROP TABLE IF EXISTS game.migration_0027_inventory_repairs" in migration
    assert "DROP TABLE game.rps_sessions" in migration
    assert "DROP TABLE game.game_sessions" in migration
    assert "DROP TABLE game.rpg_characters" in migration


def test_0055_version_and_snapshot_close_the_legacy_vocabulary() -> None:
    """@brief 0055 版本与快照移除旧表、旧账户种类和死账本原因 /
    0055 version and snapshot remove old tables, account kinds, and dead ledger reasons.

    @return None / None.
    """

    version = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/versions/0055_retire_legacy_wagers.py"
    ).read_text(encoding="utf-8")
    banking_origin = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0047_banking_ledger.sql"
    ).read_text(encoding="utf-8")
    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0055_retire_legacy_wagers"' in version
    assert 'down_revision = "0054_bank_identity_projection_boundary"' in version
    # 0055 may gain a later hardening child; the graph-head test owns the exact
    # head assertion, while this test verifies the retirement snapshot content.
    assert "-- Alembic head: " in snapshot
    assert "CHECK (operation = 'omikuji.draw')" in snapshot

    for retired in (
        "CREATE TABLE economy.user_stakes",
        "CREATE TABLE economy.stake_reward_pool",
        "CREATE TABLE economy.stake_pool_postings",
        "CREATE TABLE economy.shop_pity",
        "CREATE TABLE crypto.user_btc_predictions",
        "CREATE TABLE game.rpg_characters",
        "CREATE TABLE game.rps_sessions",
        "CREATE TABLE game.game_sessions",
        "staking_pool",
        "rpg_reward",
        "rpg_purchase",
        "'subscription_grant'",
    ):
        assert retired not in snapshot

    for surviving in (
        "CREATE TABLE chance.rounds",
        "CREATE TABLE town.towns",
        "CREATE TABLE personal_rpg.characters",
    ):
        assert surviving in snapshot

    # 新安装从 0047 起就不再创建死账户/死原因；0055 仍保留对既有历史
    # 数据库的 fail-closed cleanup，因此不能以其文本出现作为 live vocabulary。
    for retired in (
        "'staking_pool'",
        "'rpg_reward'",
        "'rpg_purchase'",
        "'subscription_grant'",
        "'system:staking_pool'",
    ):
        assert retired not in banking_origin
