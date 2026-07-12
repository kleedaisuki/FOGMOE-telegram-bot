"""@brief 增加治理开关命令回执 / Add moderation-toggle command receipts."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0034_toggle_command_receipts"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0033_rps_sessions"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立与 policy mutation 同事务的开关回执 / Create toggle receipts committed with policy mutations.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除治理开关回执并恢复 0033 / Drop moderation-toggle receipts and restore 0033.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
