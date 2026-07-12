"""@brief 增加 Assistant 计费预留状态机 / Add the Assistant billing-reservation state machine."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0031_assistant_billing_reservations"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0030_admin_announcements"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立 reserve→settle/release 账务事实 / Create reserve-to-settle-or-release billing facts.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 将活动预留转换回旧 eager-charge 语义并恢复 0030 / Convert active reservations to legacy eager-charge semantics and restore 0030.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
