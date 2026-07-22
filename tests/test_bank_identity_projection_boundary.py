"""@brief Bank 唯一货币投影边界的存储契约测试 / Storage-contract tests for the Bank-only monetary projection boundary."""

from __future__ import annotations

import re
from pathlib import Path

from fogmoe_dbctl.migrations import runner

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""


def test_identity_projection_migration_archives_legacy_billing_before_later_retirement() -> (
    None
):
    """@brief 0054 封闭 identity 金币旁路并归档旧 Assistant 预留 / Migration 0054 closes identity-token bypasses and archives legacy Assistant reservations before a later retirement migration."""

    migration_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0054_bank_identity_projection_boundary.sql"
    )
    migration = migration_path.read_text(encoding="utf-8")
    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )
    version = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/versions/0054_bank_identity_projection_boundary.py"
    ).read_text(encoding="utf-8")

    historical_projection_statements = (
        "identity_users_coins_projection_nonnegative_ck",
        "identity_users_coins_paid_projection_nonnegative_ck",
        "CREATE FUNCTION bank.guard_identity_user_money_projection()",
        "CREATE TRIGGER identity_users_money_projection_tr",
    )
    archive_statements = (
        "CREATE FUNCTION bank.forbid_legacy_assistant_billing_mutation()",
        "CREATE TRIGGER assistant_billing_reservations_retired_tr",
    )
    for statement in historical_projection_statements:
        assert statement in migration
        assert statement not in snapshot
    for statement in (
        "current_setting('bank.ledger_posting_apply', TRUE) = 'on'",
        "pg_trigger_depth() > 1",
    ):
        assert statement in migration
        assert statement in snapshot
    for statement in archive_statements:
        assert statement in migration

    # 这两项只存在于一次性数据迁移的账本 metadata 中；DDL snapshot 不复制历史数据语句。
    assert "legacy_projection_reconciliation" in migration
    assert "legacy_assistant_reservation_release" in migration

    assert "migration:0054:assistant-release:" in migration
    assert (
        "UPDATE assistant.billing_reservations AS reservation\nSET status = 'released'"
        in migration
    )
    assert 'down_revision = "0053_bank_billing_hardening"' in version
    assert re.search(r"^-- Alembic head: \S+$", snapshot, flags=re.MULTILINE)


def test_0054_sql_splitter_preserves_projection_and_archive_trigger_bodies() -> None:
    """@brief SQL 执行器不会误拆 0054 的 PL/pgSQL 触发器体 /
    SQL runner does not split 0054 PL/pgSQL trigger bodies incorrectly.
    """

    path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0054_bank_identity_projection_boundary.sql"
    )
    sections = runner._sections(path.read_text(encoding="utf-8"), path)
    up_statements = runner._split_sql_statements(sections["up"])
    down_statements = runner._split_sql_statements(sections["down"])

    assert any(
        "CREATE FUNCTION bank.guard_identity_user_money_projection" in statement
        and "pg_trigger_depth() > 1" in statement
        for statement in up_statements
    )
    assert any(
        "CREATE FUNCTION bank.forbid_legacy_assistant_billing_mutation" in statement
        and "assistant token billing is retired" in statement
        for statement in up_statements
    )
    assert any(
        "DROP TRIGGER IF EXISTS identity_users_money_projection_tr" in statement
        for statement in down_statements
    )


def test_runtime_cannot_rebootstrap_identity_money_or_retain_assistant_billing() -> (
    None
):
    """@brief 运行时代码不再从 identity 投影补账，也不保留 Assistant 计费兼容层 /
    Runtime code no longer bootstraps from identity projections or retains an Assistant-billing compatibility layer.
    """

    banking = (
        _PROJECT_ROOT / "src/fogmoe_bot/infrastructure/database/banking.py"
    ).read_text(encoding="utf-8")
    assistant_ingress = (
        _PROJECT_ROOT / "src/fogmoe_bot/application/conversation/assistant_ingress.py"
    ).read_text(encoding="utf-8")
    acceptance = (
        _PROJECT_ROOT
        / "src/fogmoe_bot/infrastructure/database/assistant_turn_acceptance.py"
    ).read_text(encoding="utf-8")
    billing = (
        _PROJECT_ROOT / "src/fogmoe_bot/infrastructure/database/assistant_billing.py"
    )

    assert "legacy-projection-bootstrap" not in banking
    assert "SELECT coins, coins_paid FROM identity.users" not in banking
    assert "coin_cost" not in assistant_ingress
    assert "fetch_user_account" not in acceptance
    assert "set_coin_balances_and_plan" not in acceptance
    assert ".reserve(" not in acceptance
    assert not billing.exists()
