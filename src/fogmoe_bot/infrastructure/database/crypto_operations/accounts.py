"""PostgreSQL account reads required by Crypto entry points."""

from fogmoe_bot.application.crypto.workflow import AccountSnapshot
from fogmoe_bot.infrastructure.database import connection as db_connection


class PostgresCryptoAccountReader:
    """Read the minimal account snapshot exposed to Crypto use cases."""

    async def account_snapshot(self, user_id: int) -> AccountSnapshot:
        """Read registration state and total coin balance."""

        row = await db_connection.fetch_one(
            "SELECT coins, coins_paid FROM identity.users WHERE id = %s",
            (user_id,),
        )
        if row is None:
            return AccountSnapshot(False)
        return AccountSnapshot(True, int(row[0]) + int(row[1]))
