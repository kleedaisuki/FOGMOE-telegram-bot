"""@brief 建立私聊个人 RPG 进度、探索与图鉴 / Establish private personal-RPG progression, exploration, and compendium."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0052_personal_rpg"
"""@brief 当前迁移版本 / Current migration revision."""

down_revision = "0051_verifiable_chance"
"""@brief 前置可验证随机活动迁移 / Parent verifiable-chance migration."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 创建个人角色、材料、每日探索、图鉴和不可变回执 / Create private characters, materials, daily explorations, collections, and immutable receipts.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 移除尚未对外启用的个人 RPG 存储 / Remove not-yet-public personal-RPG storage.

    @return None / None.
    """

    run_migration_sql(__file__, "down")
