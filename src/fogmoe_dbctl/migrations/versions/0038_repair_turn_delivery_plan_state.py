"""@brief 修复多 effect 投递计划的遗留 Turn 状态 / Repair legacy Turn state for multi-effect delivery plans."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0038_repair_turn_delivery_plan_state"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0037_turn_delivery_plans"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 恢复仍有活动 effect 的投递计划状态 / Reopen delivery plans that still contain active effects.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 保持已修复数据，避免重新制造搁浅 effect / Keep repaired data to avoid recreating stranded effects.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
