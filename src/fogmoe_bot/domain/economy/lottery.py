"""@brief 每日抽奖聚合、资格规则与奖励策略 / Daily-lottery aggregate, eligibility rule, and prize policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, Self, assert_never

from .identity import EconomyAccountId

DAILY_LOTTERY_COOLDOWN: Final = timedelta(hours=24)
"""@brief 两次成功领取之间的固定间隔 / Fixed interval between successful claims."""


@dataclass(frozen=True, slots=True, order=True)
class LotteryClaimInstant:
    """@brief 规范为 UTC 的抽奖领取时刻 / Lottery claim instant canonicalized to UTC.

    @param value naive UTC 或任意 aware datetime / Naive UTC or any aware datetime.
    @note 为保持既有持久化语义，naive datetime 被解释为 UTC，而不是主机本地时区。/
        To retain established persistence semantics, a naive datetime means UTC rather than the
        host's local timezone.
    """

    value: datetime
    """@brief aware UTC 时刻 / Aware UTC instant."""

    def __post_init__(self) -> None:
        """@brief 验证并规范领取时刻 / Validate and canonicalize the claim instant.

        @return None / None.
        @raise TypeError 值不是严格 datetime 时抛出 / Raised when the value is not a strict datetime.
        """

        if type(self.value) is not datetime:
            raise TypeError("Lottery claim instant must be a datetime")
        normalized = (
            self.value.replace(tzinfo=UTC)
            if self.value.tzinfo is None
            else self.value.astimezone(UTC)
        )
        object.__setattr__(self, "value", normalized)

    def after_daily_cooldown(self) -> Self:
        """@brief 返回本次领取之后的下次资格时刻 / Return the next eligibility instant after this claim.

        @return 恰好二十四小时后的 UTC 时刻 / UTC instant exactly twenty-four hours later.
        """

        return type(self)(self.value + DAILY_LOTTERY_COOLDOWN)


@dataclass(frozen=True, slots=True, order=True)
class LotteryPrize:
    """@brief 每日抽奖可产生的金币奖励 / Token prize producible by the daily lottery.

    @param coins 奖励金币数，范围为 1 至 20 / Prize amount in the inclusive range 1 through 20.
    """

    coins: int
    """@brief 奖励金币数量 / Prize token amount."""

    def __post_init__(self) -> None:
        """@brief 验证奖励范围 / Validate the prize range.

        @return None / None.
        @raise TypeError 奖励不是严格整数时抛出 / Raised when the prize is not a strict integer.
        @raise ValueError 奖励不在 1 至 20 时抛出 / Raised when the prize is outside 1 through 20.
        """

        if isinstance(self.coins, bool) or not isinstance(self.coins, int):
            raise TypeError("Lottery prize must be an integer")
        if not 1 <= self.coins <= 20:
            raise ValueError("Lottery prize must contain between one and twenty coins")

    def __int__(self) -> int:
        """@brief 返回原始金币数 / Return the raw token amount.

        @return 1 至 20 的整数 / Integer between 1 and 20.
        """

        return self.coins


class LotteryPrizeBand(StrEnum):
    """@brief 既有每日抽奖奖励档位 / Established daily-lottery prize bands."""

    SMALL = "small"
    """@brief 1 至 4 枚金币档 / One-to-four token band."""

    LARGE = "large"
    """@brief 11 至 20 枚金币档 / Eleven-to-twenty token band."""

    MEDIUM = "medium"
    """@brief 5 至 10 枚金币档 / Five-to-ten token band."""


PRIZE_BANDS: Final = (
    LotteryPrizeBand.SMALL,
    LotteryPrizeBand.LARGE,
    LotteryPrizeBand.MEDIUM,
)
"""@brief 保持既有随机调用顺序的档位元组 / Band tuple retaining the established random-call order."""

PRIZE_BAND_WEIGHTS: Final = (0.4, 0.1, 0.5)
"""@brief small、large、medium 的既有权重 / Established weights for small, large, and medium."""


class LotteryRandomness(Protocol):
    """@brief 每日抽奖策略所需的最窄随机能力 / Narrow randomness capability required by the prize policy."""

    def choose_prize_band(
        self,
        bands: tuple[LotteryPrizeBand, ...],
        weights: tuple[float, ...],
    ) -> LotteryPrizeBand:
        """@brief 按权重选择一个奖励档位 / Choose one prize band by weight.

        @param bands 有序候选档位 / Ordered candidate bands.
        @param weights 与候选一一对应的权重 / Weights corresponding to the candidates.
        @return 被选中的档位 / Selected band.
        """

        ...

    def integer_between(self, lower: int, upper: int) -> int:
        """@brief 从闭区间取得整数 / Obtain an integer from an inclusive interval.

        @param lower 闭区间下界 / Inclusive lower bound.
        @param upper 闭区间上界 / Inclusive upper bound.
        @return 区间内整数 / Integer inside the interval.
        """

        ...


def draw_lottery_prize(randomness: LotteryRandomness) -> LotteryPrize:
    """@brief 按既有权重和档位生成每日奖励 / Draw a daily prize using the established weights and bands.

    @param randomness 注入的随机能力 / Injected randomness capability.
    @return 经过领域约束的奖励 / Domain-constrained prize.
    """

    band = randomness.choose_prize_band(PRIZE_BANDS, PRIZE_BAND_WEIGHTS)
    match band:
        case LotteryPrizeBand.SMALL:
            bounds = (1, 4)
        case LotteryPrizeBand.LARGE:
            bounds = (11, 20)
        case LotteryPrizeBand.MEDIUM:
            bounds = (5, 10)
        case unreachable:
            assert_never(unreachable)
    return LotteryPrize(randomness.integer_between(*bounds))


@dataclass(frozen=True, slots=True, kw_only=True)
class LotteryGranted:
    """@brief 已接受并产生奖励的抽奖决策 / Accepted lottery decision carrying a prize.

    @param claimed_at 已接受的领取时刻 / Accepted claim instant.
    @param prize 本次应发奖励 / Prize to grant.
    @param next_eligible_at 下次资格时刻 / Next eligibility instant.
    """

    claimed_at: LotteryClaimInstant
    """@brief 已接受领取时刻 / Accepted claim instant."""

    prize: LotteryPrize
    """@brief 本次应发奖励 / Prize to grant."""

    next_eligible_at: LotteryClaimInstant
    """@brief 下次资格时刻 / Next eligibility instant."""

    def __post_init__(self) -> None:
        """@brief 拒绝不能由生命周期产生的授予决策 / Reject a grant impossible for the lifecycle.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised when a field has the wrong type.
        @raise ValueError 下次资格时刻不匹配时抛出 / Raised when next eligibility is inconsistent.
        """

        if not isinstance(self.claimed_at, LotteryClaimInstant):
            raise TypeError("Granted lottery requires LotteryClaimInstant")
        if not isinstance(self.prize, LotteryPrize):
            raise TypeError("Granted lottery requires LotteryPrize")
        if not isinstance(self.next_eligible_at, LotteryClaimInstant):
            raise TypeError(
                "Granted lottery next eligibility must be LotteryClaimInstant"
            )
        if self.next_eligible_at != self.claimed_at.after_daily_cooldown():
            raise ValueError("Granted lottery next eligibility must follow its claim")


@dataclass(frozen=True, slots=True, kw_only=True)
class LotteryAlreadyClaimed:
    """@brief 尚在二十四小时间隔内的拒绝决策 / Rejection within the twenty-four-hour interval.

    @param attempted_at 本次尝试时刻 / Attempted claim instant.
    @param next_eligible_at 下次资格时刻 / Next eligibility instant.
    """

    attempted_at: LotteryClaimInstant
    """@brief 本次尝试时刻 / Attempted claim instant."""

    next_eligible_at: LotteryClaimInstant
    """@brief 下次资格时刻 / Next eligibility instant."""

    def __post_init__(self) -> None:
        """@brief 验证拒绝决策的时间关系 / Validate the rejection's temporal relationship.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised when a field has the wrong type.
        @raise ValueError 尝试时刻已具资格时抛出 / Raised when the attempt is already eligible.
        """

        if not isinstance(self.attempted_at, LotteryClaimInstant):
            raise TypeError("Rejected lottery attempt requires LotteryClaimInstant")
        if not isinstance(self.next_eligible_at, LotteryClaimInstant):
            raise TypeError(
                "Rejected lottery next eligibility must be LotteryClaimInstant"
            )
        if self.attempted_at >= self.next_eligible_at:
            raise ValueError("Rejected lottery attempt must precede next eligibility")


type LotteryDecision = LotteryGranted | LotteryAlreadyClaimed
"""@brief 每日抽奖聚合的穷尽决策 / Exhaustive decision emitted by the daily-lottery aggregate."""


class DailyLottery:
    """@brief 以 Economy 账户为身份的每日抽奖生命周期 / Daily-lottery lifecycle identified by an Economy account.

    该聚合是最近成功领取时刻的唯一修改入口。资格边界采用半开区间：早于下次资格
    时刻会被拒绝，恰好等于该时刻则成功。/
    This aggregate is the sole mutation boundary for the latest successful claim. Eligibility
    uses a half-open boundary: an attempt before the next instant is rejected, while equality is
    accepted.
    """

    __slots__ = ("_account_id", "_last_claimed_at")
    """@brief 聚合私有状态槽 / Private aggregate state slots."""

    def __init__(self, account_id: EconomyAccountId) -> None:
        """@brief 创建尚未成功领取的生命周期 / Create a lifecycle with no successful claim.

        @param account_id Economy 账户身份 / Economy account identity.
        @return None / None.
        @raise TypeError 账户身份非法时抛出 / Raised when the account identity has the wrong type.
        """

        if not isinstance(account_id, EconomyAccountId):
            raise TypeError("Daily lottery requires EconomyAccountId")
        self._account_id = account_id
        self._last_claimed_at: LotteryClaimInstant | None = None

    @classmethod
    def restore(
        cls,
        *,
        account_id: EconomyAccountId,
        last_claimed_at: LotteryClaimInstant,
    ) -> Self:
        """@brief 从持久化快照恢复生命周期 / Restore the lifecycle from persistence.

        @param account_id Economy 账户身份 / Economy account identity.
        @param last_claimed_at 最近成功领取时刻 / Latest successful claim instant.
        @return 已恢复聚合 / Restored aggregate.
        @raise TypeError 最近领取时刻类型非法时抛出 / Raised when the instant has the wrong type.
        """

        if not isinstance(last_claimed_at, LotteryClaimInstant):
            raise TypeError("Restored daily lottery requires LotteryClaimInstant")
        restored = cls(account_id)
        restored._last_claimed_at = last_claimed_at
        return restored

    @property
    def account_id(self) -> EconomyAccountId:
        """@brief 返回聚合身份 / Return the aggregate identity.

        @return Economy 账户身份 / Economy account identity.
        """

        return self._account_id

    @property
    def last_claimed_at(self) -> LotteryClaimInstant | None:
        """@brief 返回最近成功领取时刻 / Return the latest successful claim instant.

        @return 尚未成功时为 None，否则为 UTC 时刻 / None before success, otherwise a UTC instant.
        """

        return self._last_claimed_at

    def claim(
        self,
        *,
        claimed_at: LotteryClaimInstant,
        proposed_prize: LotteryPrize,
    ) -> LotteryDecision:
        """@brief 尝试领取一次每日抽奖奖励 / Attempt to claim one daily-lottery prize.

        @param claimed_at 本次尝试时刻 / Attempt instant.
        @param proposed_prize 本次外部随机策略预先产生的候选奖励 / Candidate prize pre-drawn by the random policy.
        @return 授予或尚在间隔内的穷尽决策 / Exhaustive grant-or-cooldown decision.
        @raise TypeError 参数类型非法时抛出 / Raised when an argument has the wrong type.
        @note 候选奖励在拒绝时被丢弃，以保持随机副作用与数据库事务分离。/
            A rejected attempt discards its candidate prize, keeping random effects outside the
            database transaction.
        """

        if not isinstance(claimed_at, LotteryClaimInstant):
            raise TypeError("Daily-lottery claim requires LotteryClaimInstant")
        if not isinstance(proposed_prize, LotteryPrize):
            raise TypeError("Daily-lottery claim requires LotteryPrize")
        if self._last_claimed_at is not None:
            next_eligible_at = self._last_claimed_at.after_daily_cooldown()
            if claimed_at < next_eligible_at:
                return LotteryAlreadyClaimed(
                    attempted_at=claimed_at,
                    next_eligible_at=next_eligible_at,
                )
        self._last_claimed_at = claimed_at
        return LotteryGranted(
            claimed_at=claimed_at,
            prize=proposed_prize,
            next_eligible_at=claimed_at.after_daily_cooldown(),
        )

    def __eq__(self, other: object) -> bool:
        """@brief 按稳定账户身份比较聚合 / Compare aggregates by stable account identity.

        @param other 待比较对象 / Object to compare.
        @return 同一 Economy 账户时为 True / True for the same Economy account.
        """

        if not isinstance(other, DailyLottery):
            return NotImplemented
        return self._account_id == other._account_id

    def __hash__(self) -> int:
        """@brief 返回不受领取变化影响的身份哈希 / Return an identity hash stable across claims.

        @return 账户身份哈希 / Account-identity hash.
        """

        return hash((DailyLottery, self._account_id))


__all__ = [
    "DAILY_LOTTERY_COOLDOWN",
    "DailyLottery",
    "LotteryAlreadyClaimed",
    "LotteryClaimInstant",
    "LotteryDecision",
    "LotteryGranted",
    "LotteryPrize",
    "LotteryPrizeBand",
    "LotteryRandomness",
    "PRIZE_BANDS",
    "PRIZE_BAND_WEIGHTS",
    "draw_lottery_prize",
]
