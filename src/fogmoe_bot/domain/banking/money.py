"""@brief 金币与钱包值对象 / Token and wallet value objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TokenBucket(StrEnum):
    """@brief 用户金币子账户 / User token sub-account."""

    FREE = "free"
    """@brief 活动和游戏获得的免费金币 / Free tokens earned through activities and games."""

    PAID = "paid"
    """@brief 历史付费金币隔离口袋 / Isolated legacy-paid token pocket.

    @note 新订阅交付权益（entitlement）而非此类金币；该口袋只保留迁移、退款和
        经审计纠错所需的历史区分。/
        New subscriptions grant entitlements rather than these tokens; this pocket exists
        only to preserve migration, refund, and audited-correction distinctions.
    """


class SystemAccountKind(StrEnum):
    """@brief 银行管理的系统账户 / Bank-managed system account."""

    ISSUANCE = "issuance"
    """@brief 银行发行账户 / Bank issuance account."""

    BURN = "burn"
    """@brief 永久销毁账户 / Permanent burn account."""

    GROUP_TREASURY = "group_treasury"
    """@brief 群组小镇金库 / Group-town treasury."""

    ACTIVITY_POT = "activity_pot"
    """@brief 活动托管奖池 / Activity escrow pot."""


@dataclass(frozen=True, slots=True, order=True)
class TokenAmount:
    """@brief 严格正的金币数量 / Strictly positive token amount.

    @param value 金币个数 / Number of tokens.
    """

    value: int
    """@brief 原始整数数量 / Raw integral amount."""

    def __post_init__(self) -> None:
        """@brief 验证金币数量 / Validate the token amount.

        @return None / None.
        @raise TypeError 值不是严格整数时抛出 / Raised when the value is not a strict integer.
        @raise ValueError 金额不为正时抛出 / Raised when the amount is not positive.
        """

        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("Token amount must be an integer")
        if self.value <= 0:
            raise ValueError("Token amount must be positive")

    def __int__(self) -> int:
        """@brief 返回原始整数 / Return the raw integer.

        @return 严格正整数 / Strictly positive integer.
        """

        return self.value


@dataclass(frozen=True, slots=True, order=True)
class WalletBalance:
    """@brief 一个钱包子账户的非负余额 / Non-negative balance of one wallet bucket.

    @param bucket 免费或付费钱包 / Free or paid wallet.
    @param value 非负金币数 / Non-negative token count.
    """

    bucket: TokenBucket
    """@brief 钱包类别 / Wallet bucket."""

    value: int
    """@brief 非负余额 / Non-negative balance."""

    def __post_init__(self) -> None:
        """@brief 验证余额不变量 / Validate balance invariants.

        @return None / None.
        @raise TypeError 余额不是严格整数时抛出 / Raised when the balance is not a strict integer.
        @raise ValueError 余额为负时抛出 / Raised when the balance is negative.
        """

        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("Wallet balance must be an integer")
        if self.value < 0:
            raise ValueError("Wallet balance cannot be negative")

    def can_cover(self, amount: TokenAmount) -> bool:
        """@brief 判断余额是否足够 / Check whether the balance can cover an amount.

        @param amount 待扣金币 / Tokens to debit.
        @return 余额足够时为 True / True when the balance is sufficient.
        """

        return self.value >= amount.value
