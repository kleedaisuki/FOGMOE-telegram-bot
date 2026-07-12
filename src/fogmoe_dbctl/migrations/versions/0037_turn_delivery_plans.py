"""@brief 允许一个回合产生多个有序出站副作用 / Allow one Turn to produce multiple ordered outbound effects."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0037_turn_delivery_plans"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0036_media_picture_receipts"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 移除每 Turn 单条 outbox 的旧限制 / Remove the legacy one-outbox-per-Turn restriction."""

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复旧限制前拒绝多副作用回合 / Restore the legacy restriction only when data permits."""

    run_migration_sql(__file__, "down")
