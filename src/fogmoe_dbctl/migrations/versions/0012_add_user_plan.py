"""为用户表增加套餐字段。"""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = '0012_add_user_plan'
down_revision = '0011_add_user_coins_paid'
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_migration_sql(__file__, "up")


def downgrade() -> None:
    run_migration_sql(__file__, "down")
