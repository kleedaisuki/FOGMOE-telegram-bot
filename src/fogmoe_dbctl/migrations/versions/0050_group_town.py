"""@brief 建立群组小镇与银行金库投影 / Establish group-town and bank-treasury projections."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0050_group_town"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0049_billing_entitlements"
"""@brief 前置 Billing 与权益迁移 / Parent Billing and entitlement migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建群组小镇、项目、贡献和回执结构 / Create group-town, project, contribution, and receipt structures.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除尚未对外启用的小镇结构 / Remove not-yet-public town structures.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
