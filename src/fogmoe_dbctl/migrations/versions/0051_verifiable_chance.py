"""@brief 建立可验证随机活动轮次与回执 / Establish verifiable chance rounds and receipts."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0051_verifiable_chance"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0050_group_town"
"""@brief 前置群组小镇迁移 / Parent group-town migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建承诺揭示轮次、单向结算与幂等回执 / Create commit-reveal rounds, one-way settlement, and idempotency receipts.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除尚未对外启用的随机活动存储 / Remove not-yet-public chance storage.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
