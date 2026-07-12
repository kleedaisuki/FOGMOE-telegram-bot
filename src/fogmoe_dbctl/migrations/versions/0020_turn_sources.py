"""@brief 泛化 Conversation Turn 的 durable 来源 / Generalize durable sources for Conversation Turns."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0020_turn_sources"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0019_standalone_outbox"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 添加 namespaced Turn source identity / Add namespaced Turn-source identity.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复仅 Telegram Update 的 Turn 来源 / Restore Telegram-Update-only Turn sources.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
