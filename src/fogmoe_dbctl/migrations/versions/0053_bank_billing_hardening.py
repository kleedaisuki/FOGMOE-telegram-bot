"""@brief 加固银行账本投影与支付成功去重 / Harden bank-ledger projections and successful-payment deduplication."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0053_bank_billing_hardening"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0052_personal_rpg"
"""@brief 前置个人 RPG 迁移 / Parent personal-RPG migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 安装账本与支付防重复计费约束 / Install ledger and payment anti-duplicate-charge constraints.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除本迁移新增的账本与支付硬化 / Remove this migration's ledger and payment hardening.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
