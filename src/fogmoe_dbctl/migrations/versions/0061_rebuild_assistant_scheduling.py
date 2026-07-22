"""@brief 将旧 Assistant 日程重建为 Scheduling 聚合 / Rebuild legacy Assistant schedules as Scheduling aggregates."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0061_rebuild_assistant_scheduling"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0060_retire_asset_action_confirmations"
"""@brief 前置迁移版本 / Parent migration revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 原子迁移旧日程并删除旧表 / Atomically migrate legacy schedules and drop the old table.

    @return None / None.
    @note 迁移保留 schedule ID，将所有旧 naive 时间明确解释为 UTC，并把不再可信的
        executing lease 转为 retry_wait。/ The migration preserves schedule IDs, explicitly
        interprets every legacy naive timestamp as UTC, and converts no-longer-trustworthy
        executing leases to retry_wait.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝有损恢复旧调度模型 / Refuse lossy reconstruction of the legacy scheduler.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
