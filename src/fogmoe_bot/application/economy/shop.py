"""@brief 商店与奖励池应用模型及端口 / Shop and reward-pool models and port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
import random
from typing import Protocol

from .common import EconomyCode


@dataclass(frozen=True, slots=True)
class PoolFundingCommand:
    """@brief 奖励池 credit posting 命令 / Reward-pool credit-posting command.

    @param amount 正数入账额 / Positive credit amount.
    @param idempotency_key 业务幂等键 / Business idempotency key.
    @param pool_id 奖励池 ID / Reward-pool ID.
    """

    amount: Decimal
    idempotency_key: str
    pool_id: int = 1


class ShopItem(StrEnum):
    """@brief 商店可购买项目 / Purchasable shop item."""

    MEMORY_LIMIT = "memory_limit"
    PERMISSION_1 = "permission_1"
    PERMISSION_2 = "permission_2"
    PERMISSION_3 = "permission_3"
    SCRATCH = "scratch"
    HUANLE = "huanle"


@dataclass(frozen=True, slots=True)
class ShopPurchaseCommand:
    """@brief 商店购买命令 / Shop-purchase command.

    @param user_id 用户 ID / User ID.
    @param item 商品 / Item.
    @param day 业务日期 / Business date.
    @param drawn_reward 事务外抽取的原始奖励 / Raw reward drawn outside the transaction.
    @param idempotency_key 幂等键 / Idempotency key.
    """

    user_id: int
    item: ShopItem
    day: date
    drawn_reward: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ShopPurchaseResult:
    """@brief 商店购买结果 / Shop-purchase result.

    @param code 结果代码 / Result code.
    @param available 操作前余额 / Pre-operation balance.
    @param reward 抽奖奖励 / Lottery reward.
    @param bonus 保底奖励 / Pity bonus.
    @param permission 新权限级别 / New permission level.
    @param memory_limit 新永久记忆上限 / New permanent-memory limit.
    """

    code: EconomyCode
    available: int = 0
    reward: int = 0
    bonus: int = 0
    permission: int = 0
    memory_limit: int = 0


class ShopOperations(Protocol):
    """@brief 商店与奖励池持久化能力端口 / Shop and reward-pool persistence capability port."""

    async def fund_pool(self, command: PoolFundingCommand) -> None:
        """@brief 不锁支出 gate 追加奖励池 credit / Append a pool credit without locking the debit gate.

        @param command 奖励池入账命令 / Reward-pool funding command.
        @return None / None.
        """

        ...

    async def purchase(self, command: ShopPurchaseCommand) -> ShopPurchaseResult:
        """@brief 原子购买商品 / Atomically purchase an item.

        @param command 购买命令 / Purchase command.
        @return 购买结果 / Purchase result.
        """

        ...


def draw_shop_reward(item: ShopItem) -> int:
    """@brief 在事务外抽取商店奖励 / Draw a shop reward outside the transaction.

    @param item 商品 / Item.
    @return 原始奖励 / Raw reward.
    """

    if item is ShopItem.SCRATCH:
        return random.randint(0, 20)
    if item is ShopItem.HUANLE:
        value = random.random()
        if value < 0.80:
            return 0
        if value < 0.99:
            return 1
        if value < 0.9995:
            return 5
        return 100
    return 0


__all__ = [
    "PoolFundingCommand",
    "ShopItem",
    "ShopOperations",
    "ShopPurchaseCommand",
    "ShopPurchaseResult",
    "draw_shop_reward",
]
