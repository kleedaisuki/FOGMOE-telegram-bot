"""@brief 群组小镇范围值对象 / Group-town scope value object."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class TownScope:
    """@brief 一个 Telegram 群组对应的一座小镇 / One Telegram group corresponding to one town.

    此类型故意不包含消息线程（message thread）标识；主题或话题不能创建第二座小镇。
    This type deliberately excludes a message-thread identity; a topic cannot create a second town.

    @param group_id 非零 Telegram 群组稳定标识 / Non-zero stable Telegram group identity.
    """

    group_id: int
    """@brief 群组稳定标识 / Stable group identity."""

    def __post_init__(self) -> None:
        """@brief 验证群组小镇范围 / Validate the group-town scope.

        @return None / None.
        @raise TypeError 群组标识不是严格整数时抛出 / Raised when group identity is not a strict integer.
        @raise ValueError 群组标识为零时抛出 / Raised when group identity is zero.
        @note Telegram 群组标识通常为负数，因而不能套用个人 ID 的正数规则。/
            Telegram group identifiers are commonly negative, so the personal-ID positivity rule does not apply.
        """

        if isinstance(self.group_id, bool) or not isinstance(self.group_id, int):
            raise TypeError("Town-scope group ID must be an integer")
        if self.group_id == 0:
            raise ValueError("Town-scope group ID cannot be zero")
