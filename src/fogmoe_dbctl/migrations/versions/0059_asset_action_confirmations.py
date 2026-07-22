"""@brief 建立 Agent 资产动作确认状态机 / Create the Agent asset-action confirmation state machine."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0059_asset_action_confirmations"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0058_retire_assistant_legacy_billing"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立 owner 绑定的确认与 fenced 执行表 / Create owner-bound confirmation and fenced-execution table.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除资产确认状态机 / Drop the asset-confirmation state machine.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
