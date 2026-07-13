"""@brief 建立可线性化的 Memory/Profile 管理边界 / Establish linearizable Memory/Profile management boundaries."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0045_memory_management_commands"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0044_retrieval_privacy_scopes"
"""@brief 前置迁移版本 / Parent migration revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 添加 Retrieval 与 Profile 遗忘边界 / Add Retrieval and Profile forgetting boundaries.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除管理命令边界 / Remove management-command boundaries.

    @return None / None.
    @note downgrade 会允许旧来源再次被投影 / Downgrade allows old sources to be projected again.
    """

    run_migration_sql(__file__, "down")
