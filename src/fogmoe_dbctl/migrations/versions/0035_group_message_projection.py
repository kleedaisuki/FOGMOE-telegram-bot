"""@brief 建立规范群消息投影 / Establish the canonical group-message projection."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0035_group_message_projection"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0034_toggle_command_receipts"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 无损演进旧群消息表 / Losslessly evolve the legacy group-message table."""

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复旧表名与存储约定 / Restore the legacy table name and storage convention."""

    run_migration_sql(__file__, "down")
