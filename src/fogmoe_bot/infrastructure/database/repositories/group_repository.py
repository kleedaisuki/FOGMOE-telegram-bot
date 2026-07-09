import logging

from sqlalchemy.exc import SQLAlchemyError

from fogmoe_bot.infrastructure.database import mysql_connection

KNOWN_GROUP_ID_TABLES = (
    "group_keywords",
    "group_verification",
    "group_spam_control",
    "group_chart_tokens",
    "chat_records_group",
)


async def list_known_group_ids(*, connection=None) -> list[int]:
    """@brief 列出已知群组 ID / List known group IDs.

    @param connection 可选数据库连接 / Optional database connection.
    @return 去重后的群组 ID 列表 / Deduplicated group ID list.
    @note 单表读取失败时跳过该表，保持公告命令的历史容错行为 / A failed table read is skipped to preserve announcement command tolerance.
    """

    group_ids: set[int] = set()
    for table_name in KNOWN_GROUP_ID_TABLES:
        try:
            rows = await mysql_connection.fetch_all(
                f"SELECT DISTINCT group_id FROM {table_name}",
                connection=connection,
            )
        except SQLAlchemyError as exc:
            logging.warning("查询群组表 %s 时出错: %s", table_name, exc)
            continue
        group_ids.update(int(row[0]) for row in rows if row and row[0] is not None)
    return sorted(group_ids)
