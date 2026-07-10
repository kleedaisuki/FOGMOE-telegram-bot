from fogmoe_bot.domain.automation import KeywordReply
from fogmoe_bot.domain.moderation import (
    ChatId,
    GroupModerationPolicy,
    ModerationRule,
    RuleKind,
    RuleScope,
    MessageId,
    UserId,
    VerificationTask,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


async def fetch_group_keywords(
    group_id: int,
    *,
    connection=None,
) -> tuple[KeywordReply, ...]:
    """@brief 读取群组关键词回复 / Fetch group keyword responses.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 类型化关键词自动回复 / Typed keyword replies.
    """

    rows = await db_connection.fetch_all(
        "SELECT keyword, response FROM group_keywords WHERE group_id = %s",
        (group_id,),
        connection=connection,
    )
    return tuple(
        KeywordReply(keyword=str(keyword), response=str(response))
        for keyword, response in rows
    )


async def group_keyword_exists(group_id: int, keyword: str, *, connection=None) -> bool:
    """@brief 判断群组关键词是否存在 / Check whether a group keyword exists.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param keyword 关键词 / Keyword.
    @param connection 可选数据库连接 / Optional database connection.
    @return 存在返回 True / True when the keyword exists.
    """

    row = await db_connection.fetch_one(
        "SELECT keyword FROM group_keywords WHERE group_id = %s AND keyword = %s",
        (group_id, keyword),
        connection=connection,
    )
    return bool(row)


async def count_group_keywords(group_id: int, *, connection=None) -> int:
    """@brief 统计群组关键词数量 / Count group keywords.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 关键词数量 / Keyword count.
    """

    row = await db_connection.fetch_one(
        "SELECT COUNT(*) FROM group_keywords WHERE group_id = %s",
        (group_id,),
        connection=connection,
    )
    return int(row[0] or 0) if row else 0


async def upsert_group_keyword(
    group_id: int,
    keyword: str,
    response: str,
    created_by: int,
    *,
    connection=None,
) -> None:
    """@brief 写入群组关键词回复 / Upsert a group keyword response.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param keyword 关键词 / Keyword.
    @param response 回复内容 / Response text.
    @param created_by 创建者用户 ID / Creator user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO group_keywords (group_id, keyword, response, created_by) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (group_id, keyword) DO UPDATE SET "
        "response = EXCLUDED.response, created_by = EXCLUDED.created_by",
        (group_id, keyword, response, created_by),
        connection=connection,
    )


async def delete_group_keyword(group_id: int, keyword: str, *, connection=None) -> int:
    """@brief 删除群组关键词 / Delete a group keyword.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param keyword 关键词 / Keyword.
    @param connection 可选数据库连接 / Optional database connection.
    @return 删除行数 / Deleted row count.
    """

    return await db_connection.execute(
        "DELETE FROM group_keywords WHERE group_id = %s AND keyword = %s",
        (group_id, keyword),
        connection=connection,
    )


async def fetch_spam_control(
    group_id: int,
    *,
    connection=None,
) -> GroupModerationPolicy | None:
    """@brief 读取垃圾控制配置 / Fetch spam control settings.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 类型化群组审核策略；不存在时返回 None / Typed policy, or None.
    """

    row = await db_connection.fetch_one(
        "SELECT enabled, block_links, block_mentions FROM group_spam_control WHERE group_id = %s",
        (group_id,),
        connection=connection,
    )
    if not row:
        return None
    return GroupModerationPolicy(
        chat_id=ChatId(group_id),
        enabled=bool(row[0]),
        block_links=bool(row[1]),
        block_mentions=bool(row[2]),
    )


async def upsert_spam_enabled(
    group_id: int,
    enabled: bool,
    block_links: bool,
    block_mentions: bool,
    enabled_by: int,
    *,
    connection=None,
) -> None:
    """@brief 设置垃圾控制开关 / Set spam control enabled state.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param enabled 是否启用 / Whether spam control is enabled.
    @param block_links 是否过滤链接 / Whether to block links.
    @param block_mentions 是否过滤提及 / Whether to block mentions.
    @param enabled_by 操作者用户 ID / Operator user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO group_spam_control "
        "(group_id, enabled, block_links, block_mentions, enabled_by) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (group_id) DO UPDATE SET enabled = EXCLUDED.enabled, "
        "block_links = EXCLUDED.block_links, block_mentions = EXCLUDED.block_mentions, "
        "enabled_by = EXCLUDED.enabled_by, updated_at = CURRENT_TIMESTAMP",
        (group_id, enabled, block_links, block_mentions, enabled_by),
        connection=connection,
    )


async def set_spam_link_blocking(group_id: int, enabled: bool, *, connection=None) -> None:
    """@brief 设置链接过滤 / Set link blocking.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param enabled 是否启用 / Whether enabled.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE group_spam_control SET block_links = %s, updated_at = CURRENT_TIMESTAMP "
        "WHERE group_id = %s",
        (enabled, group_id),
        connection=connection,
    )


async def set_spam_mention_blocking(group_id: int, enabled: bool, *, connection=None) -> None:
    """@brief 设置提及过滤 / Set mention blocking.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param enabled 是否启用 / Whether enabled.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE group_spam_control SET block_mentions = %s, updated_at = CURRENT_TIMESTAMP "
        "WHERE group_id = %s",
        (enabled, group_id),
        connection=connection,
    )


async def fetch_group_spam_keywords(
    group_id: int,
    *,
    connection=None,
) -> tuple[ModerationRule, ...]:
    """@brief 读取群组自定义垃圾词 / Fetch group spam keywords.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 类型化群组审核规则 / Typed group moderation rules.
    """

    rows = await db_connection.fetch_all(
        "SELECT keyword, is_regex FROM group_spam_keywords WHERE group_id = %s",
        (group_id,),
        connection=connection,
    )
    return tuple(
        ModerationRule(
            pattern=str(keyword),
            kind=RuleKind.REGEX if bool(is_regex) else RuleKind.LITERAL,
            scope=RuleScope.GROUP,
        )
        for keyword, is_regex in rows
    )


async def group_spam_keyword_exists(group_id: int, keyword: str, *, connection=None) -> bool:
    """@brief 判断自定义垃圾词是否存在 / Check whether a custom spam keyword exists.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param keyword 垃圾词 / Spam keyword.
    @param connection 可选数据库连接 / Optional database connection.
    @return 存在返回 True / True when present.
    """

    row = await db_connection.fetch_one(
        "SELECT id FROM group_spam_keywords WHERE group_id = %s AND keyword = %s",
        (group_id, keyword),
        connection=connection,
    )
    return bool(row)


async def count_group_spam_keywords(group_id: int, *, connection=None) -> int:
    """@brief 统计自定义垃圾词数量 / Count custom spam keywords.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 自定义垃圾词数量 / Custom spam keyword count.
    """

    row = await db_connection.fetch_one(
        "SELECT COUNT(*) FROM group_spam_keywords WHERE group_id = %s",
        (group_id,),
        connection=connection,
    )
    return int(row[0] or 0) if row else 0


async def upsert_group_spam_keyword(
    group_id: int,
    keyword: str,
    is_regex: bool,
    created_by: int,
    *,
    connection=None,
) -> None:
    """@brief 写入自定义垃圾词 / Upsert a custom spam keyword.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param keyword 垃圾词 / Spam keyword.
    @param is_regex 是否正则 / Whether the keyword is regex.
    @param created_by 创建者用户 ID / Creator user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO group_spam_keywords (group_id, keyword, is_regex, created_by) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (group_id, keyword) DO UPDATE SET "
        "is_regex = EXCLUDED.is_regex, created_by = EXCLUDED.created_by",
        (group_id, keyword, is_regex, created_by),
        connection=connection,
    )


async def delete_group_spam_keyword(group_id: int, keyword: str, *, connection=None) -> int:
    """@brief 删除自定义垃圾词 / Delete a custom spam keyword.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param keyword 垃圾词 / Spam keyword.
    @param connection 可选数据库连接 / Optional database connection.
    @return 删除行数 / Deleted row count.
    """

    return await db_connection.execute(
        "DELETE FROM group_spam_keywords WHERE group_id = %s AND keyword = %s",
        (group_id, keyword),
        connection=connection,
    )


async def verification_group_exists(group_id: int, *, connection=None) -> bool:
    """@brief 判断群组验证是否启用 / Check whether group verification is enabled.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 启用返回 True / True when enabled.
    """

    row = await db_connection.fetch_one(
        "SELECT group_id FROM group_verification WHERE group_id = %s",
        (group_id,),
        connection=connection,
    )
    return bool(row)


async def enable_group_verification(group_id: int, group_name: str, *, connection=None) -> None:
    """@brief 开启群组验证 / Enable group verification.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param group_name 群组名称 / Group name.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO group_verification (group_id, group_name) VALUES (%s, %s) "
        "ON CONFLICT (group_id) DO UPDATE SET group_name = EXCLUDED.group_name",
        (group_id, group_name),
        connection=connection,
    )


async def disable_group_verification(group_id: int, *, connection=None) -> None:
    """@brief 关闭群组验证 / Disable group verification.

    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "DELETE FROM group_verification WHERE group_id = %s",
        (group_id,),
        connection=connection,
    )


async def upsert_verification_task(
    user_id: int,
    group_id: int,
    message_id: int,
    expire_time,
    token_hash: str,
    *,
    connection=None,
) -> None:
    """@brief 写入成员验证任务 / Upsert a member verification task.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param group_id Telegram 群组 ID / Telegram group ID.
    @param message_id 验证消息 ID / Verification message ID.
    @param expire_time 过期时间 / Expiry time.
    @param token_hash 验证 token 摘要 / Verification-token digest.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO verification_tasks (user_id, group_id, message_id, expire_time, token_hash) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id, group_id) DO UPDATE SET "
        "message_id = EXCLUDED.message_id, expire_time = EXCLUDED.expire_time, "
        "token_hash = EXCLUDED.token_hash",
        (user_id, group_id, message_id, expire_time, token_hash),
        connection=connection,
    )


async def delete_verification_task(user_id: int, group_id: int, *, connection=None) -> None:
    """@brief 删除成员验证任务 / Delete a member verification task.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param group_id Telegram 群组 ID / Telegram group ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "DELETE FROM verification_tasks WHERE user_id = %s AND group_id = %s",
        (user_id, group_id),
        connection=connection,
    )


async def fetch_verification_task(
    user_id: int,
    group_id: int,
    *,
    connection=None,
) -> VerificationTask | None:
    """@brief 读取单项成员验证任务 / Fetch one member-verification task.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param group_id Telegram 群组 ID / Telegram chat ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 验证任务；不存在返回 None / Verification task, or None.
    """

    row = await db_connection.fetch_one(
        "SELECT message_id, expire_time, token_hash FROM verification_tasks "
        "WHERE user_id = %s AND group_id = %s",
        (user_id, group_id),
        connection=connection,
    )
    if not row:
        return None
    return VerificationTask(
        chat_id=ChatId(group_id),
        user_id=UserId(user_id),
        message_id=MessageId(int(row[0])),
        expires_at=row[1],
        token_hash=str(row[2] or ""),
    )


async def fetch_active_verification_tasks(
    now,
    *,
    connection=None,
) -> tuple[VerificationTask, ...]:
    """@brief 读取未过期验证任务 / Fetch active verification tasks.

    @param now 当前时间 / Current time.
    @param connection 可选数据库连接 / Optional database connection.
    @return 类型化未过期验证任务 / Typed active verification tasks.
    """

    rows = await db_connection.fetch_all(
        "SELECT user_id, group_id, message_id, expire_time, token_hash "
        "FROM verification_tasks WHERE expire_time > %s",
        (now,),
        connection=connection,
    )
    return tuple(
        VerificationTask(
            user_id=UserId(int(user_id)),
            chat_id=ChatId(int(group_id)),
            message_id=MessageId(int(message_id)),
            expires_at=expire_time,
            token_hash=str(token_hash or ""),
        )
        for user_id, group_id, message_id, expire_time, token_hash in rows
    )


async def fetch_pending_verification_tasks(
    *,
    connection=None,
) -> tuple[VerificationTask, ...]:
    """@brief 读取所有待恢复验证任务 / Fetch every verification task for recovery.

    @param connection 可选数据库连接 / Optional database connection.
    @return 类型化验证任务，包括已到期但未处置的任务 / Typed tasks, including overdue tasks.
    """

    rows = await db_connection.fetch_all(
        "SELECT user_id, group_id, message_id, expire_time, token_hash "
        "FROM verification_tasks",
        connection=connection,
    )
    return tuple(
        VerificationTask(
            user_id=UserId(int(user_id)),
            chat_id=ChatId(int(group_id)),
            message_id=MessageId(int(message_id)),
            expires_at=expire_time,
            token_hash=str(token_hash or ""),
        )
        for user_id, group_id, message_id, expire_time, token_hash in rows
    )


async def fetch_developer_stats(limit: int = 20):
    """@brief 读取开发者统计信息 / Fetch developer statistics.

    @param limit 群组列表最大数量 / Maximum group IDs to include.
    @return 统计字典 / Statistics dictionary.
    """

    user_row = await db_connection.fetch_one("SELECT COUNT(*) as count FROM users", mapping=True)
    keyword_row = await db_connection.fetch_one(
        "SELECT COUNT(DISTINCT group_id) as count FROM group_keywords",
        mapping=True,
    )
    verify_row = await db_connection.fetch_one(
        "SELECT COUNT(*) as count FROM group_verification",
        mapping=True,
    )
    spam_row = await db_connection.fetch_one(
        "SELECT COUNT(*) as count FROM group_spam_control WHERE enabled = TRUE",
        mapping=True,
    )
    chart_row = await db_connection.fetch_one(
        "SELECT COUNT(DISTINCT group_id) as count FROM group_chart_tokens",
        mapping=True,
    )
    keyword_groups = await db_connection.fetch_all(
        "SELECT DISTINCT group_id FROM group_keywords LIMIT %s",
        (limit,),
        mapping=True,
    )
    verify_groups = await db_connection.fetch_all(
        "SELECT group_id FROM group_verification LIMIT %s",
        (limit,),
        mapping=True,
    )
    spam_groups = await db_connection.fetch_all(
        "SELECT group_id FROM group_spam_control WHERE enabled = TRUE LIMIT %s",
        (limit,),
        mapping=True,
    )
    chart_groups = await db_connection.fetch_all(
        "SELECT DISTINCT group_id FROM group_chart_tokens LIMIT %s",
        (limit,),
        mapping=True,
    )
    recent_users = await db_connection.fetch_all(
        "SELECT id, name FROM users ORDER BY id DESC LIMIT 10",
        mapping=True,
    )

    return {
        "user_count": user_row["count"] if user_row else 0,
        "keyword_group_count": keyword_row["count"] if keyword_row else 0,
        "verify_group_count": verify_row["count"] if verify_row else 0,
        "spam_group_count": spam_row["count"] if spam_row else 0,
        "chart_group_count": chart_row["count"] if chart_row else 0,
        "keyword_group_ids": [str(row["group_id"]) for row in keyword_groups],
        "verify_group_ids": [str(row["group_id"]) for row in verify_groups],
        "spam_group_ids": [str(row["group_id"]) for row in spam_groups],
        "chart_group_ids": [str(row["group_id"]) for row in chart_groups],
        "recent_users": recent_users,
    }
