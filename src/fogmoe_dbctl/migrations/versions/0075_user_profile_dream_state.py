"""@brief 补强 User Profile Dream 五态持久化矩阵 / Harden the persisted five-state User Profile Dream matrix."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0075_user_profile_dream_state"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0074_retrieval_vector_job_state"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 预检历史 Dream 并强制五态不变量 / Preflight historical Dreams and enforce five-state invariants.

    @return None / None.
    @note 迁移只读取历史业务行并增加约束；不删除数据，也不修改状态、结果、错误、
        counters、lease capability 或业务时间。任何无法由领域聚合唯一恢复的形状都会按
        分类计数终止整个事务。最终 DDL 锁必须在十秒有界维护窗口内一次取得，否则整个
        事务无副作用失败。/ The migration only reads historical business rows and adds
        constraints. It never deletes data or changes states, results, errors, counters, lease
        capabilities, or business timestamps. Any shape that cannot be restored uniquely by the
        domain aggregate aborts the whole transaction with a categorized count. The final DDL lock
        must be acquired once within a bounded ten-second maintenance window, or the transaction
        fails without side effects.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复 0043 的分散 nullable 约束 / Restore the separate nullable constraints from 0043.

    @return None / None.
    @note 降级在同样有界的最终 DDL 锁下只替换约束定义，不改写任何 Dream 行 / Under
        the same bounded final DDL lock, downgrade only replaces constraint definitions and never
        rewrites a Dream row.
    """

    run_migration_sql(__file__, "down")
