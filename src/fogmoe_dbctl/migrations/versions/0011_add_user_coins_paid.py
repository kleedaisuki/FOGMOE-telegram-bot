"""为用户表增加付费金币字段。"""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0011_add_user_coins_paid"
down_revision = "0010_add_user_give_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
