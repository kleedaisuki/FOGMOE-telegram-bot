"""@brief 为检索建立强个人/群聊隔离域 / Establish strong personal/group retrieval scopes."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0044_retrieval_privacy_scopes"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0043_user_profile_dreaming"
"""@brief 前置迁移版本 / Parent migration revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 以可验证隔离域替换含糊 owner / Replace the ambiguous owner with a verifiable scope.

    @return None / None.
    @note 旧投影会被丢弃并由 worker 从 Conversation 重新生成 / Old projections are discarded and rebuilt from Conversation by the worker.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 清空新投影并恢复个人 owner schema / Clear new projections and restore the personal-owner schema.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
