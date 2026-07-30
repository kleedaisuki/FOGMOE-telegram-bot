"""@brief 增加 Turn steer revision 与 generation-fenced checkpoint / Add Turn steer revisions and generation-fenced checkpoints."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0073_streaming_turn_steering"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0072_workspace_attachment_import_intents"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 原位演进 inference fencing、retry budget 与 checkpoint，不删除业务数据 / Evolve inference fencing, retry budget, and checkpoints in place without deleting business data.

    @return None / None.
    @note 既有 activity 与 checkpoint 精确回填为 revision/generation zero。历史
        attempt/error 快照无法重建 dependency 与普通失败的交错，因此所有存量 activity
        都获得新预算：这可能让活跃旧活动最多多运行一个配置预算窗口，但不会误终结
        durable 工作或删除业务数据。/ Existing activities and checkpoints are exactly
        backfilled as revision/generation zero. Historical attempt/error snapshots cannot
        reconstruct interleaved dependencies and ordinary failures, so every existing activity
        receives a fresh budget. This may let active legacy work run for at most one additional
        configured budget window, but cannot prematurely terminalize durable work or delete
        business data.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 仅在没有新 revision 或 retry-budget 数据时恢复旧 schema / Restore the old schema only when no new revision or retry-budget data exists.

    @return None / None.
    @note 非零 retry budget 是不可静默删除的业务状态 / Nonzero retry-budget usage is
        business state that must not be silently discarded.
    """

    run_migration_sql(__file__, "down")
