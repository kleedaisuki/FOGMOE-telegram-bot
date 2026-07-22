"""@brief 建立 Billing、支付与权益持久化边界 / Establish Billing, payment, and entitlement persistence boundary."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0049_billing_entitlements"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0048_remove_legacy_topup_and_swap"
"""@brief 前置清理遗留充值与兑换迁移 / Parent legacy-topup and swap cleanup migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建独立的 Billing 与权益持久化结构 / Create independent Billing and entitlement persistence structures.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除尚未对外启用的 Billing 边界 / Remove the not-yet-public Billing boundary.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
