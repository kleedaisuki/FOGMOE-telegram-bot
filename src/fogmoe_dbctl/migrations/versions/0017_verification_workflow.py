"""@brief 将成员验证演进为可租约领取的持久工作流 / Evolve member verification into a lease-claimed durable workflow."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0017_verification_workflow"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0016_add_conversation_workflow"
"""@brief 前一迁移版本 / Previous migration revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Alembic extra dependencies."""


def upgrade() -> None:
    """@brief 升级成员验证单表工作流 / Upgrade the single-table verification workflow.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 回退成员验证工作流列 / Revert verification-workflow columns.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
