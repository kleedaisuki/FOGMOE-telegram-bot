"""@brief 引入 durable media callback workflow / Introduce durable media callback workflows."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0023_media_workflow"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0022_economy_ledger"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建媒体 schema 与 callback 状态机 / Create the media schema and callback state machines.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除媒体工作流 / Remove media workflows.

    @return None / None.
    @note 未完成报价与音乐搜索快照会丢失 / Incomplete offers and music-search snapshots are discarded.
    """

    run_migration_sql(__file__, "down")
