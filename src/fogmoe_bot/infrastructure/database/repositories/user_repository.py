from dataclasses import dataclass
from datetime import date, datetime

from fogmoe_bot.infrastructure.database import connection as db_connection

DEFAULT_PERMANENT_RECORDS_LIMIT = 100


@dataclass(frozen=True)
class UserAccount:
    """@brief 用户账户快照 / User account snapshot.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param permission 用户权限等级 / User permission level.
    @param coins 免费硬币余额 / Free coin balance.
    @param coins_paid 付费硬币余额 / Paid coin balance.
    @param permanent_records_limit 永久记忆上限 / Permanent memory record limit.
    @param info 用户个人信息 / User personal information.
    @param name 用户名 / User name.
    @param user_plan 用户套餐 / User plan.
    @param recharge_blocked_until 充值封禁截止时间 / Recharge block deadline.
    @note 这是读取时刻的不可变快照，不代表事务外的最新状态 / This is an immutable read snapshot, not a live value outside the transaction.
    """

    user_id: int
    permission: int
    coins: int
    coins_paid: int
    permanent_records_limit: int | None
    info: str
    name: str = ""
    user_plan: str = "free"
    recharge_blocked_until: datetime | None = None

    @property
    def total_coins(self) -> int:
        """@brief 计算总硬币余额 / Calculate total coin balance.

        @return 免费硬币与付费硬币之和 / Sum of free and paid coins.
        """

        return self.coins + self.coins_paid


def _coerce_user_account(row) -> UserAccount | None:
    """@brief 将数据库行转换为账户快照 / Convert a database row into an account snapshot.

    @param row 数据库结果行 / Database result row.
    @return 用户账户快照；无行时返回 None / User account snapshot, or None when no row exists.
    """

    if not row:
        return None
    return UserAccount(
        user_id=int(row[0]),
        permission=int(row[1] or 0),
        coins=int(row[2] or 0),
        coins_paid=int(row[3] or 0),
        permanent_records_limit=None if row[4] is None else int(row[4]),
        info="" if row[5] is None else str(row[5]),
        name="" if row[6] is None else str(row[6]),
        user_plan=str(row[7] or "free"),
        recharge_blocked_until=row[8],
    )


async def fetch_user_account(
    user_id: int,
    *,
    connection=None,
    for_update: bool = False,
) -> UserAccount | None:
    """@brief 读取用户账户 / Fetch a user account.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @param for_update 是否加行锁 / Whether to lock the row with FOR UPDATE.
    @return 用户账户；不存在时返回 None / User account, or None when it does not exist.
    @note for_update=True 时调用方应处于事务中 / Callers should be inside a transaction when for_update=True.
    """

    lock_clause = " FOR UPDATE" if for_update else ""
    row = await db_connection.fetch_one(
        "SELECT id, permission, coins, coins_paid, permanent_records_limit, info, "
        "name, user_plan, recharge_blocked_until "
        f"FROM users WHERE id = %s{lock_clause}",
        (user_id,),
        connection=connection,
    )
    return _coerce_user_account(row)


async def list_user_ids(*, connection=None) -> list[int]:
    """@brief 列出所有用户 ID / List all user IDs.

    @param connection 可选数据库连接 / Optional database connection.
    @return 用户 ID 列表 / List of user IDs.
    """

    rows = await db_connection.fetch_all("SELECT id FROM users", connection=connection)
    return [int(row[0]) for row in rows]


async def upsert_telegram_user(
    user_id: int,
    name: str,
    initial_coins: int,
    *,
    connection=None,
) -> None:
    """@brief 注册或更新 Telegram 用户 / Register or update a Telegram user.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param name Telegram 用户名 / Telegram username.
    @param initial_coins 新用户初始硬币 / Initial coins for new users.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO users (id, name, coins) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name",
        (user_id, name, initial_coins),
        connection=connection,
    )


async def set_user_plan(user_id: int, user_plan: str, *, connection=None) -> None:
    """@brief 设置用户套餐 / Set user plan.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param user_plan 用户套餐 / User plan.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE users SET user_plan = %s WHERE id = %s",
        (user_plan, user_id),
        connection=connection,
    )


async def set_info(user_id: int, info: str, *, connection=None) -> None:
    """@brief 设置用户个人信息 / Set user personal information.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param info 个人信息文本 / Personal information text.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE users SET info = %s WHERE id = %s",
        (info, user_id),
        connection=connection,
    )


async def fetch_top_coin_users(limit: int = 5, *, connection=None):
    """@brief 读取金币排行榜 / Fetch top coin users.

    @param limit 最大返回数量 / Maximum rows to return.
    @param connection 可选数据库连接 / Optional database connection.
    @return `(name, coins_total)` 行列表 / Rows of `(name, coins_total)`.
    """

    return await db_connection.fetch_all(
        "SELECT name, (coins + coins_paid) AS coins_total "
        "FROM users ORDER BY coins_total DESC LIMIT %s",
        (int(limit),),
        connection=connection,
    )


async def find_user_id_by_name(name: str, *, connection=None) -> int | None:
    """@brief 按用户名查找用户 ID / Find user ID by username.

    @param name 用户名 / Username.
    @param connection 可选数据库连接 / Optional database connection.
    @return 用户 ID；不存在时返回 None / User ID, or None when absent.
    """

    row = await db_connection.fetch_one(
        "SELECT id FROM users WHERE name = %s",
        (name,),
        connection=connection,
    )
    return int(row[0]) if row else None


async def fetch_display_name(user_id: int, *, connection=None) -> str | None:
    """@brief 读取用户名 / Fetch display name.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 用户名；不存在时返回 None / Display name, or None when absent.
    """

    row = await db_connection.fetch_one(
        "SELECT name FROM users WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return str(row[0]) if row else None


async def fetch_recharge_blocked_until(user_id: int, *, connection=None) -> datetime | None:
    """@brief 读取充值封禁截止时间 / Fetch recharge block deadline.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 截止时间；不存在时返回 None / Deadline, or None when absent.
    """

    row = await db_connection.fetch_one(
        "SELECT recharge_blocked_until FROM users WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return row[0] if row and row[0] else None


async def set_recharge_blocked_until(
    user_id: int,
    blocked_until: datetime,
    *,
    connection=None,
) -> None:
    """@brief 设置充值封禁截止时间 / Set recharge block deadline.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param blocked_until 截止时间 / Block deadline.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE users SET recharge_blocked_until = %s WHERE id = %s",
        (blocked_until, user_id),
        connection=connection,
    )


async def fetch_daily_give_count_for_update(
    user_id: int,
    give_date: date,
    *,
    connection,
) -> int:
    """@brief 加锁读取每日赠送次数 / Fetch daily give count with row lock.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param give_date 日期 / Give date.
    @param connection 数据库事务连接 / Database transaction connection.
    @return 当日赠送次数 / Give count for the date.
    """

    row = await db_connection.fetch_one(
        "SELECT give_count FROM user_give_daily WHERE user_id = %s AND give_date = %s FOR UPDATE",
        (user_id, give_date),
        connection=connection,
    )
    return int(row[0] or 0) if row else 0


async def increment_daily_give_count(
    user_id: int,
    give_date: date,
    *,
    connection,
) -> None:
    """@brief 增加每日赠送次数 / Increment daily give count.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param give_date 日期 / Give date.
    @param connection 数据库事务连接 / Database transaction connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO user_give_daily (user_id, give_date, give_count) VALUES (%s, %s, 1) "
        "ON CONFLICT (user_id, give_date) DO UPDATE SET give_count = user_give_daily.give_count + 1",
        (user_id, give_date),
        connection=connection,
    )


async def user_exists(user_id: int, *, connection=None) -> bool:
    """@brief 判断用户是否存在 / Check whether a user exists.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 存在返回 True，否则返回 False / True when the user exists, otherwise False.
    """

    row = await db_connection.fetch_one(
        "SELECT id FROM users WHERE id = %s",
        (user_id,),
        connection=connection,
    )
    return row is not None


async def fetch_lottery_date(user_id: int, *, connection=None):
    """@brief 读取上次抽奖时间 / Fetch the last lottery timestamp.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 上次抽奖时间；不存在时返回 None / Last lottery timestamp, or None when missing.
    """

    row = await db_connection.fetch_one(
        "SELECT last_lottery_date FROM user_lottery WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    return row[0] if row else None


async def upsert_lottery_date(user_id: int, lottery_date: datetime, *, connection=None) -> None:
    """@brief 写入用户抽奖时间 / Upsert a user lottery timestamp.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param lottery_date 抽奖时间 / Lottery timestamp.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO user_lottery (user_id, last_lottery_date) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET last_lottery_date = EXCLUDED.last_lottery_date",
        (user_id, lottery_date),
        connection=connection,
    )


async def add_free_coins(user_id: int, coins: int, *, connection=None) -> None:
    """@brief 增加免费硬币 / Add free coins.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param coins 增加数量 / Amount to add.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE users SET coins = coins + %s WHERE id = %s",
        (coins, user_id),
        connection=connection,
    )


async def add_paid_coins_and_plan(
    user_id: int,
    coins: int,
    user_plan: str,
    *,
    connection=None,
) -> None:
    """@brief 增加付费硬币并更新用户计划 / Add paid coins and update the user plan.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param coins 增加数量 / Amount to add.
    @param user_plan 用户计划值 / User plan value.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE users SET coins_paid = coins_paid + %s, user_plan = %s WHERE id = %s",
        (coins, user_plan, user_id),
        connection=connection,
    )


async def set_coin_balances_and_plan(
    user_id: int,
    coins: int,
    coins_paid: int,
    user_plan: str,
    *,
    connection=None,
) -> None:
    """@brief 设置硬币余额和用户计划 / Set coin balances and user plan.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param coins 免费硬币余额 / Free coin balance.
    @param coins_paid 付费硬币余额 / Paid coin balance.
    @param user_plan 用户计划值 / User plan value.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE users SET coins = %s, coins_paid = %s, user_plan = %s WHERE id = %s",
        (coins, coins_paid, user_plan, user_id),
        connection=connection,
    )


async def set_permission(user_id: int, permission: int, *, connection=None) -> None:
    """@brief 设置用户权限等级 / Set a user's permission level.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param permission 新权限等级 / New permission level.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE users SET permission = %s WHERE id = %s",
        (permission, user_id),
        connection=connection,
    )


async def increment_permanent_records_limit(
    user_id: int,
    delta: int,
    *,
    connection=None,
) -> int:
    """@brief 增加永久记忆上限 / Increment permanent memory record limit.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param delta 增加量 / Increment amount.
    @param connection 可选数据库连接 / Optional database connection.
    @return 更新后的永久记忆上限 / Updated permanent memory record limit.
    """

    await db_connection.execute(
        "UPDATE users SET permanent_records_limit = COALESCE(permanent_records_limit, 100) + %s "
        "WHERE id = %s",
        (delta, user_id),
        connection=connection,
    )
    account = await fetch_user_account(user_id, connection=connection)
    if account and account.permanent_records_limit is not None:
        return account.permanent_records_limit
    return 100 + delta


async def fetch_affection(user_id: int, *, connection=None, for_update: bool = False) -> int | None:
    """@brief 读取好感度 / Fetch affection.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @param for_update 是否加行锁 / Whether to lock the row with FOR UPDATE.
    @return 好感度；不存在时返回 None / Affection value, or None when missing.
    """

    lock_clause = " FOR UPDATE" if for_update else ""
    row = await db_connection.fetch_one(
        f"SELECT affection FROM ai_user_affection WHERE user_id = %s{lock_clause}",
        (user_id,),
        connection=connection,
    )
    return int(row[0]) if row and row[0] is not None else None


async def fetch_impression(user_id: int, *, connection=None) -> str:
    """@brief 读取用户印象 / Fetch user impression.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 用户印象文本；不存在时返回空字符串 / Impression text, or an empty string when missing.
    """

    row = await db_connection.fetch_one(
        "SELECT impression FROM ai_user_affection WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    if row and row[0] is not None:
        return str(row[0])
    return ""


async def upsert_affection(user_id: int, affection: int, *, connection=None) -> None:
    """@brief 写入好感度 / Upsert affection.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param affection 好感度值 / Affection value.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO ai_user_affection (user_id, affection) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET affection = EXCLUDED.affection, updated_at = CURRENT_TIMESTAMP",
        (user_id, affection),
        connection=connection,
    )


async def upsert_impression(user_id: int, impression: str, *, connection=None) -> None:
    """@brief 写入用户印象 / Upsert user impression.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param impression 用户印象文本 / User impression text.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await db_connection.execute(
        "INSERT INTO ai_user_affection (user_id, affection, impression) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET impression = EXCLUDED.impression, updated_at = CURRENT_TIMESTAMP",
        (user_id, 0, impression),
        connection=connection,
    )
