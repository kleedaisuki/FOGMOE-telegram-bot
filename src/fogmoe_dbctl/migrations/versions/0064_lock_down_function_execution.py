"""@brief 收紧观测读模型与数据库函数边界 / Harden observability read models and database-function boundaries."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0064_lock_down_function_execution"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0063_observability_resource_liveness"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立聚合观测读模型并撤销 PUBLIC routine 执行能力 / Build aggregate observability read models and revoke PUBLIC routine execution.

    @return None / None.
    @note 同时修改迁移 owner 的全局函数默认权限，避免新函数再次向 PUBLIC 开放。/
        Also changes the migration owner's global function defaults so new functions
        are not exposed to PUBLIC again.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 回退聚合读模型并恢复旧 routine ACL / Revert aggregate read models and restore legacy routine ACLs.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
