"""@brief 强化 Billing 订阅续费订单完整性 / Harden Billing subscription-renewal order integrity."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0057_billing_renewal_order_integrity"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0056_bank_balance_projection_fix"
"""@brief 前置 Bank 余额投影修复迁移 / Parent Bank balance-projection repair migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 限定每个订阅同时只能有一笔未终态续费订单 / Limit each subscription to one simultaneous open renewal order.

    @return None / None.
    @note 先在受锁表上拒绝历史重复数据，再以 partial unique index 作为绕过应用锁时的
        数据库最终防线。/ First reject historical duplicates while the table is locked, then use a
        partial unique index as the database final defense when application locking is bypassed.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除续费未终态唯一性约束 / Remove the open-renewal uniqueness constraint.

    @return None / None.
    @note 降级只删除索引，不会合成或删除任何订单记录。/
        Downgrade removes only the index and never synthesizes or deletes order records.
    """

    run_migration_sql(__file__, "down")
