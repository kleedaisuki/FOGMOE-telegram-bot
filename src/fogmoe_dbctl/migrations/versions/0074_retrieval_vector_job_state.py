"""@brief 补强 Retrieval 向量任务状态矩阵 / Harden the Retrieval vector-job state matrix."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0074_retrieval_vector_job_state"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0073_streaming_turn_steering"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 预检历史数据并补强向量状态 nullable 约束 / Preflight historical data and harden vector-state nullable constraints.

    @return None / None.
    @note 迁移只清理由非 processing 状态遗留的无效 lease capability；不删除任务、
        不修改状态、向量、完成结果、错误、counter 或业务时间。无法唯一解释的历史形状会以
        分类计数终止迁移。/ The migration clears only invalid lease capabilities retained by
        non-processing states. It never deletes jobs or changes status, vectors, completion
        results, errors, counters, or business timestamps. Historically ambiguous shapes abort
        with category counts.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复较宽松的 lease/result 约束 / Restore the less strict lease and result constraints.

    @return None / None.
    @note 已清理的非 processing lease 字段不代表有效 ownership，因而不会重建。/
        Cleared non-processing lease fields did not represent valid ownership and are not rebuilt.
    """

    run_migration_sql(__file__, "down")
