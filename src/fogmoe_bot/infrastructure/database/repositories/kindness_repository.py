from fogmoe_bot.infrastructure.database import connection as db_connection


async def fetch_latest_gift_for_recipient(recipient_id: int, *, connection=None):
    """@brief 读取收礼者最近一次善意赠礼 / Fetch a recipient's latest kindness gift.

    @param recipient_id 收礼者用户 ID / Recipient user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(amount, created_at)` 行；不存在时返回 None / `(amount, created_at)` row, or None.
    """

    return await db_connection.fetch_one(
        "SELECT amount, created_at FROM kindness_gifts "
        "WHERE recipient_id = %s ORDER BY created_at DESC LIMIT 1",
        (recipient_id,),
        connection=connection,
    )


async def insert_gift(recipient_id: int, amount: int, *, connection=None) -> None:
    """@brief 记录善意赠礼 / Insert a kindness gift.

    @param recipient_id 收礼者用户 ID / Recipient user ID.
    @param amount 赠礼硬币数量 / Gift coin amount.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO kindness_gifts (recipient_id, amount, created_at) VALUES (%s, %s, NOW())",
        (recipient_id, amount),
        connection=connection,
    )
