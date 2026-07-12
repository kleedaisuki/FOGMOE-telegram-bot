"""@brief 增加 durable Crypto 工作流与幂等回执 / Add durable Crypto workflows and idempotency receipts."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0026_crypto_workflow"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0025_economy_remove_pool_fk"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立 Crypto receipts、swap 唯一性与预测恢复字段 / Create Crypto receipts, swap uniqueness, and prediction recovery fields.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复 0025 schema 并还原 legacy swap 冲突状态 / Restore the 0025 schema and legacy swap-conflict statuses.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
