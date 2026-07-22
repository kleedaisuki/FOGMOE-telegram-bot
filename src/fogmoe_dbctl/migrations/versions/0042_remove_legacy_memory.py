"""@brief 移除无语义的旧 Memory schema 与用户额度 / Remove the obsolete Memory schema and user quota."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0042_remove_legacy_memory"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0041_episodic_retrieval"
"""@brief 前置迁移版本 / Parent migration revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 删除旧 Memory quota 与空 schema / Drop the legacy Memory quota and empty schema.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复旧 quota 与空 schema 形状 / Restore the old quota and empty schema shape.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
