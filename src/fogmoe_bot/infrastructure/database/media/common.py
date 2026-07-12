"""图片账户事务共享的金币与时间原语 / Coin and time primitives shared by picture-account transactions."""

from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import user_repository


_POOL_ID = 1
_POOL_RATE = Decimal("0.2")


def utc(value: datetime) -> datetime:
    """规范化 aware UTC 时间 / Normalize an aware UTC instant."""

    if value.tzinfo is None:
        raise ValueError("media repository requires aware datetimes")
    return value.astimezone(UTC)


async def spend(
    account: user_repository.UserAccount,
    *,
    cost: int,
    connection: AsyncConnection,
) -> None:
    """在已锁账户上优先扣免费金币 / Spend free coins first on an already locked account."""

    if cost <= 0 or account.total_coins < cost:
        raise ValueError("account cannot cover cost")
    free = max(account.coins - cost, 0)
    paid = account.coins_paid - max(cost - account.coins, 0)
    if account.user_id == config.ADMIN_USER_ID:
        plan = "admin"
    else:
        plan = "paid" if paid > 0 else "free"
    await user_repository.set_coin_balances_and_plan(
        account.user_id,
        free,
        paid,
        plan,
        connection=connection,
    )


async def credit_reward_pool(
    cost: int,
    *,
    idempotency_key: str,
    connection: AsyncConnection,
) -> None:
    """在同一事务结转既有 20% 奖励池 / Accrue the established 20% reward-pool share in the same transaction."""

    amount = (Decimal(cost) * _POOL_RATE).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN,
    )
    if amount <= 0:
        return
    await db_connection.execute(
        "INSERT INTO economy.stake_pool_postings (pool_id, idempotency_key, delta) "
        "VALUES (%s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING",
        (_POOL_ID, idempotency_key, amount),
        connection=connection,
    )
