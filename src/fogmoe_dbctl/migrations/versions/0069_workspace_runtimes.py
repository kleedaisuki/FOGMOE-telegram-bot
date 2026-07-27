"""@brief 建立可恢复 Workspace runtime 身份 / Establish recoverable Workspace runtime identities."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0069_workspace_runtimes"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0068_canonical_assistant_messages"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 排空遗留代码执行后建立 Workspace runtime 映射 / Drain legacy code execution, then establish Workspace runtime mappings.

    @return None / None.
    @note 该 revision 保留已成功的 ``execute_python_code`` 历史审计，但拒绝尚可能被旧
        binary 重放的 inference 或 receipt。/ This revision retains successful
        ``execute_python_code`` historical audit data but rejects inference or receipts that an
        old binary could still replay.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝丢失 host 可恢复性映射的回退 / Reject a downgrade that would lose host recoverability mappings.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
