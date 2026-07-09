"""Add daily give limit tracking table."""

from fogmoe_bot.infrastructure.database.migrations.runner import run_migration_sql

revision = '0010_add_user_give_daily'
down_revision = '0009_add_recharge_blocked_until'
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
