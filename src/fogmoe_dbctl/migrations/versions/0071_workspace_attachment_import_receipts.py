"""@brief 将 Workspace 附件 native 发布见证为 durable receipt / Witness Workspace-attachment native publication as a durable receipt."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0071_workspace_attachment_import_receipts"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0070_workspace_attachment_model_boundary"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立不可变附件 receipt 与 fail-closed 可见性状态机 / Create immutable attachment receipts and a fail-closed visibility state machine.

    @return None / None.
    @note 该 revision 在部署新 Python worker 前运行。它不从旧 placeholder 推断 native
        ``add_file`` 成功，所有缺少 durable receipt 的历史或 rollout 中行都终结为
        ``unavailable``。/ This revision runs before deploying the new Python worker. It never
        infers native ``add_file`` success from an old placeholder; every historical or rollout
        row without a durable receipt is terminalized as ``unavailable``.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝删除 native 发布审计事实的回退 / Refuse a downgrade that would delete native-publication audit facts.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
