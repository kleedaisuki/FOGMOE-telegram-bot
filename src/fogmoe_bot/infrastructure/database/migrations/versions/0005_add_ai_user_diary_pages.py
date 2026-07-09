"""Add ai_user_diary_pages table."""

from fogmoe_bot.infrastructure.database.migrations.runner import run_migration_sql

revision = '0005_add_ai_user_diary_pages'
down_revision = '0004_add_ai_schedules'
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
