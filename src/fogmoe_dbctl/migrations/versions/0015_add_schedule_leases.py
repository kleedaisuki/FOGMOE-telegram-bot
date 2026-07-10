"""Add lease fencing to scheduled jobs."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0015_add_schedule_leases"
down_revision = "0014_add_verification_token_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """@brief 添加任务租约列 / Add scheduled-job lease columns."""

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除任务租约列 / Drop scheduled-job lease columns."""

    run_migration_sql(__file__, "down")
