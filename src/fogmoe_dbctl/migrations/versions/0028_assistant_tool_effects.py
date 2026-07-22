"""@brief 增加 Assistant step checkpoint 与 tool effect receipts / Add Assistant step checkpoints and tool-effect receipts."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0028_assistant_tool_effects"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0027_games_workflow"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立 checkpoint 与 receipts / Create checkpoints and receipts.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复 0027 schema / Restore the 0027 schema.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
