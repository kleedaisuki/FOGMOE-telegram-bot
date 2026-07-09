from decimal import Decimal, ROUND_DOWN

from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import economy_repository

POOL_ROW_ID = 1
POOL_RATE = Decimal("0.2")
POOL_QUANT = Decimal("0.01")


def _normalize_amount(amount) -> Decimal:
    if amount is None:
        return Decimal("0")
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if value <= 0:
        return Decimal("0")
    return value.quantize(POOL_QUANT, rounding=ROUND_DOWN)


def calculate_pool_add(cost: int) -> Decimal:
    if cost <= 0:
        return Decimal("0")
    return _normalize_amount(Decimal(cost) * POOL_RATE)


async def _ensure_pool_row(*, connection=None) -> None:
    await economy_repository.ensure_stake_reward_pool(
        POOL_ROW_ID,
        connection=connection,
    )


async def get_pool_balance(*, connection=None, for_update: bool = False) -> Decimal:
    await _ensure_pool_row(connection=connection)
    balance = await economy_repository.fetch_stake_reward_pool_balance(
        POOL_ROW_ID,
        connection=connection,
        for_update=for_update,
    )
    if balance is None:
        return Decimal("0")
    return Decimal(str(balance))


async def add_to_pool(amount, *, connection=None) -> Decimal:
    amount = _normalize_amount(amount)
    if amount <= 0:
        return Decimal("0")
    if connection is None:
        async with db_connection.transaction() as connection:
            return await add_to_pool(amount, connection=connection)
    await _ensure_pool_row(connection=connection)
    await economy_repository.add_stake_reward_pool_balance(
        POOL_ROW_ID,
        amount,
        connection=connection,
    )
    return amount


async def subtract_from_pool(amount, *, connection=None) -> Decimal:
    amount = _normalize_amount(amount)
    if amount <= 0:
        return Decimal("0")
    if connection is None:
        async with db_connection.transaction() as connection:
            return await subtract_from_pool(amount, connection=connection)
    await _ensure_pool_row(connection=connection)
    await economy_repository.subtract_stake_reward_pool_balance(
        POOL_ROW_ID,
        amount,
        connection=connection,
    )
    return amount
