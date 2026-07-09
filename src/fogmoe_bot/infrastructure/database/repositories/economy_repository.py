from fogmoe_bot.infrastructure.database import mysql_connection


async def fetch_user_checkin(user_id: int, *, connection=None):
    """@brief 读取用户签到状态 / Fetch user check-in state.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(last_checkin_date, consecutive_days)` 行；不存在时返回 None / Check-in row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT last_checkin_date, consecutive_days FROM user_checkin WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )


async def upsert_user_checkin(
    user_id: int,
    checkin_date,
    consecutive_days: int,
    *,
    connection=None,
) -> None:
    """@brief 写入用户签到状态 / Upsert user check-in state.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param checkin_date 签到日期 / Check-in date.
    @param consecutive_days 连续签到天数 / Consecutive days.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO user_checkin (user_id, last_checkin_date, consecutive_days) "
        "VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE last_checkin_date = VALUES(last_checkin_date), "
        "consecutive_days = VALUES(consecutive_days)",
        (user_id, checkin_date, consecutive_days),
        connection=connection,
    )


async def user_task_completed(user_id: int, task_id: int, *, connection=None) -> bool:
    """@brief 判断用户任务是否完成 / Check whether a user task is completed.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param task_id 任务 ID / Task ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 已完成返回 True / True when completed.
    """

    row = await mysql_connection.fetch_one(
        "SELECT 1 FROM user_task WHERE user_id = %s AND task_id = %s",
        (user_id, task_id),
        connection=connection,
    )
    return bool(row)


async def insert_user_task(user_id: int, task_id: int, *, connection=None) -> None:
    """@brief 记录用户任务完成 / Record user task completion.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param task_id 任务 ID / Task ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO user_task (user_id, task_id) VALUES (%s, %s)",
        (user_id, task_id),
        connection=connection,
    )


async def fetch_web_password(user_id: int, *, connection=None):
    """@brief 读取 Web 密码记录 / Fetch web password record.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 映射行；不存在时返回 None / Mapping row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT password, created_at, updated_at FROM web_password WHERE user_id = %s",
        (user_id,),
        mapping=True,
        connection=connection,
    )


async def upsert_web_password(user_id: int, password_hash: str, *, connection=None) -> None:
    """@brief 写入 Web 密码 / Upsert web password.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param password_hash 密码哈希 / Password hash.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO web_password (user_id, password) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE password = VALUES(password)",
        (user_id, password_hash),
        connection=connection,
    )


async def ensure_stake_reward_pool(pool_id: int, *, connection=None) -> None:
    """@brief 确保质押奖励池存在 / Ensure stake reward pool row exists.

    @param pool_id 奖励池 ID / Reward pool ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO stake_reward_pool (id, balance) VALUES (%s, 0) "
        "ON DUPLICATE KEY UPDATE balance = balance",
        (pool_id,),
        connection=connection,
    )


async def fetch_stake_reward_pool_balance(pool_id: int, *, connection=None, for_update: bool = False):
    """@brief 读取质押奖励池余额 / Fetch stake reward pool balance.

    @param pool_id 奖励池 ID / Reward pool ID.
    @param connection 可选数据库连接 / Optional database connection.
    @param for_update 是否加行锁 / Whether to lock the row.
    @return 余额值；不存在时返回 None / Balance value, or None.
    """

    lock_clause = " FOR UPDATE" if for_update else ""
    row = await mysql_connection.fetch_one(
        f"SELECT balance FROM stake_reward_pool WHERE id = %s{lock_clause}",
        (pool_id,),
        connection=connection,
    )
    return row[0] if row and row[0] is not None else None


async def add_stake_reward_pool_balance(pool_id: int, amount, *, connection=None) -> None:
    """@brief 增加质押奖励池余额 / Add stake reward pool balance.

    @param pool_id 奖励池 ID / Reward pool ID.
    @param amount 增加金额 / Amount to add.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "UPDATE stake_reward_pool SET balance = balance + %s WHERE id = %s",
        (amount, pool_id),
        connection=connection,
    )


async def subtract_stake_reward_pool_balance(pool_id: int, amount, *, connection=None) -> None:
    """@brief 减少质押奖励池余额 / Subtract stake reward pool balance.

    @param pool_id 奖励池 ID / Reward pool ID.
    @param amount 减少金额 / Amount to subtract.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "UPDATE stake_reward_pool SET balance = balance - %s WHERE id = %s",
        (amount, pool_id),
        connection=connection,
    )


async def sum_user_coin_balances(*, connection=None):
    """@brief 统计用户金币总量 / Sum all user coin balances.

    @param connection 可选数据库连接 / Optional database connection.
    @return 金币总量 / Total coin balance.
    """

    row = await mysql_connection.fetch_one("SELECT SUM(coins + coins_paid) FROM user", connection=connection)
    return row[0] if row and row[0] else 0


async def sum_user_stakes(*, connection=None):
    """@brief 统计质押总量 / Sum total staked coins.

    @param connection 可选数据库连接 / Optional database connection.
    @return 质押总量 / Total staked amount.
    """

    row = await mysql_connection.fetch_one("SELECT SUM(stake_amount) FROM user_stakes", connection=connection)
    return row[0] if row and row[0] else 0


async def fetch_user_stake(user_id: int, *, connection=None):
    """@brief 读取用户质押 / Fetch a user's stake.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(stake_amount, stake_time, last_reward_time)` 行；不存在时返回 None / Stake row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT stake_amount, stake_time, last_reward_time FROM user_stakes WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )


async def insert_user_stake(user_id: int, amount: int, stake_time, *, connection=None) -> None:
    """@brief 创建用户质押 / Insert a user stake.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param amount 质押金额 / Stake amount.
    @param stake_time 质押时间 / Stake time.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO user_stakes (user_id, stake_amount, stake_time) VALUES (%s, %s, %s)",
        (user_id, amount, stake_time),
        connection=connection,
    )


async def set_user_stake_last_reward_time(user_id: int, last_reward_time, *, connection=None) -> None:
    """@brief 更新用户质押领奖时间 / Set user stake last reward time.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param last_reward_time 新领奖时间 / New last reward time.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "UPDATE user_stakes SET last_reward_time = %s WHERE user_id = %s",
        (last_reward_time, user_id),
        connection=connection,
    )


async def delete_user_stake(user_id: int, *, connection=None) -> None:
    """@brief 删除用户质押 / Delete a user stake.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "DELETE FROM user_stakes WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )


async def fetch_redemption_code_for_update(code: str, *, connection):
    """@brief 加锁读取兑换码 / Fetch a redemption code with row lock.

    @param code 兑换码 / Redemption code.
    @param connection 事务连接 / Transaction connection.
    @return 兑换码行；不存在时返回 None / Redemption code row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT id, code, amount, is_used, used_by, used_at FROM redemption_codes "
        "WHERE code = %s FOR UPDATE",
        (code,),
        connection=connection,
    )


async def mark_redemption_code_used(code_id: int, user_id: int, used_at, *, connection=None) -> None:
    """@brief 标记兑换码已使用 / Mark a redemption code as used.

    @param code_id 兑换码行 ID / Redemption code row ID.
    @param user_id 使用者用户 ID / Redeemer user ID.
    @param used_at 使用时间 / Used timestamp.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "UPDATE redemption_codes SET is_used = TRUE, used_by = %s, used_at = %s WHERE id = %s",
        (user_id, used_at, code_id),
        connection=connection,
    )


async def redemption_code_exists(code: str, *, connection=None) -> bool:
    """@brief 判断兑换码是否存在 / Check whether a redemption code exists.

    @param code 兑换码 / Redemption code.
    @param connection 可选数据库连接 / Optional database connection.
    @return 存在返回 True / True when present.
    """

    row = await mysql_connection.fetch_one(
        "SELECT id FROM redemption_codes WHERE code = %s",
        (code,),
        connection=connection,
    )
    return bool(row)


async def insert_redemption_code(code: str, amount: int, *, connection=None) -> None:
    """@brief 创建兑换码 / Insert a redemption code.

    @param code 兑换码 / Redemption code.
    @param amount 金币数量 / Coin amount.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO redemption_codes (code, amount) VALUES (%s, %s)",
        (code, amount),
        connection=connection,
    )


async def fetch_invitation_referrer(invited_user_id: int, *, connection=None):
    """@brief 读取被邀请用户的邀请人 ID / Fetch invited user's referrer ID.

    @param invited_user_id 被邀请用户 ID / Invited user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(referrer_id,)` 行；不存在时返回 None / Referrer row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT referrer_id FROM user_invitations WHERE invited_user_id = %s",
        (invited_user_id,),
        connection=connection,
    )


async def insert_invitation(
    invited_user_id: int,
    referrer_id: int,
    *,
    connection=None,
) -> None:
    """@brief 创建邀请记录 / Insert an invitation record.

    @param invited_user_id 被邀请用户 ID / Invited user ID.
    @param referrer_id 邀请人用户 ID / Referrer user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "INSERT INTO user_invitations "
        "(invited_user_id, referrer_id, invitation_time, reward_claimed) "
        "VALUES (%s, %s, NOW(), TRUE)",
        (invited_user_id, referrer_id),
        connection=connection,
    )


async def count_invited_users(referrer_id: int, *, connection=None) -> int:
    """@brief 统计邀请人数 / Count invited users.

    @param referrer_id 邀请人用户 ID / Referrer user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 邀请人数 / Invitation count.
    """

    row = await mysql_connection.fetch_one(
        "SELECT COUNT(*) FROM user_invitations WHERE referrer_id = %s",
        (referrer_id,),
        connection=connection,
    )
    return int(row[0] or 0) if row else 0


async def fetch_invited_users(referrer_id: int, *, connection=None):
    """@brief 读取邀请列表 / Fetch invited user list.

    @param referrer_id 邀请人用户 ID / Referrer user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(invited_user_id, name, invitation_time)` 行列表 / Invited user rows.
    """

    return await mysql_connection.fetch_all(
        "SELECT i.invited_user_id, u.name, i.invitation_time "
        "FROM user_invitations i "
        "JOIN user u ON i.invited_user_id = u.id "
        "WHERE i.referrer_id = %s "
        "ORDER BY i.invitation_time DESC",
        (referrer_id,),
        connection=connection,
    )


async def fetch_referrer_with_name(invited_user_id: int, *, connection=None):
    """@brief 读取邀请人与用户名 / Fetch referrer with display name.

    @param invited_user_id 被邀请用户 ID / Invited user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(referrer_id, name)` 行；不存在时返回 None / Referrer row, or None.
    """

    return await mysql_connection.fetch_one(
        "SELECT ui.referrer_id, u.name "
        "FROM user_invitations ui "
        "JOIN user u ON ui.referrer_id = u.id "
        "WHERE ui.invited_user_id = %s",
        (invited_user_id,),
        connection=connection,
    )
