from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure.database import connection as db_connection


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


def _coerce_user_account(row: Sequence[object] | None) -> UserAccount | None:
    """@brief 将数据库行转换为账户快照 / Convert a database row into an account snapshot.

    @param row 数据库结果行 / Database result row.
    @return 用户账户快照；无行时返回 None / User account snapshot, or None when no row exists.
    """

    if not row:
        return None
    return UserAccount(
        user_id=int(str(row[0])),
        permission=int(str(row[1] or 0)),
        coins=int(str(row[2] or 0)),
        coins_paid=int(str(row[3] or 0)),
        permanent_records_limit=None if row[4] is None else int(str(row[4])),
        info="" if row[5] is None else str(row[5]),
        name="" if row[6] is None else str(row[6]),
        user_plan=str(row[7] or "free"),
        recharge_blocked_until=cast(datetime | None, row[8]),
    )


async def fetch_user_account(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
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


async def add_free_coins(
    user_id: int,
    coins: int,
    *,
    connection: AsyncConnection | None = None,
) -> None:
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


async def set_coin_balances_and_plan(
    user_id: int,
    coins: int,
    coins_paid: int,
    user_plan: str,
    *,
    connection: AsyncConnection | None = None,
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


async def fetch_impression(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
) -> str:
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


async def upsert_impression(
    user_id: int,
    impression: str,
    *,
    connection: AsyncConnection | None = None,
) -> None:
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
