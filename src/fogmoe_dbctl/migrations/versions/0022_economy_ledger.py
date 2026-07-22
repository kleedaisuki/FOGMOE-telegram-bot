"""@brief 引入奖励池帖本与经济幂等回执 / Introduce the reward-pool ledger and economy idempotency receipts."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0022_economy_ledger"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0021_moderation_workflow"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建 economy ledger、OCC 与幂等结构 / Create economy ledger, OCC, and idempotency structures.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 将帖本净额折回旧余额并删除新结构 / Fold ledger net balances into legacy balances and remove new structures.

    @return None / None.
    @note 操作回执与商店保底历史会丢失 / Operation receipts and shop-pity history are discarded.
    """

    run_migration_sql(__file__, "down")
