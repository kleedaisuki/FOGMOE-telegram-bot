"""Add recurrence fields to ai_schedules."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = '0013_add_ai_schedule_recurrence'
down_revision = '0012_add_user_plan'
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
