import random
from datetime import datetime, timedelta

from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import mysql_connection
from fogmoe_bot.infrastructure.database.repositories import user_repository

USER_PLAN_FREE = "free"
USER_PLAN_PAID = "paid"
USER_PLAN_ADMIN = "admin"


def resolve_user_plan(user_id: int, coins_paid: int) -> str:
    if user_id == config.ADMIN_USER_ID:
        return USER_PLAN_ADMIN
    return USER_PLAN_PAID if coins_paid > 0 else USER_PLAN_FREE

# 添加用户抽奖锁字典，防止同一用户并发抽奖
lottery_locks = {}


async def get_user_last_lottery_date(user_id):
    return await user_repository.fetch_lottery_date(user_id)


async def update_user_lottery_date(user_id):
    await user_repository.upsert_lottery_date(user_id, datetime.now())


async def get_user_account(
    user_id: int,
    *,
    connection=None,
    for_update: bool = False,
) -> user_repository.UserAccount | None:
    """@brief 读取用户账户快照 / Fetch a user account snapshot.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @param for_update 是否加行锁 / Whether to lock the row.
    @return 用户账户快照；不存在时返回 None / User account snapshot, or None when missing.
    """

    return await user_repository.fetch_user_account(
        user_id,
        connection=connection,
        for_update=for_update,
    )


async def register_telegram_user(
    user_id: int,
    user_name: str,
    initial_coins: int,
    *,
    connection=None,
) -> user_repository.UserAccount | None:
    """@brief 注册或刷新 Telegram 用户 / Register or refresh a Telegram user.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param user_name Telegram 用户名 / Telegram username.
    @param initial_coins 新用户初始硬币 / Initial coins for new users.
    @param connection 可选数据库连接 / Optional database connection.
    @return 用户账户快照；不存在时返回 None / User account snapshot, or None when missing.
    """

    await user_repository.upsert_telegram_user(
        user_id,
        user_name,
        initial_coins,
        connection=connection,
    )
    account = await get_user_account(user_id, connection=connection)
    if not account:
        return None

    resolved_plan = resolve_user_plan(user_id, account.coins_paid)
    if account.user_plan != resolved_plan:
        await user_repository.set_user_plan(
            user_id,
            resolved_plan,
            connection=connection,
        )
        account = await get_user_account(user_id, connection=connection)
    return account


async def get_user_coin_balances(user_id, *, connection=None) -> tuple[int, int]:
    account = await get_user_account(user_id, connection=connection)
    if not account:
        return 0, 0
    return account.coins, account.coins_paid


async def get_user_total_coins(user_id, *, connection=None) -> int:
    coins_free, coins_paid = await get_user_coin_balances(
        user_id,
        connection=connection,
    )
    return coins_free + coins_paid


async def add_free_coins(user_id, coins, *, connection=None) -> int:
    coins = int(coins)
    if coins <= 0:
        return 0
    await user_repository.add_free_coins(user_id, coins, connection=connection)
    return coins


async def add_paid_coins(user_id, coins, *, connection=None) -> int:
    coins = int(coins)
    if coins <= 0:
        return 0
    plan = resolve_user_plan(user_id, coins_paid=1)
    await user_repository.add_paid_coins_and_plan(
        user_id,
        coins,
        plan,
        connection=connection,
    )
    return coins


async def spend_user_coins(user_id, amount, *, connection=None) -> bool:
    amount = int(amount)
    if amount <= 0:
        return True
    if connection is None:
        async with mysql_connection.transaction() as connection:
            return await spend_user_coins(user_id, amount, connection=connection)

    account = await get_user_account(
        user_id,
        connection=connection,
        for_update=True,
    )
    if not account:
        return False
    coins_free = account.coins
    coins_paid = account.coins_paid
    total = coins_free + coins_paid
    if total < amount:
        return False

    if coins_free >= amount:
        new_free = coins_free - amount
        new_paid = coins_paid
    else:
        remaining = amount - coins_free
        new_free = 0
        new_paid = coins_paid - remaining
    plan = resolve_user_plan(user_id, new_paid)
    await user_repository.set_coin_balances_and_plan(
        user_id,
        new_free,
        new_paid,
        plan,
        connection=connection,
    )
    return True


async def update_user_coins(user_id, coins, *, connection=None):
    coins = int(coins)
    if coins >= 0:
        return await add_free_coins(user_id, coins, connection=connection)
    return await spend_user_coins(user_id, -coins, connection=connection)


async def user_exists(user_id):
    return await user_repository.user_exists(user_id)


def user_exists_sync(user_id):
    return mysql_connection.run_sync(user_exists(user_id))


async def async_user_exists(user_id):
    return await user_exists(user_id)


async def lottery(user_id):
    if not await user_exists(user_id):
        return (
            "请先使用 /me 命令获取个人信息。\n"
            "Please register first using the /me command."
        )

    last_lottery_date = await get_user_last_lottery_date(user_id)
    if last_lottery_date and datetime.now() - last_lottery_date < timedelta(hours=24):
        return (
            "每24小时您只能参加一次抽奖喵。下次再来吧！\n"
            "You can only participate in the lottery once every 24 hours. Meow! Come back later!"
        )

    probabilities = [0.4, 0.1, 0.5]
    coins_distribution = [
        random.choices(range(1, 5), k=1)[0],
        random.choices(range(11, 21), k=1)[0],
        random.choices(range(5, 11), k=1)[0],
    ]
    coins = random.choices(coins_distribution, probabilities)[0]

    await update_user_coins(user_id, coins)
    await update_user_lottery_date(user_id)

    return (
        f"恭喜！您赢得了 {coins} 枚硬币喵。\n"
        f"Congratulations! You have won {coins} coins. Meow!"
    )


async def async_lottery(user_id):
    if user_id in lottery_locks:
        return (
            "抽奖操作过于频繁，请等待上一次操作完成。\n"
            "You're drawing too fast, please wait for the previous lottery to complete."
        )

    try:
        lottery_locks[user_id] = True
        return await lottery(user_id)
    finally:
        lottery_locks.pop(user_id, None)


async def get_user_personal_info(user_id: int) -> str:
    account = await get_user_account(user_id)
    if not account or account.info == "":
        return ""
    return account.info


def get_user_personal_info_sync(user_id: int) -> str:
    return mysql_connection.run_sync(get_user_personal_info(user_id))


async def async_get_user_personal_info(user_id: int) -> str:
    return await get_user_personal_info(user_id)


async def get_user_coins(user_id: int) -> int:
    return await get_user_total_coins(user_id)


def get_user_coins_sync(user_id: int) -> int:
    return mysql_connection.run_sync(get_user_coins(user_id))


async def async_get_user_coins(user_id: int) -> int:
    return await get_user_coins(user_id)


async def get_user_affection(user_id: int) -> int:
    affection = await user_repository.fetch_affection(user_id)
    return affection if affection is not None else 0


def get_user_affection_sync(user_id: int) -> int:
    return mysql_connection.run_sync(get_user_affection(user_id))


async def update_user_affection(user_id: int, delta: int) -> int:
    delta = int(delta)
    if delta > 10:
        delta = 10
    elif delta < -10:
        delta = -10

    async with mysql_connection.transaction() as connection:
        current = await user_repository.fetch_affection(
            user_id,
            connection=connection,
            for_update=True,
        )
        if current is None:
            current = 0
        updated = max(-100, min(100, current + delta))
        await user_repository.upsert_affection(
            user_id,
            updated,
            connection=connection,
        )

    return updated


def update_user_affection_sync(user_id: int, delta: int) -> int:
    return mysql_connection.run_sync(update_user_affection(user_id, delta))


async def async_get_user_affection(user_id: int) -> int:
    return await get_user_affection(user_id)


async def async_update_user_affection(user_id: int, delta: int) -> int:
    return await update_user_affection(user_id, delta)


async def get_user_permission(user_id: int) -> int:
    account = await get_user_account(user_id)
    return account.permission if account else 0


def get_user_permission_sync(user_id: int) -> int:
    return mysql_connection.run_sync(get_user_permission(user_id))


async def async_get_user_permission(user_id: int) -> int:
    return await get_user_permission(user_id)


async def async_update_user_coins(user_id: int, amount: int) -> None:
    await update_user_coins(user_id, amount)


async def get_user_impression(user_id: int) -> str:
    return await user_repository.fetch_impression(user_id)


def get_user_impression_sync(user_id: int) -> str:
    return mysql_connection.run_sync(get_user_impression(user_id))


async def update_user_impression(user_id: int, impression: str) -> str:
    text = (impression or "").strip()
    async with mysql_connection.transaction() as connection:
        await user_repository.upsert_impression(
            user_id,
            text,
            connection=connection,
        )
    return text


async def update_user_personal_info(user_id: int, info: str, *, connection=None) -> None:
    """@brief 更新用户个人信息 / Update user personal information.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param info 个人信息文本 / Personal information text.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await user_repository.set_info(user_id, info, connection=connection)


def update_user_impression_sync(user_id: int, impression: str) -> str:
    return mysql_connection.run_sync(update_user_impression(user_id, impression))


async def async_get_user_impression(user_id: int) -> str:
    return await get_user_impression(user_id)


async def async_update_user_impression(user_id: int, impression: str) -> str:
    return await update_user_impression(user_id, impression)


async def set_user_permission(user_id: int, permission: int, *, connection=None) -> None:
    """@brief 设置用户权限等级 / Set a user's permission level.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param permission 新权限等级 / New permission level.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await user_repository.set_permission(
        user_id,
        int(permission),
        connection=connection,
    )


async def increase_user_permanent_records_limit(
    user_id: int,
    delta: int = 1,
    *,
    connection=None,
) -> int:
    """@brief 增加用户永久记忆上限 / Increase a user's permanent memory limit.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param delta 增加量 / Increment amount.
    @param connection 可选数据库连接 / Optional database connection.
    @return 更新后的永久记忆上限 / Updated permanent memory limit.
    """

    return await user_repository.increment_permanent_records_limit(
        user_id,
        int(delta),
        connection=connection,
    )
