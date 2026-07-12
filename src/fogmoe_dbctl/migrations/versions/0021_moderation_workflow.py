"""@brief 持久化群组治理聚合与副作用 / Persist group-moderation aggregates and effects."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0021_moderation_workflow"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0020_turn_sources"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建治理工作流 schema / Create moderation-workflow schema.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除治理工作流 schema / Remove moderation-workflow schema.

    @return None / None.
    @note downgrade 会丢弃举报、警告与 effect 历史 / Downgrade discards report, warning, and effect history.
    """

    run_migration_sql(__file__, "down")
