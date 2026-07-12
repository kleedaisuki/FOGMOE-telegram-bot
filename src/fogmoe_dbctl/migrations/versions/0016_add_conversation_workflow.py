"""Add durable conversation inbox, workflow turns, messages, and outbox."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0016_add_conversation_workflow"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0015_add_schedule_leases"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建可持久化会话工作流表 / Create durable conversation-workflow tables.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除可持久化会话工作流表 / Drop durable conversation-workflow tables.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
