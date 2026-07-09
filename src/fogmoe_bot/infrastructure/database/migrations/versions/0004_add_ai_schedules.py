"""Add ai_schedules table."""

from fogmoe_bot.infrastructure.database.migrations.runner import run_migration_sql

revision = '0004_add_ai_schedules'
down_revision = '0003_add_ai_user_diary'
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
