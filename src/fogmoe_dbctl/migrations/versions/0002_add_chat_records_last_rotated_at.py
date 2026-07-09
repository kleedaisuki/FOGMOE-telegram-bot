"""Add last_rotated_at to chat_records."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = '0002_add_chat_records_last_rotated_at'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
