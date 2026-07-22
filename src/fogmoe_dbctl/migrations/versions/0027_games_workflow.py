"""@brief 增加 durable Games 会话、回执与 RPG OCC / Add durable Games sessions, receipts, and RPG OCC."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0027_games_workflow"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0026_crypto_workflow"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立游戏工作流、回执、冷却和 OCC / Create game workflows, receipts, cooldowns, and OCC.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复 0026 schema / Restore the 0026 schema.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
