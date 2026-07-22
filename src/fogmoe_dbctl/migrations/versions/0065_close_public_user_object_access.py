"""@brief 关闭 PUBLIC 对用户对象的隐式访问 / Close implicit PUBLIC access to user objects."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0065_close_public_user_object_access"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0064_lock_down_function_execution"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 收敛 PUBLIC ACL 并以 schema 门控受信 vector 扩展 / Converge PUBLIC ACLs and gate the trusted vector extension by schema.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 明确拒绝无法安全重建 ACL 来源的回退 / Explicitly reject a downgrade that cannot safely reconstruct ACL provenance.

    @return None / None.
    @raise RuntimeError 迁移 SQL 始终以 PostgreSQL 0A000 拒绝 / Migration SQL always rejects with PostgreSQL ``0A000``.
    """

    run_migration_sql(__file__, "down")
