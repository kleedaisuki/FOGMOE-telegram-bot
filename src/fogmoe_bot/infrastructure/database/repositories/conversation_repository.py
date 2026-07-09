import json
from typing import Any

from fogmoe_bot.infrastructure.database import mysql_connection


async def user_diary_exists(user_id: int, *, connection=None) -> bool:
    """@brief 判断用户日记是否存在 / Check whether user diary content exists.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 存在非空日记页返回 True / True when a non-empty diary page exists.
    """

    row = await mysql_connection.fetch_one(
        "SELECT 1 FROM ai_user_diary_pages WHERE user_id = %s AND content != '' LIMIT 1",
        (user_id,),
        connection=connection,
    )
    return bool(row)


async def fetch_chat_messages_raw(conversation_id: int, *, connection=None) -> Any:
    """@brief 读取聊天记录原始 JSON / Fetch raw chat message JSON.

    @param conversation_id 会话 ID / Conversation ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 原始 messages 值；不存在时返回 None / Raw messages value, or None when absent.
    """

    row = await mysql_connection.fetch_one(
        "SELECT messages FROM chat_records WHERE conversation_id = %s",
        (conversation_id,),
        connection=connection,
    )
    return row[0] if row else None


def normalise_snapshot_text(raw_snapshot: Any) -> str | None:
    """@brief 规范化归档快照文本 / Normalise archived snapshot text.

    @param raw_snapshot 原始快照 / Raw snapshot value.
    @return JSON 文本；无内容时返回 None / JSON text, or None when empty.
    """

    if not raw_snapshot:
        return None
    if isinstance(raw_snapshot, bytes):
        return raw_snapshot.decode("utf-8")
    if isinstance(raw_snapshot, (dict, list)):
        return json.dumps(raw_snapshot, ensure_ascii=False)
    return str(raw_snapshot)


async def insert_permanent_snapshot(user_id: int, snapshot_text: str, *, connection=None) -> None:
    """@brief 插入永久记忆快照 / Insert permanent memory snapshot.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param snapshot_text 快照 JSON 文本 / Snapshot JSON text.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO permanent_chat_records (user_id, conversation_snapshot) VALUES (%s, %s)",
        (user_id, snapshot_text),
        connection=connection,
    )


async def delete_chat_record(conversation_id: int, *, connection=None) -> None:
    """@brief 删除当前聊天记录 / Delete current chat record.

    @param conversation_id 会话 ID / Conversation ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "DELETE FROM chat_records WHERE conversation_id = %s",
        (conversation_id,),
        connection=connection,
    )


async def archive_and_clear_chat(user_id: int, conversation_id: int) -> tuple[bool, list[dict]]:
    """@brief 归档并清空聊天记录 / Archive and clear chat records.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param conversation_id 会话 ID / Conversation ID.
    @return `(是否创建快照, 被裁剪归档)` / `(snapshot_created, pruned_archives)`.
    """

    snapshot_created = False
    archived_records: list[dict] = []
    async with mysql_connection.transaction() as connection:
        raw_snapshot = await fetch_chat_messages_raw(conversation_id, connection=connection)
        snapshot_text = normalise_snapshot_text(raw_snapshot)
        if snapshot_text:
            await insert_permanent_snapshot(user_id, snapshot_text, connection=connection)
            snapshot_created = True
            archived_records = await mysql_connection.prune_permanent_records(
                user_id,
                connection=connection,
            )
        await delete_chat_record(conversation_id, connection=connection)
    return snapshot_created, archived_records


async def fetch_pending_permanent_snapshot(user_id: int):
    """@brief 读取待摘要永久快照 / Fetch pending permanent snapshot.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @return `(record_id, snapshot_text)`；不存在时返回 None / `(record_id, snapshot_text)`, or None when absent.
    """

    row = await mysql_connection.fetch_one(
        "SELECT id, conversation_snapshot FROM permanent_chat_records "
        "WHERE user_id = %s AND (summary IS NULL OR summary = '') "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (user_id,),
    )
    if not row:
        return None
    snapshot = normalise_snapshot_text(row[1])
    if snapshot is None:
        return None
    return row[0], snapshot


async def update_permanent_summary(record_id: int, summary_text: str) -> None:
    """@brief 更新永久快照摘要 / Update permanent snapshot summary.

    @param record_id 永久记录 ID / Permanent record ID.
    @param summary_text 摘要文本 / Summary text.
    @return None / None.
    """

    await mysql_connection.execute(
        "UPDATE permanent_chat_records SET summary = %s WHERE id = %s",
        (summary_text, record_id),
    )


async def count_summarised_permanent_records(user_id: int) -> int:
    """@brief 统计已有摘要的永久记录 / Count summarised permanent records.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @return 已摘要记录数 / Summarised record count.
    """

    row = await mysql_connection.fetch_one(
        "SELECT COUNT(*) FROM permanent_chat_records WHERE user_id = %s "
        "AND summary IS NOT NULL AND summary != ''",
        (user_id,),
    )
    return int(row[0] or 0) if row else 0


async def fetch_summarised_permanent_records(user_id: int, *, limit: int, offset: int):
    """@brief 读取已有摘要的永久记录 / Fetch summarised permanent records.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param limit 最大返回数量 / Maximum rows to return.
    @param offset 偏移量 / Row offset.
    @return 数据库结果行 / Database rows.
    """

    return await mysql_connection.fetch_all(
        """
        SELECT id, summary, created_at
        FROM permanent_chat_records
        WHERE user_id = %s AND summary IS NOT NULL AND summary != ''
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s
        """,
        (user_id, limit, offset),
    )


async def count_permanent_records(user_id: int) -> int:
    """@brief 统计永久记录 / Count permanent records.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @return 永久记录数量 / Permanent record count.
    """

    row = await mysql_connection.fetch_one(
        "SELECT COUNT(*) FROM permanent_chat_records WHERE user_id = %s",
        (user_id,),
    )
    return int(row[0] or 0) if row else 0


async def fetch_permanent_records_batch(
    user_id: int,
    *,
    newest_first: bool,
    limit: int,
    offset: int,
):
    """@brief 分批读取永久记录 / Fetch permanent records in batches.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param newest_first 是否按新到旧排序 / Whether to sort newest first.
    @param limit 最大返回数量 / Maximum rows to return.
    @param offset 偏移量 / Row offset.
    @return 数据库结果行 / Database rows.
    """

    order_clause = "ORDER BY created_at DESC, id DESC" if newest_first else "ORDER BY created_at ASC, id ASC"
    return await mysql_connection.fetch_all(
        f"""
        SELECT id, conversation_snapshot, created_at
        FROM permanent_chat_records
        WHERE user_id = %s
        {order_clause}
        LIMIT %s OFFSET %s
        """,
        (user_id, limit, offset),
    )


async def fetch_max_diary_page(user_id: int) -> int:
    """@brief 读取最大日记页码 / Fetch maximum diary page number.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @return 最大页码；无日记时为 0 / Maximum page number, or 0 when absent.
    """

    row = await mysql_connection.fetch_one(
        "SELECT MAX(page_no) FROM ai_user_diary_pages WHERE user_id = %s",
        (user_id,),
    )
    return int(row[0] or 0) if row else 0


async def fetch_diary_page(user_id: int, page_no: int):
    """@brief 读取日记页 / Fetch diary page.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param page_no 页码 / Page number.
    @return 数据库结果行；不存在时返回 None / Database row, or None when absent.
    """

    return await mysql_connection.fetch_one(
        "SELECT content, created_at, updated_at FROM ai_user_diary_pages "
        "WHERE user_id = %s AND page_no = %s",
        (user_id, page_no),
    )


async def upsert_diary_page(user_id: int, page_no: int, content: str) -> None:
    """@brief 写入日记页 / Upsert diary page.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param page_no 页码 / Page number.
    @param content 日记内容 / Diary content.
    @return None / None.
    """

    await mysql_connection.execute(
        """
        INSERT INTO ai_user_diary_pages (user_id, page_no, content)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE content = VALUES(content), updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, page_no, content),
    )


async def insert_group_message(
    group_id: int,
    message_id: int,
    user_id: int | None,
    message_type: str,
    content: str,
    created_at,
    *,
    connection=None,
) -> None:
    """@brief 插入群聊消息记录 / Insert a group chat message record.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param message_id Telegram 消息 ID / Telegram message ID.
    @param user_id 发送者用户 ID / Sender user ID.
    @param message_type 消息类型 / Message type.
    @param content 消息内容 / Message content.
    @param created_at 创建时间 / Creation timestamp.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO chat_records_group "
        "(group_id, message_id, user_id, message_type, content, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (group_id, message_id, user_id, message_type, content, created_at),
        connection=connection,
    )


async def prune_group_history(
    group_id: int,
    *,
    keep: int = 100,
    connection=None,
) -> None:
    """@brief 裁剪群聊历史 / Prune group chat history.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param keep 保留的最新消息数量 / Number of newest messages to keep.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "DELETE FROM chat_records_group "
        "WHERE group_id = %s AND id NOT IN ("
        "  SELECT id FROM ("
        "    SELECT id FROM chat_records_group "
        "    WHERE group_id = %s "
        "    ORDER BY created_at DESC, id DESC "
        "    LIMIT %s"
        "  ) AS recent"
        ")",
        (group_id, group_id, keep),
        connection=connection,
    )


async def fetch_group_context_rows(
    group_id: int,
    around_message_id: int | None,
    window_size: int,
    *,
    connection=None,
):
    """@brief 读取群聊上下文原始行 / Fetch raw group context rows.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param around_message_id 中心消息 ID / Center message ID.
    @param window_size 窗口大小 / Context window size.
    @param connection 可选数据库连接 / Optional database connection.
    @return 映射行列表 / List of mapping rows.
    """

    select_columns = (
        "SELECT cr.id, cr.message_id, cr.user_id, cr.message_type, "
        "cr.content, cr.created_at, u.name AS username "
        "FROM chat_records_group cr "
        "LEFT JOIN user u ON u.id = cr.user_id "
    )
    if around_message_id:
        before = await mysql_connection.fetch_all(
            select_columns
            + "WHERE group_id = %s AND message_id <= %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (group_id, around_message_id, window_size),
            mapping=True,
            connection=connection,
        )
        after = await mysql_connection.fetch_all(
            select_columns
            + "WHERE group_id = %s AND message_id > %s "
            "ORDER BY created_at ASC, id ASC LIMIT %s",
            (group_id, around_message_id, window_size),
            mapping=True,
            connection=connection,
        )
        return list(reversed(before)) + list(after)

    records = await mysql_connection.fetch_all(
        select_columns
        + "WHERE group_id = %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        (group_id, window_size),
        mapping=True,
        connection=connection,
    )
    return list(reversed(records))
