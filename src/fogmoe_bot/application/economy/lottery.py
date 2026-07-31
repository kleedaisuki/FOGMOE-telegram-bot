"""@brief 每日抽奖应用消息与端口 / Daily-lottery application messages and port."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .common import EconomyCode


@dataclass(frozen=True, slots=True)
class LotteryCommand:
    """@brief 每日抽奖的原子命令 / Atomic daily-lottery command.

    @param user_id 用户 ID / User ID.
    @param prize 事务外抽取的奖励 / Prize drawn outside the transaction.
    @param claimed_at 领取时刻 / Claim instant.
    @param cooldown 再次领取间隔 / Re-claim interval.
    @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
    """

    user_id: int
    """@brief 用户 ID / User ID."""

    prize: int
    """@brief 事务外抽取的奖励 / Prize drawn outside the transaction."""

    claimed_at: datetime
    """@brief 领取时刻 / Claim instant."""

    cooldown: timedelta
    """@brief 再次领取间隔 / Re-claim interval."""

    idempotency_key: str
    """@brief 来源 Update 幂等键 / Source-Update idempotency key."""


@dataclass(frozen=True, slots=True)
class LotteryResult:
    """@brief 每日抽奖结果 / Daily-lottery result.

    @param code 结果代码 / Result code.
    @param prize 实际奖励 / Granted prize.
    @param next_eligible_at 下次可领取时刻 / Next eligible instant.
    @param replayed 是否回放已提交结果 / Whether a committed result was replayed.
    """

    code: EconomyCode
    """@brief 结果代码 / Result code."""

    prize: int = 0
    """@brief 实际奖励 / Granted prize."""

    next_eligible_at: datetime | None = None
    """@brief 下次可领取时刻 / Next eligible instant."""

    replayed: bool = False
    """@brief 是否回放已提交结果 / Whether a committed result was replayed."""


class LotteryOperations(Protocol):
    """@brief 每日抽奖持久化能力端口 / Daily-lottery persistence capability port."""

    async def claim_lottery(self, command: LotteryCommand) -> LotteryResult:
        """@brief 原子领取每日抽奖 / Atomically claim a daily lottery prize.

        @param command 抽奖命令 / Lottery command.
        @return 抽奖结果 / Lottery result.
        """

        ...


def draw_lottery_prize() -> int:
    """@brief 按既有分布在事务外抽取奖励 / Draw a prize outside the transaction using the established distribution.

    @return 1 至 20 枚金币 / Between 1 and 20 coins.
    """

    bucket = random.choices(("small", "large", "medium"), (0.4, 0.1, 0.5), k=1)[0]
    if bucket == "small":
        return random.randint(1, 4)
    if bucket == "large":
        return random.randint(11, 20)
    return random.randint(5, 10)


__all__ = [
    "LotteryCommand",
    "LotteryOperations",
    "LotteryResult",
    "draw_lottery_prize",
]
