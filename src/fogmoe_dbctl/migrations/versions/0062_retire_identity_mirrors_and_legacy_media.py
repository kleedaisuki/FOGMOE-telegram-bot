"""@brief 退役 identity 镜像与旧图片经济状态 / Retire identity mirrors and legacy picture economic state."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0062_retire_identity_mirrors_and_legacy_media"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0061_rebuild_assistant_scheduling"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 删除可证明冗余的身份镜像和已结清媒体状态 / Drop provably redundant identity mirrors and settled media state.

    @return None / None.
    @note 迁移在删除前验证 Bank 与 identity 金额一致，并只允许有明确 delivered 或
        refunded 证据的图片经济记录通过。/ Before dropping data, the migration verifies
        Bank/identity monetary equality and permits picture economic records only with explicit
        delivered or refunded evidence.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝伪造已删除的业务事实 / Refuse to fabricate deleted business facts.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
