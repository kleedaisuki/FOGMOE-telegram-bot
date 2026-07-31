"""@brief Economy 上下文引用的账户身份 / Account identity referenced by the Economy context."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class EconomyAccountId:
    """@brief Economy 中稳定的账户身份 / Stable account identity inside Economy.

    @param value Identity 上下文分配的正整数账户 ID / Positive account ID assigned by Identity.
    @note 该值对象只表达跨上下文引用，不复制 Identity 用户资料。/
        This value object expresses a cross-context reference without mirroring Identity profile data.
    """

    value: int
    """@brief 原始账户 ID / Raw account identifier."""

    def __post_init__(self) -> None:
        """@brief 验证账户身份 / Validate the account identity.

        @return None / None.
        @raise TypeError 值不是严格整数时抛出 / Raised when the value is not a strict integer.
        @raise ValueError 值不为正时抛出 / Raised when the value is not positive.
        """

        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise TypeError("Economy account ID must be an integer")
        if self.value <= 0:
            raise ValueError("Economy account ID must be positive")

    def __int__(self) -> int:
        """@brief 返回数据库边界使用的整数 / Return the integer used at persistence boundaries.

        @return 正整数账户 ID / Positive integral account identifier.
        """

        return self.value


__all__ = ["EconomyAccountId"]
