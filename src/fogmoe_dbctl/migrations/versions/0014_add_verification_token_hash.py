"""Add persisted verification-token digests."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0014_add_verification_token_hash"
down_revision = "0013_add_ai_schedule_recurrence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """@brief 添加 token 摘要列 / Add the token-digest column."""

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除 token 摘要列 / Drop the token-digest column."""

    run_migration_sql(__file__, "down")
