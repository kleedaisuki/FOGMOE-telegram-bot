"""@brief 建立 speaker-aware 群 Topic 上下文 / Establish speaker-aware group-topic context."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0046_group_aware_context"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0045_memory_management_commands"
"""@brief 前置迁移版本 / Parent migration revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 添加群 Topic 与 speaker identity / Add group-topic and speaker identity.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除群感知上下文字段 / Remove group-aware context fields.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
