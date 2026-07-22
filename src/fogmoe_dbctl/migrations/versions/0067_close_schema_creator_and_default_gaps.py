"""@brief 关闭 schema creator 与默认 ACL 证明缺口 / Close schema-creator and default-ACL proof gaps."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0067_close_schema_creator_and_default_gaps"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0066_validate_public_default_acl_owners"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 验证 schema CREATE 唯一性与 owner 显式安全默认 / Validate schema CREATE exclusivity and explicit safe owner defaults.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 回退无状态验证 revision / Revert the stateless validation revision.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
