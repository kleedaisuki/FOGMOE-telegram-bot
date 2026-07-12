"""Add recharge block column to user table."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0009_add_recharge_blocked_until"
down_revision = "0008_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
