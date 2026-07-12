"""@brief 增加崩溃一致的猜拳会话 / Add crash-consistent RPS sessions."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0033_rps_sessions"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0032_conversation_retention"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立 RPS 状态、玩家槽与事务事实 / Create RPS state, player slots, and transaction facts."""

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除 RPS 耐久状态并恢复 0032 / Remove durable RPS state and restore 0032."""

    run_migration_sql(__file__, "down")
