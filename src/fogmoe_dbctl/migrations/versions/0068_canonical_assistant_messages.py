"""@brief 迁移 Assistant 规范消息 V2 / Migrate canonical Assistant messages V2."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0068_canonical_assistant_messages"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0067_close_schema_creator_and_default_gaps"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 将持久化的 OpenAI 形消息迁移为 canonical V2 / Migrate persisted OpenAI-shaped messages to canonical V2.

    @return None / None.
    @note 本 revision 只重写 JSON 数据；不会拆分 append-only Conversation 行。/
        This revision rewrites JSON data only and never splits append-only Conversation rows.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 拒绝不可逆的 canonical V2 回退 / Reject irreversible canonical-V2 downgrade.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
