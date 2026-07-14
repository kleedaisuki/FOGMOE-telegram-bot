"""@brief 退役已回滚的资产动作确认状态机 / Retire the reverted asset-action confirmation state machine."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0060_retire_asset_action_confirmations"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0059_asset_action_confirmations"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 安全退役已回滚的确认表 / Safely retire the reverted confirmation table.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝无损重建已退役状态 / Refuse lossy reconstruction of retired state.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
