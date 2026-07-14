"""@brief 删除已归档 Assistant 计费与善意赠币遗留结构 / Drop retired Assistant-billing and kindness-gift legacy structures."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0058_retire_assistant_legacy_billing"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0057_billing_renewal_order_integrity"
"""@brief 前置 Billing renewal-order 完整性迁移 / Parent Billing renewal-order integrity migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 删除已完成 0054 审计闭合的 Assistant 预留与无操作赠币表 / Drop Assistant reservations closed by 0054 audit and the no-op gift table.

    @return None / None.
    @note 迁移在删除前拒绝任何未闭合预留；既有用户余额已经由 Bank 账本和投影解释，
        不会被本迁移改写。/ The migration rejects any unresolved reservation before
        deletion; existing user balances are already explained by the Bank ledger and
        projection and are never rewritten here.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝重建已删除的历史事实 / Reject reconstruction of deleted historical facts.

    @return None / None.
    @note 删除的审计表与 kindness 记录没有无损逆变换；Bank 账本仍是货币历史的唯一
        权威。/ Deleted audit tables and kindness rows have no lossless inverse; the Bank
        ledger remains the sole authority for monetary history.
    """

    run_migration_sql(__file__, "down")
