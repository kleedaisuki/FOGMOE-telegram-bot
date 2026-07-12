"""@brief 添加可恢复长推理活动 / Add recoverable long-running inference activities."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0018_inference_activities"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0017_verification_workflow"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建推理活动队列 / Create the inference-activity queue.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除推理活动队列 / Drop the inference-activity queue.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
