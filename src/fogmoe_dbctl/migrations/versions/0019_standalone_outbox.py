"""@brief 允许无 Turn 的独立 outbox 副作用 / Allow standalone outbox effects without a Turn."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0019_standalone_outbox"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0018_inference_activities"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 放宽 outbox Turn 所有权 / Relax outbox Turn ownership.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复每条 outbox 必须属于 Turn / Restore mandatory Turn ownership for every outbox row.

    @return None / None.
    @note 无 Turn 行会被删除，因为旧 schema 无法表示它们 / Rows without a Turn are deleted because the old schema cannot represent them.
    """

    run_migration_sql(__file__, "down")
