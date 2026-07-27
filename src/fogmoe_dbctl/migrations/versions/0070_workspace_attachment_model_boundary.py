"""@brief 清理历史附件的模型派生边界 / Clean historical attachment model-derivation boundaries."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0070_workspace_attachment_model_boundary"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0069_workspace_runtimes"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 隔离历史 raw attachment 文本及其所有模型派生物 / Isolate historical raw attachment text and every model-derived artifact.

    @return None / None.
    @note 旧附件没有 ``RuntimeProcess.add_file`` 成功 receipt，因此迁移绝不伪造一个
        可执行的 ``<workspace_file>`` 路径。它保留不可变审计行，却把整 Turn 排除出未来
        Assistant 上下文、检索和画像重建，并删除可含 raw 文本的派生状态。旧群上下文读取没有
        durable 的逐读取 provenance，因此还会保守排除历史群 Assistant Turn：否则 raw 媒体
        caption 可能只作为先前模型回复或 summary 存活。/ Legacy attachments have no successful
        ``RuntimeProcess.add_file`` receipt, so this migration never fabricates an executable
        ``<workspace_file>`` path. It retains immutable audit rows, excludes the entire Turn from
        future Assistant context/retrieval/profile rebuilds, and deletes derived state that may
        contain raw text. Old group-context reads have no durable per-read provenance, so it also
        conservatively excludes historical group Assistant Turns: a raw media caption could
        otherwise survive only as an earlier model reply or summary.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝重新暴露已隔离 raw attachment 的回退 / Refuse a downgrade that would re-expose isolated raw attachments.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
