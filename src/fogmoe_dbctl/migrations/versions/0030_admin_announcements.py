"""@brief 增加 durable Admin 公告意图与回执 / Add durable Admin announcement intents and receipts."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0030_admin_announcements"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0029_identity_operation_receipts"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立公告意图、受众快照与 fencing 回执 / Create announcement intents, audience snapshots, and fenced receipts.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复 0029 schema / Restore the 0029 schema.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
