"""@brief 建立 typed observability 存储与 durable trace context / Add typed observability storage and durable trace context."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0039_observability"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0038_repair_turn_delivery_plan_state"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建观测模型和 trace carriers / Create the observability model and trace carriers.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除观测模型和 trace carriers / Remove the observability model and trace carriers.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
