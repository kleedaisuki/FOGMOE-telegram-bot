"""@brief 在 native add_file 前持久化 Workspace 附件导入意图 / Persist Workspace attachment-import intents before native add_file."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0072_workspace_attachment_import_intents"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0071_workspace_attachment_import_receipts"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建 immutable intent aggregate、回填既有 receipt 并收紧 receipt/unavailable gate / Create immutable intent aggregates, backfill existing receipts, and tighten receipt/unavailable gates.

    @return None / None.
    @note 部署必须先停止 0071 worker；0072 会从已 durable 的 receipt 精确回填 intent，随后
        只允许有先前 intent 的新 receipt。/ Deployment must first stop 0071 workers; 0072
        exactly backfills intents from already durable receipts, then permits new receipts only
        when a prior intent exists.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝删除恢复 provenance 的回退 / Refuse a downgrade that would delete recovery provenance.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
