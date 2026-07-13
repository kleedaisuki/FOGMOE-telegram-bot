"""@brief PostgreSQL 商店与奖池入账适配器 / PostgreSQL shop and reward-pool funding adapter."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.shop import (
    PoolFundingCommand,
    ShopItem,
    ShopOperations,
    ShopPurchaseCommand,
    ShopPurchaseResult,
)
from fogmoe_bot.domain.economy import AccountBalance
from fogmoe_bot.infrastructure.database import connection as db_connection

from .common import _load_result, _plan_after_spend, _save_result


class PostgresShopOperations(ShopOperations):
    """@brief 执行商店购买与奖励池幂等入账 / Execute shop purchases and idempotent reward-pool funding."""

    def __init__(self, *, admin_user_id: int) -> None:
        """@brief 注入管理员身份 / Inject administrator identity.

        @param admin_user_id 管理员用户 ID / Administrator user ID.
        """

        self._admin_user_id = admin_user_id

    async def fund_pool(self, command: PoolFundingCommand) -> None:
        """@brief 仅追加 credit posting，不锁奖励支出 gate / Append only a credit posting without locking the reward-debit gate.

        @param command 奖励池入账命令 / Reward-pool funding command.
        @return None / None.
        """

        if command.amount <= 0:
            raise ValueError("Pool credit must be positive")
        async with db_connection.transaction() as connection:
            await db_connection.execute(
                "INSERT INTO economy.stake_reward_pool (id, balance) VALUES (%s, 0) "
                "ON CONFLICT (id) DO NOTHING",
                (command.pool_id,),
                connection=connection,
            )
            inserted = await db_connection.execute(
                "INSERT INTO economy.stake_pool_postings "
                "(pool_id, idempotency_key, delta) VALUES (%s, %s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (command.pool_id, command.idempotency_key, command.amount),
                connection=connection,
            )
            if inserted == 1:
                return
            row = await db_connection.fetch_one(
                "SELECT pool_id, delta FROM economy.stake_pool_postings "
                "WHERE idempotency_key = %s",
                (command.idempotency_key,),
                connection=connection,
            )
            if (
                row is None
                or cast(int, row[0]) != command.pool_id
                or Decimal(str(row[1])) != command.amount
            ):
                raise ValueError("Pool funding idempotency key changed meaning")

    async def purchase(self, command: ShopPurchaseCommand) -> ShopPurchaseResult:
        """@brief 以账户行串行化购买与持久化保底 / Serialize purchase and durable pity on the account row.

        @param command 购买命令 / Purchase command.
        @return 购买结果 / Purchase result.
        """

        async with db_connection.transaction() as connection:
            row = await db_connection.fetch_one(
                "SELECT id, coins, coins_paid, user_plan, permission "
                "FROM identity.users "
                "WHERE id = %s FOR UPDATE",
                (command.user_id,),
                connection=connection,
            )
            if row is None:
                return ShopPurchaseResult(EconomyCode.NOT_REGISTERED)
            replay = await _load_result(command.idempotency_key, connection)
            if replay is not None:
                return _purchase_from_mapping(replay)
            account = AccountBalance(
                cast(int, row[0]),
                cast(int, row[1]),
                cast(int, row[2]),
                cast(str, row[3]),
            )
            permission = cast(int, row[4])
            price = _shop_price(command.item)
            rejection = _purchase_prerequisite(command.item, permission)
            if rejection is not None:
                result = ShopPurchaseResult(
                    rejection,
                    available=account.total,
                    permission=permission,
                )
            else:
                charged = account.spend(price)
                if charged is None:
                    result = ShopPurchaseResult(
                        EconomyCode.INSUFFICIENT_COINS,
                        available=account.total,
                        permission=permission,
                    )
                else:
                    reward = command.drawn_reward
                    bonus = 0
                    if command.item in {ShopItem.SCRATCH, ShopItem.HUANLE}:
                        misses = await _next_pity_count(command, connection)
                        if misses >= 5:
                            bonus = 10 if command.item is ShopItem.SCRATCH else 2
                            await _reset_pity(command, connection)
                    permission = _new_permission(command.item, permission)
                    await db_connection.execute(
                        "UPDATE identity.users SET coins = %s, coins_paid = %s, "
                        "user_plan = %s, permission = %s "
                        "WHERE id = %s",
                        (
                            charged.free + reward + bonus,
                            charged.paid,
                            _plan_after_spend(
                                command.user_id,
                                charged.paid,
                                self._admin_user_id,
                            ),
                            permission,
                            command.user_id,
                        ),
                        connection=connection,
                    )
                    result = ShopPurchaseResult(
                        EconomyCode.SUCCESS,
                        available=account.total,
                        reward=reward,
                        bonus=bonus,
                        permission=permission,
                    )
            await _save_result(
                command.idempotency_key,
                "shop_purchase",
                command.user_id,
                _purchase_mapping(result),
                connection,
            )
            return result


def _shop_price(item: ShopItem) -> int:
    """@brief 返回商品旧价格 / Return an item's legacy price.

    @param item 商品 / Shop item.
    @return 旧价格 / Legacy price.
    """

    return {
        ShopItem.PERMISSION_1: 50,
        ShopItem.PERMISSION_2: 100,
        ShopItem.PERMISSION_3: 10_000,
        ShopItem.SCRATCH: 10,
        ShopItem.HUANLE: 1,
    }[item]


def _purchase_prerequisite(item: ShopItem, permission: int) -> EconomyCode | None:
    """@brief 校验权限商品前置条件 / Validate permission-item prerequisites.

    @param item 商品 / Shop item.
    @param permission 当前权限等级 / Current permission level.
    @return 拒绝码；可购买为 None / Rejection code, or None when allowed.
    """

    if item is ShopItem.PERMISSION_1 and permission != 0:
        return EconomyCode.ALREADY_OWNED
    if item is ShopItem.PERMISSION_2:
        if permission == 0:
            return EconomyCode.PERMISSION_PREREQUISITE
        if permission >= 2:
            return EconomyCode.ALREADY_OWNED
    if item is ShopItem.PERMISSION_3:
        if permission < 2:
            return EconomyCode.PERMISSION_PREREQUISITE
        if permission >= 3:
            return EconomyCode.ALREADY_OWNED
    return None


def _new_permission(item: ShopItem, current: int) -> int:
    """@brief 计算购买后权限 / Calculate post-purchase permission.

    @param item 商品 / Shop item.
    @param current 当前权限等级 / Current permission level.
    @return 新权限等级 / New permission level.
    """

    return {
        ShopItem.PERMISSION_1: 1,
        ShopItem.PERMISSION_2: 2,
        ShopItem.PERMISSION_3: 3,
    }.get(item, current)


async def _next_pity_count(
    command: ShopPurchaseCommand,
    connection: AsyncConnection,
) -> int:
    """@brief 原子更新持久化保底计数 / Atomically update a durable pity count.

    @param command 购买命令 / Purchase command.
    @param connection 当前事务 / Current transaction.
    @return 更新后的连续未中奖次数 / Updated consecutive miss count.
    """

    missed = (
        command.drawn_reward < 10
        if command.item is ShopItem.SCRATCH
        else command.drawn_reward == 0
    )
    await db_connection.execute(
        "INSERT INTO economy.shop_pity (user_id, game, business_date, misses) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (user_id, game) DO UPDATE SET "
        "business_date = EXCLUDED.business_date, misses = CASE "
        "WHEN economy.shop_pity.business_date <> EXCLUDED.business_date "
        "THEN EXCLUDED.misses WHEN EXCLUDED.misses = 0 THEN 0 "
        "ELSE economy.shop_pity.misses + 1 END",
        (command.user_id, command.item.value, command.day, int(missed)),
        connection=connection,
    )
    row = await db_connection.fetch_one(
        "SELECT misses FROM economy.shop_pity WHERE user_id = %s AND game = %s",
        (command.user_id, command.item.value),
        connection=connection,
    )
    return cast(int, row[0]) if row is not None else 0


async def _reset_pity(
    command: ShopPurchaseCommand,
    connection: AsyncConnection,
) -> None:
    """@brief 在发放保底后归零 / Reset pity after granting the bonus.

    @param command 购买命令 / Purchase command.
    @param connection 当前事务 / Current transaction.
    @return None / None.
    """

    await db_connection.execute(
        "UPDATE economy.shop_pity SET misses = 0 WHERE user_id = %s AND game = %s",
        (command.user_id, command.item.value),
        connection=connection,
    )


def _purchase_mapping(result: ShopPurchaseResult) -> dict[str, object]:
    """@brief 序列化购买结果 / Serialize a purchase result.

    @param result 购买结果 / Purchase result.
    @return JSON mapping / JSON mapping.
    """

    return {
        "code": result.code.value,
        "available": result.available,
        "reward": result.reward,
        "bonus": result.bonus,
        "permission": result.permission,
    }


def _purchase_from_mapping(value: Mapping[str, Any]) -> ShopPurchaseResult:
    """@brief 解析购买回执 / Parse a purchase receipt.

    @param value 回执映射 / Receipt mapping.
    @return 购买结果 / Purchase result.
    """

    return ShopPurchaseResult(
        EconomyCode(str(value["code"])),
        available=int(value.get("available", 0)),
        reward=int(value.get("reward", 0)),
        bonus=int(value.get("bonus", 0)),
        permission=int(value.get("permission", 0)),
    )
