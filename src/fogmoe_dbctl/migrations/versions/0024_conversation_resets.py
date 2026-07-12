"""@brief 添加 append-only Conversation 历史 reset 边界 / Add append-only Conversation-history reset boundaries."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0024_conversation_resets"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0023_media_workflow"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建 Conversation reset 边界表 / Create the Conversation-reset boundary table.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除 reset 边界表 / Drop the reset-boundary table.

    @return None / None.
    @note 规范消息不会被删除；降级后它们全部重新可见 / Canonical messages are not
        deleted; all become visible again after downgrade.
    """

    run_migration_sql(__file__, "down")
