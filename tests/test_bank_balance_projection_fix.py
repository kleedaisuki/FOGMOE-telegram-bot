"""@brief Bank 余额投影借记修复的存储契约测试 / Storage-contract tests for the Bank balance-projection debit repair."""

from __future__ import annotations

import re
from pathlib import Path

from fogmoe_dbctl.migrations import runner

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""


def test_0056_replaces_speculative_negative_upsert_with_zero_initialization() -> None:
    """@brief 0056 先安全零初始化，再锁行累加 delta / 0056 safely initializes zero, then applies the delta under a row lock.

    @return None / None.
    @note 该断言限定在 up 段，down 为精确 revision 回退而保留旧定义。/
        This assertion is deliberately limited to the up section because down retains the old
        definition for exact revision rollback.
    """

    path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0056_bank_balance_projection_fix.sql"
    )
    sections = runner._sections(path.read_text(encoding="utf-8"), path)
    upgrade = sections["up"]
    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )
    version = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/versions/0056_bank_balance_projection_fix.py"
    ).read_text(encoding="utf-8")

    for storage in (upgrade, snapshot):
        assert "NEW.account_key, 0, 0, CURRENT_TIMESTAMP" in storage
        assert "ON CONFLICT (account_key) DO NOTHING" in storage
        assert "UPDATE bank.account_balances AS current_balance" in storage
        assert "current_balance.balance + NEW.delta" in storage
        assert (
            "bank balance projection % was unavailable after initialization" in storage
        )
        assert "set_config('bank.ledger_posting_apply', 'on', TRUE)" in storage

    assert "NEW.account_key, NEW.delta, 0, CURRENT_TIMESTAMP" not in upgrade
    assert 'revision = "0056_bank_balance_projection_fix"' in version
    assert 'down_revision = "0055_retire_legacy_wagers"' in version
    assert re.search(r"^-- Alembic head: \S+$", snapshot, flags=re.MULTILINE)


def test_0056_sql_splitter_keeps_projection_trigger_and_rollback_separate() -> None:
    """@brief SQL 执行器不会拆坏 0056 的 PL/pgSQL 触发器体 / SQL runner preserves 0056 PL/pgSQL trigger bodies.

    @return None / None.
    """

    path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0056_bank_balance_projection_fix.sql"
    )
    sections = runner._sections(path.read_text(encoding="utf-8"), path)
    up_statements = runner._split_sql_statements(sections["up"])
    down_statements = runner._split_sql_statements(sections["down"])

    assert any(
        "CREATE OR REPLACE FUNCTION bank.apply_ledger_posting_balance" in statement
        and "ON CONFLICT (account_key) DO NOTHING" in statement
        and "current_balance.balance + NEW.delta" in statement
        for statement in up_statements
    )
    assert any(
        "CREATE OR REPLACE FUNCTION bank.apply_ledger_posting_balance" in statement
        and "ON CONFLICT (account_key) DO UPDATE" in statement
        for statement in down_statements
    )


def test_0054_fresh_cutover_locks_writers_and_fails_closed_on_ambiguous_reservations() -> (
    None
):
    """@brief 新库 0054 切换锁住旧写入，并对不明预留要求人工审计 / Fresh 0054 cutover locks legacy writers and requires audit for ambiguous reservations.

    @return None / None.
    """

    migration = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0054_bank_identity_projection_boundary.sql"
    ).read_text(encoding="utf-8")

    for marker in (
        "IN SHARE ROW EXCLUSIVE MODE",
        "assistant.billing_reservations",
        "bank.ledger_entries",
        "bank.ledger_postings",
        "reservation.user_id <= 0",
        "entry.idempotency_key NOT LIKE 'migration:0047:opening:%'",
        "entry.created_at >= reservation.reserved_at",
        "manual audit is required",
        "free_account.account_key IS NULL",
        "paid_balance.account_key IS NULL",
    ):
        assert marker in migration
