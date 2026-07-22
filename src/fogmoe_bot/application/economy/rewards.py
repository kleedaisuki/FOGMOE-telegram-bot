"""@brief 签到与抽奖应用模型及端口 / Check-in and lottery models and port."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from .common import EconomyCode


@dataclass(frozen=True, slots=True)
class CheckInCommand:
    """@brief 签到命令 / Check-in command.

    @param user_id 用户 ID / User ID.
    @param day 业务日期 / Business date.
    @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
    """

    user_id: int
    day: date
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CheckInResult:
    """@brief 签到结果 / Check-in result.

    @param code 结果代码 / Result code.
    @param consecutive_days 连续天数 / Consecutive days.
    @param reward 奖励金币 / Reward coins.
    @param replayed 是否幂等回放 / Whether replayed.
    """

    code: EconomyCode
    consecutive_days: int = 0
    reward: int = 0
    replayed: bool = False


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
    prize: int
    claimed_at: datetime
    cooldown: timedelta
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class LotteryResult:
    """@brief 每日抽奖结果 / Daily-lottery result.

    @param code 结果代码 / Result code.
    @param prize 实际奖励 / Granted prize.
    @param next_eligible_at 下次可领取时刻 / Next eligible instant.
    @param replayed 是否回放已提交结果 / Whether a committed result was replayed.
    """

    code: EconomyCode
    prize: int = 0
    next_eligible_at: datetime | None = None
    replayed: bool = False


class RewardOperations(Protocol):
    """@brief 签到与抽奖持久化能力端口 / Check-in and lottery persistence capability port."""

    async def check_in(self, command: CheckInCommand) -> CheckInResult:
        """@brief 原子签到 / Atomically check in.

        @param command 签到命令 / Check-in command.
        @return 签到结果 / Check-in result.
        """

        ...

    async def claim_lottery(self, command: LotteryCommand) -> LotteryResult:
        """@brief 原子领取每日抽奖 / Atomically claim a daily lottery prize.

        @param command 抽奖命令 / Lottery command.
        @return 抽奖结果 / Lottery result.
        """

        ...


def calculate_checkin_reward(consecutive_days: int) -> int:
    """@brief 计算旧的阶梯签到奖励 / Calculate the legacy tiered check-in reward.

    @param consecutive_days 连续天数 / Consecutive days.
    @return 1 至 7 枚金币 / Between 1 and 7 coins.
    """

    if consecutive_days <= 0:
        raise ValueError("Consecutive days must be positive")
    return min(7, (consecutive_days - 1) // 5 + 1)


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
    "CheckInCommand",
    "CheckInResult",
    "LotteryCommand",
    "LotteryResult",
    "RewardOperations",
    "calculate_checkin_reward",
    "draw_lottery_prize",
]
