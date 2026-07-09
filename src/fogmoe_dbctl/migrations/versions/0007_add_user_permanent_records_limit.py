"""Add permanent_records_limit to user table."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = '0007_add_user_permanent_records_limit'
down_revision = '0006_drop_ai_user_diary'
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
