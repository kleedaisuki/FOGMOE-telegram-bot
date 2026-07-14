"""@brief 建立银行双重记账与代币申请边界 / Establish banking ledger and token-request boundary."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0047_banking_ledger"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0046_group_aware_context"
"""@brief 前置迁移版本 / Parent migration revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建可审计银行账本 / Create the auditable bank ledger.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除银行账本边界 / Remove the banking ledger boundary.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
