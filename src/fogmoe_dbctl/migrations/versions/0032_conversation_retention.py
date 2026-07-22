"""@brief 增加 durable 会话 retention、compaction 与永久记忆投影 / Add durable conversation retention, compaction, and permanent-memory projection."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0032_conversation_retention"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0031_assistant_billing_reservations"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立 Segment queue、迁移旧历史并删除旧存储 / Create the segment queue, migrate legacy history, and remove legacy storage.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复 0031 schema 并保全 legacy archive 数据 / Restore the 0031 schema while preserving legacy archive data.

    @return None / None.
    @note 0031 无法表达新 cumulative checkpoint 的 active-context 语义；downgrade 将其保存为旧永久 archive。/
    Revision 0031 cannot express active-context semantics for new cumulative checkpoints; downgrade preserves them as legacy permanent archives.
    """

    run_migration_sql(__file__, "down")
