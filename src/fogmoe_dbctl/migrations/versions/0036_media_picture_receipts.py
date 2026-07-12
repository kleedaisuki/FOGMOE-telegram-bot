"""@brief 建立图片请求回执与 transactional photo outbox / Add picture-request receipts and transactional photo delivery."""

from fogmoe_dbctl.migrations.runner import run_migration_sql


revision = "0036_media_picture_receipts"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0035_group_message_projection"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 建立图片 source receipt / Create picture source receipts."""

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 删除图片 source receipt / Drop picture source receipts."""

    run_migration_sql(__file__, "down")
