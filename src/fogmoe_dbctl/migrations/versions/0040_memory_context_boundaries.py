"""@brief 拆分 Memory 与 Context Window 数据所有权 / Split Memory and Context Window data ownership."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0040_memory_context_boundaries"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0039_observability"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立显式 Memory 与 Context Window schema / Establish explicit Memory and Context Window schemas.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复统一 retention storage / Restore unified retention storage.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
