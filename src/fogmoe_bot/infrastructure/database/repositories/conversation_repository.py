from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure.database import connection as db_connection


async def user_diary_exists(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
) -> bool:
    """@brief 判断用户日记是否存在 / Check whether user diary content exists.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 存在非空日记页返回 True / True when a non-empty diary page exists.
    """

    row = await db_connection.fetch_one(
        "SELECT 1 FROM conversation.ai_user_diary_pages "
        "WHERE user_id = %s AND content != '' LIMIT 1",
        (user_id,),
        connection=connection,
    )
    return bool(row)
