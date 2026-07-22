"""@brief 退役旧质押、BTC 预测与 RPG 持久化 / Retire legacy staking, BTC prediction, and RPG persistence."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0055_retire_legacy_wagers"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0054_bank_identity_projection_boundary"
"""@brief 前置 Bank 投影边界迁移 / Parent Bank projection-boundary migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 审计性退还未结算本金并移除退役结构 / Auditably refund unsettled principal and remove retired structures.

    @return None / None.
    @note 退款只通过平衡 Bank ledger 分录完成；绝不直接写入 identity 金币投影。/
        Refunds use balanced Bank-ledger entries only and never directly write the identity token
        projection.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝重建已明确退役的旧产品 / Refuse to recreate explicitly retired legacy products.

    @return None / None.
    @note 已创建的退款分录为不可变审计事实，不能在 downgrade 中删除。/
        Created refund entries are immutable audit facts and cannot be deleted by a downgrade.
    """

    run_migration_sql(__file__, "down")
