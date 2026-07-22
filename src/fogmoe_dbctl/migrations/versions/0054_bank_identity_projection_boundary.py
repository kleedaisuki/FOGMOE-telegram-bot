"""@brief 封闭 identity 金币投影旁路并归档旧 Assistant 预留 / Close identity token-projection bypasses and archive legacy Assistant reservations."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0054_bank_identity_projection_boundary"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0053_bank_billing_hardening"
"""@brief 前置银行与 Billing 硬化迁移 / Parent bank and Billing hardening migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 安装投影守卫并审计性结清遗留预留 / Install projection guard and auditably settle legacy reservations.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除 0054 新增守卫，不回写不可变事实 / Remove 0054 guards without rewriting immutable facts.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
