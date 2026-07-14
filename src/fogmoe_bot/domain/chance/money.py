"""@brief 随机活动专用免费金币值对象 / Free-token value objects for chance activities."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class FreeTokenStake:
    """@brief 只能用于随机活动的免费金币押注 / Free-token-only stake for chance activities.

    此类型故意不接受钱包类别（wallet bucket）或通用付费资产。随机活动的调用边界
    只能构造 ``FreeTokenStake``，因此历史付费金币（paid tokens）不能被误接入。
    This type deliberately accepts neither a wallet bucket nor a generic paid asset. The
    chance-activity boundary can construct only ``FreeTokenStake``, so legacy paid tokens
    cannot be routed here by accident.

    @param value 严格正的免费金币数量 / Strictly positive number of free tokens.
    """

    value: int
    """@brief 严格正的免费金币数量 / Strictly positive free-token amount."""

    def __post_init__(self) -> None:
        """@brief 校验免费押注不变量 / Validate free-stake invariants.

        @return None / None.
        @raise TypeError 数量不是严格整数时抛出 / Raised when the amount is not a strict integer.
        @raise ValueError 数量不为正时抛出 / Raised when the amount is not positive.
        """

        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("Free-token stake must be an integer")
        if self.value <= 0:
            raise ValueError("Free-token stake must be positive")

    def __int__(self) -> int:
        """@brief 返回原始免费金币数 / Return the raw free-token count.

        @return 严格正的免费金币数 / Strictly positive free-token count.
        """

        return self.value


@dataclass(frozen=True, slots=True, order=True)
class FreeTokenPayout:
    """@brief 随机活动胜利时的免费金币派彩 / Free-token payout on a chance win.

    @param value 严格正的总派彩金额（含本金） / Strictly positive gross payout including stake.
    """

    value: int
    """@brief 严格正的总派彩 / Strictly positive gross payout."""

    def __post_init__(self) -> None:
        """@brief 校验免费派彩不变量 / Validate free-payout invariants.

        @return None / None.
        @raise TypeError 数量不是严格整数时抛出 / Raised when the amount is not a strict integer.
        @raise ValueError 数量不为正时抛出 / Raised when the amount is not positive.
        """

        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("Free-token payout must be an integer")
        if self.value <= 0:
            raise ValueError("Free-token payout must be positive")

    def __int__(self) -> int:
        """@brief 返回原始免费金币数 / Return the raw free-token count.

        @return 严格正的免费金币数 / Strictly positive free-token count.
        """

        return self.value
