"""@brief 跨领域个人身份范围值对象 / Cross-domain personal identity-scope value object."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class PersonalScope:
    """@brief 个人进度与个人资产使用的用户范围 / User scope for personal progression and personal assets.

    数字用户标识被封装在此类型内，不能作为 ``TownScope`` 或群组范围使用。
    The numeric user identity is enclosed by this type and cannot be used as a ``TownScope`` or group scope.

    @param user_id 正的 Telegram 用户稳定标识 / Positive stable Telegram user identity.
    """

    user_id: int
    """@brief 用户稳定标识 / Stable user identity."""

    def __post_init__(self) -> None:
        """@brief 验证个人范围 / Validate the personal scope.

        @return None / None.
        @raise TypeError 用户标识不是严格整数时抛出 / Raised when user identity is not a strict integer.
        @raise ValueError 用户标识不为正时抛出 / Raised when user identity is not positive.
        """

        if isinstance(self.user_id, bool) or not isinstance(self.user_id, int):
            raise TypeError("Personal-scope user ID must be an integer")
        if self.user_id <= 0:
            raise ValueError("Personal-scope user ID must be positive")
