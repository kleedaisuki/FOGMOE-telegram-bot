"""@brief 每日抽奖应用命令、穷尽结果与事务端口 / Daily-lottery command, exhaustive results, and transaction port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fogmoe_bot.domain.economy.identity import EconomyAccountId
from fogmoe_bot.domain.economy.lottery import LotteryClaimInstant, LotteryPrize


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimLotteryCommand:
    """@brief 原子领取一次每日抽奖的应用命令 / Application command for one atomic daily-lottery claim.

    @param account_id 已验证的 Economy 账户身份 / Validated Economy account identity.
    @param proposed_prize 事务外预先生成的候选奖励 / Candidate prize pre-drawn outside the transaction.
    @param claimed_at 来源 Update 的领取时刻 / Claim instant from the source Update.
    @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
    @note 候选奖励故意不属于幂等请求指纹：重试会重新抽取，但必须回放首次已提交奖励。/
        The candidate prize deliberately is not part of idempotent request identity: a retry draws
        again but must replay the first committed prize.
    """

    account_id: EconomyAccountId
    """@brief 已验证 Economy 账户身份 / Validated Economy account identity."""

    proposed_prize: LotteryPrize
    """@brief 事务外生成的候选奖励 / Candidate prize generated outside the transaction."""

    claimed_at: LotteryClaimInstant
    """@brief 规范为 UTC 的领取时刻 / Claim instant canonicalized to UTC."""

    idempotency_key: str
    """@brief 来源 Update 幂等键 / Source-Update idempotency key."""

    def __post_init__(self) -> None:
        """@brief 验证应用消息边界 / Validate the application-message boundary.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised when a field has the wrong type.
        @raise ValueError 幂等键为空或过长时抛出 / Raised when the key is blank or too long.
        """

        if not isinstance(self.account_id, EconomyAccountId):
            raise TypeError("Lottery command requires EconomyAccountId")
        if not isinstance(self.proposed_prize, LotteryPrize):
            raise TypeError("Lottery command requires LotteryPrize")
        if not isinstance(self.claimed_at, LotteryClaimInstant):
            raise TypeError("Lottery command requires LotteryClaimInstant")
        if not isinstance(self.idempotency_key, str):
            raise TypeError("Lottery idempotency key must be a string")
        if not self.idempotency_key.strip() or len(self.idempotency_key) > 200:
            raise ValueError("Lottery idempotency key must contain 1-200 characters")


@dataclass(frozen=True, slots=True, kw_only=True)
class LotteryGrantedResult:
    """@brief 已成功发放奖励的用例结果 / Use-case result for a successfully granted prize.

    @param prize 已发奖励 / Granted prize.
    @param next_eligible_at 下次资格时刻 / Next eligibility instant.
    @param replayed 是否来自已提交回执 / Whether restored from a committed receipt.
    """

    prize: LotteryPrize
    """@brief 已发奖励 / Granted prize."""

    next_eligible_at: LotteryClaimInstant
    """@brief 下次资格时刻 / Next eligibility instant."""

    replayed: bool = False
    """@brief 是否来自已提交回执 / Whether restored from a committed receipt."""

    def __post_init__(self) -> None:
        """@brief 验证成功结果形状 / Validate the granted-result shape.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised when a field has the wrong type.
        """

        if not isinstance(self.prize, LotteryPrize):
            raise TypeError("Granted lottery result requires LotteryPrize")
        if not isinstance(self.next_eligible_at, LotteryClaimInstant):
            raise TypeError("Granted lottery result requires LotteryClaimInstant")
        if not isinstance(self.replayed, bool):
            raise TypeError("Lottery replay marker must be a boolean")


@dataclass(frozen=True, slots=True, kw_only=True)
class LotteryAlreadyClaimedResult:
    """@brief 尚在领取间隔内的用例结果 / Use-case result for a claim still within its interval.

    @param next_eligible_at 下次资格时刻 / Next eligibility instant.
    @param replayed 是否来自已提交回执 / Whether restored from a committed receipt.
    """

    next_eligible_at: LotteryClaimInstant
    """@brief 下次资格时刻 / Next eligibility instant."""

    replayed: bool = False
    """@brief 是否来自已提交回执 / Whether restored from a committed receipt."""

    def __post_init__(self) -> None:
        """@brief 验证冷却结果形状 / Validate the cooldown-result shape.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised when a field has the wrong type.
        """

        if not isinstance(self.next_eligible_at, LotteryClaimInstant):
            raise TypeError("Repeated lottery result requires LotteryClaimInstant")
        if not isinstance(self.replayed, bool):
            raise TypeError("Lottery replay marker must be a boolean")


@dataclass(frozen=True, slots=True, kw_only=True)
class LotteryNotRegisteredResult:
    """@brief Economy 账户不存在的用例结果 / Use-case result for a missing Economy account.

    @param replayed 固定为 False；未注册结果不写 receipt / Always False because missing-account results are not receipted.
    """

    replayed: bool = False
    """@brief 固定为 False / Always False."""

    def __post_init__(self) -> None:
        """@brief 拒绝伪造的未注册重放结果 / Reject a fabricated replay of a missing-account result.

        @return None / None.
        @raise TypeError replayed 不是 bool 时抛出 / Raised when replayed is not a bool.
        @raise ValueError replayed 为 True 时抛出 / Raised when replayed is true.
        """

        if not isinstance(self.replayed, bool):
            raise TypeError("Lottery replay marker must be a boolean")
        if self.replayed:
            raise ValueError("Unregistered lottery result cannot be replayed")


type LotteryResult = (
    LotteryGrantedResult | LotteryAlreadyClaimedResult | LotteryNotRegisteredResult
)
"""@brief 每日抽奖用例的穷尽结果 / Exhaustive daily-lottery use-case result."""


class LotteryClaimTransaction(Protocol):
    """@brief 原子抽奖领取事务端口 / Atomic daily-lottery claim transaction port."""

    async def claim_lottery(self, command: ClaimLotteryCommand) -> LotteryResult:
        """@brief 原子映射领域决策、账本发放与回执 / Atomically map the domain decision, ledger grant, and receipt.

        @param command 已验证领取命令 / Validated claim command.
        @return 稳定且可重放的用例结果 / Stable replayable use-case result.
        """

        ...


__all__ = [
    "ClaimLotteryCommand",
    "LotteryAlreadyClaimedResult",
    "LotteryClaimTransaction",
    "LotteryGrantedResult",
    "LotteryNotRegisteredResult",
    "LotteryResult",
]
