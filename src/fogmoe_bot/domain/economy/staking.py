"""@brief 质押领域规则 / Staking domain rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

REWARD_INTERVAL_DAYS = 7
"""@brief 领奖窗口天数 / Reward-window length in days."""

WITHDRAW_FEE_RATE = Decimal("0.03")
"""@brief 取回本金手续费率 / Principal-withdrawal fee rate."""

MAX_DAILY_RATE = Decimal("0.3")
"""@brief 旧产品最高日回报百分比 / Legacy maximum daily percentage rate."""

MIN_DAILY_RATE = Decimal("0.05")
"""@brief 旧产品最低日回报百分比 / Legacy minimum daily percentage rate."""


@dataclass(frozen=True, slots=True)
class AccountBalance:
    """@brief 可锁定的用户金币快照 / Lockable user coin snapshot.

    @param user_id 用户 ID / User ID.
    @param free 免费金币 / Free coins.
    @param paid 付费金币 / Paid coins.
    @param plan 账户计划 / Account plan.
    """

    user_id: int
    free: int
    paid: int
    plan: str

    def __post_init__(self) -> None:
        """@brief 校验金币快照 / Validate the coin snapshot.

        @return None / None.
        """

        if self.user_id <= 0:
            raise ValueError("Account user_id must be positive")
        if min(self.free, self.paid) < 0:
            raise ValueError("Account balances cannot be negative")

    @property
    def total(self) -> int:
        """@brief 返回可用总余额 / Return the total spendable balance.

        @return 免费与付费金币之和 / Sum of free and paid coins.
        """

        return self.free + self.paid

    def spend(self, amount: int) -> AccountBalance | None:
        """@brief 按免费后付费的旧规则扣费 / Spend free coins before paid coins using the legacy rule.

        @param amount 正整数扣费额 / Positive charge.
        @return 扣费后快照；余额不足为 None / Post-charge snapshot, or None when insufficient.
        """

        if amount <= 0:
            raise ValueError("Spend amount must be positive")
        if self.total < amount:
            return None
        if self.free >= amount:
            return AccountBalance(
                user_id=self.user_id,
                free=self.free - amount,
                paid=self.paid,
                plan=self.plan,
            )
        return AccountBalance(
            user_id=self.user_id,
            free=0,
            paid=self.paid - (amount - self.free),
            plan=self.plan,
        )


@dataclass(frozen=True, slots=True)
class StakePosition:
    """@brief 用户质押头寸 / User staking position.

    @param user_id 用户 ID / User ID.
    @param amount 本金 / Principal.
    @param staked_at 开仓时间 / Opening time.
    @param last_reward_at 上次已结算边界 / Last settled boundary.
    @param version OCC 版本 / OCC version.
    """

    user_id: int
    amount: int
    staked_at: datetime
    last_reward_at: datetime | None
    version: int = 0

    def __post_init__(self) -> None:
        """@brief 校验质押头寸 / Validate the staking position.

        @return None / None.
        """

        if self.user_id <= 0 or self.amount <= 0 or self.version < 0:
            raise ValueError("Invalid staking position")

    @property
    def reward_cursor(self) -> datetime:
        """@brief 返回奖励结算游标 / Return the reward settlement cursor.

        @return 上次结算或开仓时间 / Last settlement or opening time.
        """

        return self.last_reward_at or self.staked_at


class StakeAction(StrEnum):
    """@brief 质押用例结果代码 / Staking use-case result code."""

    STATUS = "status"
    """@brief 当前状态快照 / Current status snapshot."""

    OPENED = "opened"
    """@brief 已开仓 / Position opened."""

    ALREADY_STAKED = "already_staked"
    """@brief 已有质押 / Position already exists."""

    NOT_REGISTERED = "not_registered"
    """@brief 账户不存在 / Account does not exist."""

    INSUFFICIENT_COINS = "insufficient_coins"
    """@brief 余额不足 / Insufficient balance."""

    NO_STAKE = "no_stake"
    """@brief 无质押 / No position exists."""

    TOO_EARLY = "too_early"
    """@brief 未到领奖窗口 / Reward window has not elapsed."""

    BELOW_ONE_COIN = "below_one_coin"
    """@brief 累计奖励不足一枚 / Accrued reward is below one coin."""

    POOL_EMPTY = "pool_empty"
    """@brief 奖励池不足 / Reward pool is insufficient."""

    COLLECTED = "collected"
    """@brief 已领取 / Reward collected."""

    WITHDRAWN = "withdrawn"
    """@brief 已取回本金 / Principal withdrawn."""


@dataclass(frozen=True, slots=True)
class StakeDecision:
    """@brief 与 Telegram 无关的质押决策 / Telegram-independent staking decision.

    @param action 结果代码 / Result code.
    @param position 操作后头寸 / Post-operation position.
    @param available 可用余额 / Available account balance.
    @param reward 已发奖励 / Paid reward.
    @param principal 已返还本金 / Refunded principal.
    @param fee 手续费 / Fee.
    @param daily_rate 日回报百分比 / Daily percentage rate.
    @param replayed 是否为幂等回放 / Whether this is an idempotent replay.
    """

    action: StakeAction
    position: StakePosition | None = None
    available: int = 0
    reward: int = 0
    principal: int = 0
    fee: int = 0
    daily_rate: Decimal = MAX_DAILY_RATE
    replayed: bool = False


def calculate_daily_reward_rate(total_coins: int, total_staked: int) -> Decimal:
    """@brief 根据流通量计算旧产品日回报率 / Calculate the legacy daily percentage rate from supply.

    @param total_coins 未质押金币 / Unstaked coin supply.
    @param total_staked 已质押金币 / Staked principal.
    @return 0.05 至 0.3 的百分比数值 / Percentage value between 0.05 and 0.3.
    """

    if total_coins < 0 or total_staked < 0:
        raise ValueError("Coin supply cannot be negative")
    if total_coins == 0 or total_staked == 0:
        return MAX_DAILY_RATE
    ratio = min(
        Decimal(1),
        Decimal(total_staked) / Decimal(total_coins + total_staked),
    )
    return MAX_DAILY_RATE - ratio * (MAX_DAILY_RATE - MIN_DAILY_RATE)


def calculate_reward_for_intervals(
    stake_amount: int,
    daily_rate: Decimal,
    intervals: int,
) -> int:
    """@brief 计算完整结算窗口的奖励 / Calculate rewards for complete settlement windows.

    @param stake_amount 本金 / Principal.
    @param daily_rate 日回报百分比 / Daily percentage rate.
    @param intervals 完整七日窗口数 / Number of complete seven-day windows.
    @return 向下取整的金币奖励 / Coin reward rounded down.
    """

    if stake_amount <= 0 or intervals <= 0:
        return 0
    reward_days = intervals * REWARD_INTERVAL_DAYS
    reward = Decimal(stake_amount) * daily_rate * Decimal(reward_days) / Decimal(100)
    return max(0, int(reward))


def calculate_reward_window(
    position: StakePosition,
    daily_rate: Decimal,
    *,
    now: datetime,
) -> tuple[int, int, datetime]:
    """@brief 计算已到期奖励窗口 / Calculate matured reward windows.

    @param position 质押头寸 / Staking position.
    @param daily_rate 日回报百分比 / Daily percentage rate.
    @param now 业务时间 / Business time.
    @return 应付奖励、窗口数与原游标 / Due reward, window count, and original cursor.
    """

    cursor = position.reward_cursor
    elapsed = max(0, int((now - cursor).total_seconds()))
    intervals = elapsed // (REWARD_INTERVAL_DAYS * 86_400)
    return (
        calculate_reward_for_intervals(position.amount, daily_rate, intervals),
        intervals,
        cursor,
    )


def calculate_payable_intervals(
    *,
    stake_amount: int,
    daily_rate: Decimal,
    intervals_due: int,
    pool_balance: Decimal,
) -> int:
    """@brief 二分求奖励池可支付的最大窗口数 / Find the maximum pool-funded windows by binary search.

    @param stake_amount 本金 / Principal.
    @param daily_rate 日回报百分比 / Daily percentage rate.
    @param intervals_due 到期窗口数 / Matured window count.
    @param pool_balance 奖励池可用额 / Available pool balance.
    @return 可支付窗口数 / Payable window count.
    """

    if intervals_due <= 0 or pool_balance <= 0:
        return 0
    low = 0
    high = intervals_due
    while low < high:
        middle = (low + high + 1) // 2
        reward = calculate_reward_for_intervals(stake_amount, daily_rate, middle)
        if Decimal(reward) <= pool_balance:
            low = middle
        else:
            high = middle - 1
    if calculate_reward_for_intervals(stake_amount, daily_rate, low) <= 0:
        return 0
    return low


def advance_reward_cursor(cursor: datetime, intervals: int) -> datetime:
    """@brief 按已付窗口推进游标 / Advance a reward cursor by paid windows.

    @param cursor 原游标 / Original cursor.
    @param intervals 已付窗口数 / Paid window count.
    @return 新游标 / New cursor.
    """

    if intervals <= 0:
        raise ValueError("Cursor advancement requires a positive interval count")
    return cursor + timedelta(days=intervals * REWARD_INTERVAL_DAYS)
