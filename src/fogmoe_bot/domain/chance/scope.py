"""@brief 随机活动个人与群组上下文 / Personal and group contexts for chance activities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


class ChanceScopeKind(StrEnum):
    """@brief 随机活动归属上下文种类 / Chance-activity ownership-context kind."""

    PERSONAL = "personal"
    """@brief 个人 RPG 世界中的私人活动 / Private activity in a personal RPG world."""

    GROUP = "group"
    """@brief 群组小镇中的共同活动 / Shared activity in a group town."""


@dataclass(frozen=True, slots=True)
class PersonalRoundScope:
    """@brief 个人 RPG 内的活动范围 / Chance-round scope inside a personal RPG.

    @param user_id 个人世界拥有者的稳定用户标识 / Stable user identity owning the personal world.
    """

    user_id: int
    """@brief 个人世界拥有者标识 / Personal-world owner identity."""

    def __post_init__(self) -> None:
        """@brief 校验个人范围 / Validate the personal scope.

        @return None / None.
        @raise ValueError 用户标识不为正时抛出 / Raised when the user identity is not positive.
        """

        if (
            isinstance(self.user_id, bool)
            or not isinstance(self.user_id, int)
            or self.user_id <= 0
        ):
            raise ValueError("Personal chance scope requires a positive user")

    @property
    def kind(self) -> ChanceScopeKind:
        """@brief 返回固定的个人范围类别 / Return the fixed personal scope kind.

        @return 个人范围类别 / Personal scope kind.
        """

        return ChanceScopeKind.PERSONAL


@dataclass(frozen=True, slots=True)
class GroupRoundScope:
    """@brief 群组小镇内的活动范围 / Chance-round scope inside a group town.

    @param group_id 群组小镇的稳定标识 / Stable identity of the group town.
    @param topic_id 可选的群组话题标识 / Optional group-topic identity.
    """

    group_id: int
    """@brief 群组小镇标识 / Group-town identity."""

    topic_id: int | None = None
    """@brief 可选话题标识 / Optional topic identity."""

    def __post_init__(self) -> None:
        """@brief 校验群组范围 / Validate the group scope.

        @return None / None.
        @raise ValueError 群组或话题标识非法时抛出 / Raised when a group or topic identity is invalid.
        """

        if (
            isinstance(self.group_id, bool)
            or not isinstance(self.group_id, int)
            or self.group_id == 0
        ):
            raise ValueError("Group chance scope requires a non-zero group")
        if self.topic_id is not None and (
            isinstance(self.topic_id, bool)
            or not isinstance(self.topic_id, int)
            or self.topic_id <= 0
        ):
            raise ValueError("Group chance topic must be positive when present")

    @property
    def kind(self) -> ChanceScopeKind:
        """@brief 返回固定的群组范围类别 / Return the fixed group scope kind.

        @return 群组范围类别 / Group scope kind.
        """

        return ChanceScopeKind.GROUP


RoundScope: TypeAlias = PersonalRoundScope | GroupRoundScope
"""@brief 显式的随机活动范围并集 / Explicit union of chance-round scopes."""
