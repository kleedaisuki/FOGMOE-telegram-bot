"""@brief 删除人工充值、卡密与链上兑换遗留结构 / Remove legacy top-up, redemption, and token-swap structures."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0048_remove_legacy_topup_and_swap"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0047_banking_ledger"
"""@brief 前置银行账本迁移 / Parent banking-ledger migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 删除旧法币入口与链上兑换持久化结构 / Drop legacy fiat-entry and token-swap persistence structures.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复旧表形状而不恢复已清理业务数据 / Restore legacy table shapes without restoring purged business data.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
