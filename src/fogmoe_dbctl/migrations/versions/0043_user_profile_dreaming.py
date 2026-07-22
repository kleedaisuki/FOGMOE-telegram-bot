"""@brief 建立 User Profile Dreaming bounded context / Establish the User Profile Dreaming bounded context."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0043_user_profile_dreaming"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0042_remove_legacy_memory"
"""@brief 前置迁移版本 / Parent migration revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立 evidence、Dream queue 与 Profile revisions / Create evidence, Dream queue, and Profile revisions.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除 User Profile 并恢复旧 affection 表 / Remove User Profile and restore the legacy affection table.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
