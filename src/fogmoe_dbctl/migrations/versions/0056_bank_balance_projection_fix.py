"""@brief 修复 Bank 余额投影借记触发器 / Repair the Bank balance-projection debit trigger."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0056_bank_balance_projection_fix"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0055_retire_legacy_wagers"
"""@brief 前置旧押注退役迁移 / Parent legacy-wager retirement migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 修复负数账本过账在 UPSERT 前被错误拒绝的问题 / Repair negative postings rejected before UPSERT conflict handling.

    @return None / None.
    @note 该修复保持账本、余额投影和 identity 镜像的既有事务边界。/
        This repair preserves the existing transaction boundary for ledger, balance projection,
        and identity mirror updates.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复前一 revision 的触发器定义 / Restore the prior revision's trigger definition.

    @return None / None.
    @note 降级只用于严格 revision 回退；生产货币库不应回退到已知缺陷实现。/
        Downgrade exists only for exact revision rollback; production money stores should not
        return to the known-defective implementation.
    """

    run_migration_sql(__file__, "down")
