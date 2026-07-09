"""Drop ai_user_diary table."""

from fogmoe_bot.infrastructure.database.migrations.runner import run_migration_sql

revision = '0006_drop_ai_user_diary'
down_revision = '0005_add_ai_user_diary_pages'
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
