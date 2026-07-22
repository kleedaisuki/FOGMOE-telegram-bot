"""@brief 用持久心跳表达遥测资源存活性 / Express telemetry-resource liveness with durable heartbeats."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0063_observability_resource_liveness"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0062_retire_identity_mirrors_and_legacy_media"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 从历史信号回填并启用资源心跳 / Backfill resource heartbeats from historical signals and enable liveness tracking.

    @return None / None.
    @note 回填取资源起止时间与 logs、spans、metrics 的最新观测值。/
        The backfill takes the latest of resource lifecycle timestamps and observations
        from logs, spans, and metrics.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除可重建的资源心跳投影 / Remove the reconstructable resource-heartbeat projection.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
