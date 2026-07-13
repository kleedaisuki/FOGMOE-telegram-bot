"""@brief 建立独立情景检索并移除 checkpoint Memory / Establish episodic retrieval and remove checkpoint memory."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0041_episodic_retrieval"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0040_memory_context_boundaries"
"""@brief 前置迁移版本 / Parent migration revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立 pgvector retrieval schema / Establish the pgvector retrieval schema.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除 retrieval 并恢复旧 Memory 表形状 / Remove retrieval and restore the old Memory table shape.

    @return None / None.
    @note 旧 Memory 数据不会由 retrieval passage 反向合成 / Old Memory data is not synthesized from retrieval passages.
    """

    run_migration_sql(__file__, "down")
