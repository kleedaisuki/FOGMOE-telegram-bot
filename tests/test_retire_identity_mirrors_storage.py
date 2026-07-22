"""@brief 0062 identity/legacy-media 退役存储契约 / Storage contracts for 0062 identity/legacy-media retirement."""

from __future__ import annotations

from pathlib import Path

from fogmoe_dbctl.migrations import runner


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root directory."""


def _migration() -> str:
    """@brief 读取 0062 SQL / Read the 0062 SQL.

    @return 完整 migration SQL / Complete migration SQL.
    """

    return (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0062_retire_identity_mirrors_and_legacy_media.sql"
    ).read_text(encoding="utf-8")


def test_0062_fails_closed_before_retiring_identity_money_mirrors() -> None:
    """@brief 0062 先比较缺账户为零的 Bank 权威余额，再删 identity 镜像 / 0062 compares Bank-authoritative balances with missing accounts treated as zero before dropping identity mirrors."""

    migration = _migration()
    preflight = migration.index("identity and Bank balances differ")
    column_drop = migration.index("DROP COLUMN coins")

    assert "COALESCE(free_balance.balance, 0)" in migration
    assert "COALESCE(paid_balance.balance, 0)" in migration
    assert "users.coins IS DISTINCT FROM" in migration
    assert "users.coins_paid IS DISTINCT FROM" in migration
    assert preflight < column_drop
    assert "DROP TRIGGER identity_users_money_projection_tr" in migration
    assert "DROP FUNCTION bank.guard_identity_user_money_projection()" in migration
    assert "DROP COLUMN user_plan" in migration


def test_0062_uses_delivery_and_refund_facts_for_preview_liability() -> None:
    """@brief 滞后的 preview_pending 只有 delivered receipt 或退款证据时可清理 / A stale preview_pending row is removable only with delivered-receipt or refund evidence."""

    migration = _migration()

    assert "outbound.status = 'delivered'" in migration
    assert "receipt.result ? 'offer'" in migration
    assert "(receipt.result ->> 'cost')::BIGINT = offer.preview_cost" in migration
    assert "NOT offer.preview_refunded" in migration
    assert "a preview charge is neither delivered nor refunded" in migration
    assert migration.index(
        "a preview charge is neither delivered nor refunded"
    ) < migration.index("DROP TABLE media.picture_request_receipts")


def test_0062_allows_unclaimed_hd_quote_but_blocks_unrefunded_hd_charge() -> None:
    """@brief available+hd_cost 只是报价；只有 charged_user 证明发生 HD 收费 / available+hd_cost is only a quote; charged_user alone proves an HD charge."""

    migration = _migration()
    charged_user_predicate = migration.index("offer.charged_user_id IS NOT NULL")
    liability_start = migration.rfind("IF EXISTS (", 0, charged_user_predicate)
    liability_end = migration.index("END IF;", charged_user_predicate)
    liability_check = migration[liability_start:liability_end]

    assert "offer.charged_user_id IS NOT NULL" in migration
    assert "offer.hd_cost IS NOT NULL" not in liability_check
    assert "offer.state = 'refunded' AND offer.hd_refunded" in migration
    assert "state IN ('charged', 'delivered')" in migration


def test_0062_drops_legacy_tables_explicitly_and_is_irreversible() -> None:
    """@brief 0062 显式按依赖顺序删除旧表且不伪造 downgrade / 0062 drops legacy tables in explicit dependency order and does not fabricate a downgrade."""

    migration_path = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0062_retire_identity_mirrors_and_legacy_media.sql"
    )
    migration = migration_path.read_text(encoding="utf-8")
    sections = runner._sections(migration, migration_path)
    version = (
        _PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/versions/0062_retire_identity_mirrors_and_legacy_media.py"
    ).read_text(encoding="utf-8")
    snapshot = (_PROJECT_ROOT / "src/fogmoe_dbctl/schema.sql").read_text(
        encoding="utf-8"
    )

    assert migration.index(
        "DROP TABLE media.picture_request_receipts"
    ) < migration.index("DROP TABLE media.picture_offers")
    assert "DROP TABLE IF EXISTS game.migration_0027_omikuji_repairs" in migration
    assert "is irreversible" in sections["down"]
    assert 'revision = "0062_retire_identity_mirrors_and_legacy_media"' in version
    assert 'down_revision = "0061_rebuild_assistant_scheduling"' in version
    identity_users = snapshot[
        snapshot.index("CREATE TABLE identity.users (") : snapshot.index(
            ");", snapshot.index("CREATE TABLE identity.users (")
        )
    ]
    for retired in ("coins", "coins_paid", "user_plan"):
        assert retired not in identity_users
    for retired in (
        "bank.guard_identity_user_money_projection",
        "CREATE TABLE media.picture_offers",
        "CREATE TABLE media.picture_request_receipts",
        "CREATE TABLE game.migration_0027_omikuji_repairs",
    ):
        assert retired not in snapshot
