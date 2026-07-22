"""@brief 移除 credit posting 对支出 gate 的行锁耦合 / Remove row-lock coupling between credit postings and the debit gate."""

from fogmoe_dbctl.migrations.runner import run_migration_sql

revision = "0025_economy_remove_pool_fk"
"""@brief 当前 Alembic revision / Current Alembic revision."""

down_revision = "0024_conversation_resets"
"""@brief 前置 Alembic revision / Parent Alembic revision."""

branch_labels = None
"""@brief Alembic 分支标签 / Alembic branch labels."""

depends_on = None
"""@brief Alembic 额外依赖 / Additional Alembic dependencies."""


def upgrade() -> None:
    """@brief 删除会让 credit 取得 KEY SHARE 的外键 / Drop the foreign key that makes credits acquire KEY SHARE.

    @return None / None.
    """

    run_migration_sql(__file__, "up")


def downgrade() -> None:
    """@brief 恢复奖励池外键 / Restore the reward-pool foreign key.

    @return None / None.
    @note 恢复后 credit posting 会再次与 ``FOR UPDATE`` gate 相互阻塞 / Restored credits again conflict with the ``FOR UPDATE`` gate.
    """

    run_migration_sql(__file__, "down")
