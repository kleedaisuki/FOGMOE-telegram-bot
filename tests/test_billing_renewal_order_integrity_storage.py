"""@brief Billing 订阅续费订单完整性的存储契约测试 / Storage-contract tests for Billing subscription-renewal order integrity."""

from __future__ import annotations

from pathlib import Path

from fogmoe_dbctl.migrations import runner


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""


def test_0057_limits_each_subscription_to_one_open_renewal_order() -> None:
    """@brief 0057 以锁定预检和 partial unique index 限制开放续费订单 / 0057 uses a locked preflight and partial unique index to limit open renewal orders.

    @return None / None.
    """

    migration_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0057_billing_renewal_order_integrity.sql"
    )
    version_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/versions/0057_billing_renewal_order_integrity.py"
    )
    sections = runner._sections(
        migration_path.read_text(encoding="utf-8"),
        migration_path,
    )
    upgrade = sections["up"]
    downgrade = sections["down"]
    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )
    version = version_path.read_text(encoding="utf-8")

    assert 'revision = "0057_billing_renewal_order_integrity"' in version
    assert 'down_revision = "0056_bank_balance_projection_fix"' in version
    assert "LOCK TABLE billing.orders IN SHARE ROW EXCLUSIVE MODE" in upgrade
    assert "GROUP BY orders.renewal_subscription_id" in upgrade
    assert "HAVING count(*) > 1" in upgrade
    assert "duplicate open renewal orders exist" in upgrade
    assert "DROP INDEX IF EXISTS billing.billing_orders_one_open_renewal_uq" in downgrade

    for storage in (upgrade, snapshot):
        assert "CREATE UNIQUE INDEX billing_orders_one_open_renewal_uq" in storage
        assert "ON billing.orders (renewal_subscription_id)" in storage
        assert "renewal_subscription_id IS NOT NULL" in storage
        assert "'awaiting_payment', 'paid', 'refund_pending'" in storage


def test_0057_sql_runner_keeps_duplicate_preflight_and_unique_index_distinct() -> None:
    """@brief SQL runner 将预检 DO 块和唯一索引作为独立安全语句执行 / The SQL runner executes the preflight DO block and unique index as separate safe statements.

    @return None / None.
    """

    migration_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0057_billing_renewal_order_integrity.sql"
    )
    sections = runner._sections(
        migration_path.read_text(encoding="utf-8"),
        migration_path,
    )
    statements = runner._split_sql_statements(sections["up"])

    assert any(
        "LOCK TABLE billing.orders IN SHARE ROW EXCLUSIVE MODE" in statement
        for statement in statements
    )
    assert any(
        "duplicate open renewal orders exist" in statement
        and "HAVING count(*) > 1" in statement
        for statement in statements
    )
    assert any(
        "CREATE UNIQUE INDEX billing_orders_one_open_renewal_uq" in statement
        and "'awaiting_payment', 'paid', 'refund_pending'" in statement
        for statement in statements
    )
