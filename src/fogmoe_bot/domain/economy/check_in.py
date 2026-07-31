"""@brief 每日签到聚合与阶梯奖励规则 / Daily check-in aggregate and tiered reward policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Self

from .identity import EconomyAccountId


@dataclass(frozen=True, slots=True, order=True)
class CheckInStreakLength:
    """@brief 严格为正的连续签到天数 / Strictly positive check-in streak length.

    @param days 连续签到天数 / Number of consecutive check-in days.
    """

    days: int
    """@brief 连续签到天数 / Consecutive check-in days."""

    def __post_init__(self) -> None:
        """@brief 验证连续天数 / Validate the streak length.

        @return None / None.
        @raise TypeError 天数不是严格整数时抛出 / Raised when days is not a strict integer.
        @raise ValueError 天数不为正时抛出 / Raised when days is not positive.
        """

        if isinstance(self.days, bool) or not isinstance(self.days, int):
            raise TypeError("Check-in streak length must be an integer")
        if self.days <= 0:
            raise ValueError("Check-in streak length must be positive")


@dataclass(frozen=True, slots=True, order=True)
class CheckInReward:
    """@brief 一次签到产生的免费金币奖励 / Free-token reward produced by one check-in.

    @param coins 奖励金币数量，范围为 1 至 7 / Reward amount in the inclusive range 1 through 7.
    """

    coins: int
    """@brief 免费金币奖励 / Free-token reward."""

    def __post_init__(self) -> None:
        """@brief 验证奖励边界 / Validate reward boundaries.

        @return None / None.
        @raise TypeError 金币不是严格整数时抛出 / Raised when coins is not a strict integer.
        @raise ValueError 金币不在签到奖励范围时抛出 / Raised when coins is outside the check-in range.
        """

        if isinstance(self.coins, bool) or not isinstance(self.coins, int):
            raise TypeError("Check-in reward must be an integer")
        if not 1 <= self.coins <= 7:
            raise ValueError("Check-in reward must contain between one and seven coins")

    @classmethod
    def for_streak(cls, streak: CheckInStreakLength) -> Self:
        """@brief 按既有五日阶梯计算奖励 / Calculate the established five-day tier reward.

        @param streak 已完成签到后的连续天数 / Streak length after the claim.
        @return 1 至 7 枚免费金币 / Between one and seven free tokens.
        @raise TypeError 连续天数类型非法时抛出 / Raised when the streak type is invalid.
        """

        if not isinstance(streak, CheckInStreakLength):
            raise TypeError("Check-in reward requires CheckInStreakLength")
        return cls(min(7, (streak.days - 1) // 5 + 1))


@dataclass(frozen=True, slots=True)
class CheckInGranted:
    """@brief 已接受并产生奖励的签到决策 / Accepted check-in decision carrying a reward.

    @param claimed_on 已领取奖励的业务日期 / Business date whose reward was claimed.
    @param streak 更新后的连续天数 / Updated streak length.
    @param reward 本次应发奖励 / Reward to grant for this claim.
    """

    claimed_on: date
    """@brief 已领取的业务日期 / Claimed business date."""

    streak: CheckInStreakLength
    """@brief 更新后的连续天数 / Updated streak length."""

    reward: CheckInReward
    """@brief 本次应发奖励 / Reward due for this claim."""

    def __post_init__(self) -> None:
        """@brief 验证已接受决策的完整形状 / Validate the complete accepted-decision shape.

        @return None / None.
        @raise TypeError 日期、连续天数或奖励类型非法时抛出 /
            Raised when date, streak, or reward types are invalid.
        @raise ValueError 奖励不匹配连续天数时抛出 / Raised when reward does not match the streak.
        """

        if type(self.claimed_on) is not date:
            raise TypeError("Granted check-in day must be a date")
        if not isinstance(self.streak, CheckInStreakLength):
            raise TypeError("Granted check-in requires CheckInStreakLength")
        if not isinstance(self.reward, CheckInReward):
            raise TypeError("Granted check-in requires CheckInReward")
        if self.reward != CheckInReward.for_streak(self.streak):
            raise ValueError("Granted check-in reward does not match its streak")


@dataclass(frozen=True, slots=True)
class CheckInAlreadyClaimed:
    """@brief 同一业务日已签到的拒绝决策 / Rejection because the business day was already claimed.

    @param claimed_on 已经领取过的业务日期 / Business date already claimed.
    @param streak 保持不变的连续天数 / Unchanged streak length.
    """

    claimed_on: date
    """@brief 已经领取过的业务日期 / Business date already claimed."""

    streak: CheckInStreakLength
    """@brief 保持不变的连续天数 / Unchanged streak length."""

    def __post_init__(self) -> None:
        """@brief 验证重复领取决策 / Validate the repeated-claim decision.

        @return None / None.
        @raise TypeError 日期或连续天数类型非法时抛出 /
            Raised when date or streak types are invalid.
        """

        if type(self.claimed_on) is not date:
            raise TypeError("Repeated check-in day must be a date")
        if not isinstance(self.streak, CheckInStreakLength):
            raise TypeError("Repeated check-in requires CheckInStreakLength")


type CheckInDecision = CheckInGranted | CheckInAlreadyClaimed
"""@brief 签到聚合的穷尽决策 / Exhaustive decision emitted by the check-in aggregate."""


class CheckInStreak:
    """@brief 以 Economy 账户身份聚合的签到生命周期 / Check-in lifecycle aggregated by Economy account identity.

    该实体是同一账户签到状态的唯一修改入口。重复日期不改变状态；紧邻上一签到日时
    连续天数递增，其他日期保持既有语义并从一天重新开始。
    This entity is the sole mutation boundary for one account's check-in state. A duplicate date
    leaves state unchanged; the day immediately after the previous claim extends the streak, and
    every other date preserves established behavior by restarting at one day.
    """

    __slots__ = ("_account_id", "_last_claimed_on", "_length")
    """@brief 聚合私有状态槽 / Private aggregate state slots."""

    def __init__(self, account_id: EconomyAccountId) -> None:
        """@brief 创建尚未签到的账户生命周期 / Create a lifecycle with no prior check-in.

        @param account_id Economy 账户身份 / Economy account identity.
        @return None / None.
        @raise TypeError 账户身份类型非法时抛出 / Raised when account identity has the wrong type.
        """

        if not isinstance(account_id, EconomyAccountId):
            raise TypeError("Check-in streak requires EconomyAccountId")
        self._account_id = account_id
        self._last_claimed_on: date | None = None
        self._length: CheckInStreakLength | None = None

    @classmethod
    def restore(
        cls,
        *,
        account_id: EconomyAccountId,
        last_claimed_on: date,
        consecutive_days: int,
    ) -> Self:
        """@brief 从持久化快照恢复有效生命周期 / Restore a valid lifecycle from persistence.

        @param account_id Economy 账户身份 / Economy account identity.
        @param last_claimed_on 最近签到业务日期 / Most recent claimed business date.
        @param consecutive_days 已持久化连续天数 / Persisted consecutive-day count.
        @return 已恢复且满足不变量的实体 / Restored entity satisfying all invariants.
        @raise TypeError 日期或身份类型非法时抛出 / Raised for invalid date or identity types.
        @raise ValueError 连续天数不为正时抛出 / Raised when the persisted length is not positive.
        """

        if type(last_claimed_on) is not date:
            raise TypeError("Last check-in day must be a date")
        restored = cls(account_id)
        restored._last_claimed_on = last_claimed_on
        restored._length = CheckInStreakLength(consecutive_days)
        return restored

    @property
    def account_id(self) -> EconomyAccountId:
        """@brief 返回聚合身份 / Return the aggregate identity.

        @return Economy 账户身份 / Economy account identity.
        """

        return self._account_id

    @property
    def last_claimed_on(self) -> date | None:
        """@brief 返回最近签到日 / Return the most recent claimed day.

        @return 尚未签到时为 None，否则为业务日期 / None before the first claim, otherwise its business date.
        """

        return self._last_claimed_on

    @property
    def length(self) -> CheckInStreakLength | None:
        """@brief 返回当前连续天数 / Return the current streak length.

        @return 尚未签到时为 None，否则为严格正的连续天数 /
            None before the first claim, otherwise a strictly positive length.
        """

        return self._length

    def claim(self, claimed_on: date) -> CheckInDecision:
        """@brief 领取一个业务日的签到奖励 / Claim the reward for one business day.

        @param claimed_on 待领取业务日期 / Business date to claim.
        @return 已发奖励或当日已领取决策 / Granted or already-claimed decision.
        @raise TypeError 参数不是严格 date 时抛出 / Raised when the argument is not a strict date.
        """

        if type(claimed_on) is not date:
            raise TypeError("Check-in claim day must be a date")
        if self._last_claimed_on == claimed_on:
            current = self._length
            if current is None:
                raise RuntimeError("Claimed check-in day has no streak length")
            return CheckInAlreadyClaimed(claimed_on=claimed_on, streak=current)

        previous = self._length
        updated_days = (
            previous.days + 1
            if previous is not None
            and self._last_claimed_on == claimed_on - timedelta(days=1)
            else 1
        )
        updated = CheckInStreakLength(updated_days)
        self._last_claimed_on = claimed_on
        self._length = updated
        return CheckInGranted(
            claimed_on=claimed_on,
            streak=updated,
            reward=CheckInReward.for_streak(updated),
        )

    def __eq__(self, other: object) -> bool:
        """@brief 按稳定账户身份比较实体 / Compare entities by stable account identity.

        @param other 待比较对象 / Object to compare.
        @return 同一 Economy 账户时为 True / True for the same Economy account.
        """

        if not isinstance(other, CheckInStreak):
            return NotImplemented
        return self._account_id == other._account_id

    def __hash__(self) -> int:
        """@brief 返回不受生命周期变化影响的身份哈希 / Return an identity hash stable across lifecycle changes.

        @return 账户身份哈希 / Account-identity hash.
        """

        return hash((CheckInStreak, self._account_id))


__all__ = [
    "CheckInAlreadyClaimed",
    "CheckInDecision",
    "CheckInGranted",
    "CheckInReward",
    "CheckInStreak",
    "CheckInStreakLength",
]
