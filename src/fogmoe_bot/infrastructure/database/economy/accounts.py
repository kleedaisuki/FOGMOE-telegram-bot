"""@brief PostgreSQL 经济账户查询适配器 / PostgreSQL economy account-query adapter."""

from fogmoe_bot.application.economy.common import AccountLookup
from fogmoe_bot.infrastructure.database import db


class PostgresAccountLookup(AccountLookup):
    """@brief 查询经济账户是否存在 / Query whether an economy account exists."""

    async def account_exists(self, user_id: int) -> bool:
        """@brief 检查账户是否存在 / Check whether an account exists.

        @param user_id 用户 ID / User ID.
        @return 存在为 True / True when present.
        """

        row = await db.fetch_one(
            "SELECT 1 FROM identity.users WHERE id = %s",
            (user_id,),
        )
        return row is not None
