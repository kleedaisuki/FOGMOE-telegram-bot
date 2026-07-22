"""@brief Bank 与 Billing 硬化迁移的 PostgreSQL 存储契约测试 / PostgreSQL storage-contract tests for bank and Billing hardening."""

from __future__ import annotations

import re
from pathlib import Path

from fogmoe_dbctl.migrations import runner

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""


def test_bank_billing_hardening_migration_and_snapshot_share_integrity_contract() -> (
    None
):
    """@brief 迁移和快照共同阻止重复支付、空分录与余额直写 /
    Migration and snapshot jointly reject duplicate payments, empty entries, and direct balance writes.
    """

    migration_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0053_bank_billing_hardening.sql"
    )
    migration = migration_path.read_text(encoding="utf-8")
    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )
    version = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/versions/0053_bank_billing_hardening.py"
    ).read_text(encoding="utf-8")

    for statement in (
        "CREATE UNIQUE INDEX billing_payment_events_success_payment_uq",
        "WHERE event_kind = 'payment_succeeded'",
        "CREATE CONSTRAINT TRIGGER bank_ledger_entries_complete_ct",
        "AFTER INSERT ON bank.ledger_entries",
        "DEFERRABLE INITIALLY DEFERRED",
        "set_config('bank.ledger_posting_apply', 'on', TRUE)",
        "CREATE FUNCTION bank.forbid_account_mutation()",
        "CREATE TRIGGER bank_accounts_no_direct_mutation_tr",
        "CREATE FUNCTION bank.guard_account_balance_mutation()",
        "CREATE TRIGGER bank_account_balances_authorize_mutation_tr",
        "pg_trigger_depth() > 1",
    ):
        assert statement in migration
        assert statement in snapshot

    assert "CREATE OR REPLACE FUNCTION bank.apply_ledger_posting_balance()" in migration
    assert "CREATE FUNCTION bank.apply_ledger_posting_balance()" in snapshot
    assert "ADD COLUMN request_fingerprint CHAR(64)" in migration
    assert "SET request_fingerprint = repeat('0', 64)" in migration
    assert "DISABLE TRIGGER billing_operation_receipts_append_only_tr" in migration
    assert "ALTER COLUMN request_fingerprint SET NOT NULL" in migration
    assert "billing_operation_receipts_request_fingerprint_ck" in migration
    assert "DROP COLUMN IF EXISTS request_fingerprint" in migration
    assert "request_fingerprint CHAR(64) NOT NULL CHECK" in snapshot
    assert "identity.users write guard is intentionally deferred" in migration
    assert 'down_revision = "0052_personal_rpg"' in version
    # 0053 的不变量必须在后续迁移后的完整快照中继续存在；快照头会随新迁移前进。
    assert re.search(r"^-- Alembic head: \S+$", snapshot, flags=re.MULTILINE)

    for storage in (migration, snapshot):
        assert "BEFORE UPDATE OR DELETE ON bank.accounts" in storage
        assert "BEFORE INSERT OR UPDATE OR DELETE ON bank.account_balances" in storage


def test_bank_billing_hardening_sql_is_split_without_breaking_trigger_bodies() -> None:
    """@brief SQL 执行器不会把 0053 的 PL/pgSQL 触发器体误拆分 /
    The SQL runner does not split 0053 PL/pgSQL trigger bodies incorrectly.
    """

    migration_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0053_bank_billing_hardening.sql"
    )
    sections = runner._sections(
        migration_path.read_text(encoding="utf-8"), migration_path
    )
    up_statements = runner._split_sql_statements(sections["up"])
    down_statements = runner._split_sql_statements(sections["down"])

    assert any(
        "CREATE OR REPLACE FUNCTION bank.apply_ledger_posting" in statement
        and "PERFORM set_config('bank.ledger_posting_apply', 'on', TRUE);" in statement
        for statement in up_statements
    )
    assert any(
        "CREATE FUNCTION bank.guard_account_balance_mutation" in statement
        and "pg_trigger_depth() > 1" in statement
        for statement in up_statements
    )
    assert any(
        "CREATE OR REPLACE FUNCTION bank.apply_ledger_posting" in statement
        for statement in down_statements
    )
