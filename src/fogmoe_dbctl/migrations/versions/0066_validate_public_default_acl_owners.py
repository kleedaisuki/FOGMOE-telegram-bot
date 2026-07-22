"""@brief 验证所有 owner 的 PUBLIC default ACL / Validate PUBLIC default ACLs for every owner."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0066_validate_public_default_acl_owners"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0065_close_public_user_object_access"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 对所有 owner 的未来 PUBLIC 权限执行失败关闭验证 / Fail closed on future PUBLIC privileges owned by any role.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 回退无状态验证 revision / Revert the stateless validation revision.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
