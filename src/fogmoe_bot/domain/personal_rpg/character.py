"""@brief 仅限个人成长的 RPG 角色 / Personal-growth-only RPG character."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from fogmoe_bot.domain.personal_rpg._validation import normalize_text
from fogmoe_bot.domain.world.scope import PersonalScope


MAX_PERSONAL_LEVEL: Final[int] = 50
"""@brief 个人 RPG 的安全等级上限 / Safety level cap for the personal RPG."""


def experience_threshold(level: int) -> int:
    """@brief 返回离开当前等级所需的累计经验 / Return cumulative experience needed to leave a level.

    @param level 当前等级 / Current level.
    @return 升到下一等级的累计经验阈值 / Cumulative threshold for the next level.
    @raise TypeError 等级不是严格整数时抛出 / Raised when level is not a strict integer.
    @raise ValueError 等级不在支持区间时抛出 / Raised when level is outside the supported range.
    """

    if isinstance(level, bool) or not isinstance(level, int):
        raise TypeError("Personal RPG level must be an integer")
    if not 1 <= level <= MAX_PERSONAL_LEVEL:
        raise ValueError("Personal RPG level is outside the supported range")
    return 20 * level * (level + 1)


def level_from_experience(experience: int) -> int:
    """@brief 将累计经验映射为个人等级 / Map cumulative experience to a personal level.

    @param experience 非负累计经验 / Non-negative cumulative experience.
    @return ``[1, MAX_PERSONAL_LEVEL]`` 内等级 / Level in ``[1, MAX_PERSONAL_LEVEL]``.
    @raise TypeError 经验不是严格整数时抛出 / Raised when experience is not a strict integer.
    @raise ValueError 经验为负时抛出 / Raised when experience is negative.
    """

    if isinstance(experience, bool) or not isinstance(experience, int):
        raise TypeError("Personal RPG experience must be an integer")
    if experience < 0:
        raise ValueError("Personal RPG experience cannot be negative")
    for level in range(1, MAX_PERSONAL_LEVEL):
        if experience < experience_threshold(level):
            return level
    return MAX_PERSONAL_LEVEL


@dataclass(frozen=True, slots=True)
class PersonalCharacter:
    """@brief 只属于一个私聊个人范围的 RPG 角色 / RPG character belonging only to one private personal scope.

    角色不含对手、群组、挑战开关或伤害字段；这些字段属于旧 PVP RPG，刻意不进入此模型。
    This character has no opponent, group, challenge, or damage fields; those belong to the legacy
    PVP RPG and are deliberately excluded from this model.

    @param scope 角色所属个人范围 / Personal scope owning the character.
    @param name 面向玩家的角色名称 / Player-facing character name.
    @param experience 非负累计探索经验 / Non-negative cumulative exploration experience.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    """

    scope: PersonalScope
    """@brief 角色所属个人范围 / Owning personal scope."""

    name: str
    """@brief 面向玩家的角色名称 / Player-facing character name."""

    experience: int = 0
    """@brief 非负累计探索经验 / Non-negative cumulative exploration experience."""

    version: int = 0
    """@brief 乐观并发版本 / Optimistic-concurrency version."""

    def __post_init__(self) -> None:
        """@brief 验证个人角色不变量 / Validate personal-character invariants.

        @return None / None.
        @raise TypeError 范围、经验或版本类型非法时抛出 / Raised when scope, experience, or version types are invalid.
        @raise ValueError 经验或版本为负时抛出 / Raised when experience or version is negative.
        """

        if not isinstance(self.scope, PersonalScope):
            raise TypeError("Personal RPG character must use PersonalScope")
        if isinstance(self.experience, bool) or not isinstance(self.experience, int):
            raise TypeError("Personal RPG experience must be an integer")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Personal RPG character version must be an integer")
        if self.experience < 0 or self.version < 0:
            raise ValueError("Personal RPG experience and version cannot be negative")
        object.__setattr__(
            self,
            "name",
            normalize_text(
                self.name,
                field="Personal RPG character name",
                minimum_length=1,
                maximum_length=40,
            ),
        )

    @property
    def level(self) -> int:
        """@brief 由累计经验派生当前等级 / Derive the current level from cumulative experience.

        @return 当前个人等级 / Current personal level.
        """

        return level_from_experience(self.experience)

    @property
    def experience_progress(self) -> tuple[int, int]:
        """@brief 返回本等级经验进度 / Return experience progress within the current level.

        @return ``(本级已获经验, 本级所需经验)`` / ``(earned in level, experience needed in level)``.
        """

        current_level = self.level
        if current_level == MAX_PERSONAL_LEVEL:
            return 0, 0
        previous_threshold = (
            0 if current_level == 1 else experience_threshold(current_level - 1)
        )
        following_threshold = experience_threshold(current_level)
        return (
            self.experience - previous_threshold,
            following_threshold - previous_threshold,
        )

    def gain_experience(self, amount: int) -> PersonalCharacter:
        """@brief 记录确定性探索经验奖励 / Record deterministic exploration experience reward.

        @param amount 严格正的经验奖励 / Strictly positive experience reward.
        @return 经验和版本已更新的角色 / Character with updated experience and version.
        @raise TypeError 经验奖励不是严格整数时抛出 / Raised when the reward is not a strict integer.
        @raise ValueError 经验奖励不为正时抛出 / Raised when the reward is not positive.
        """

        if isinstance(amount, bool) or not isinstance(amount, int):
            raise TypeError("Personal RPG experience reward must be an integer")
        if amount <= 0:
            raise ValueError("Personal RPG experience reward must be positive")
        return replace(
            self,
            experience=self.experience + amount,
            version=self.version + 1,
        )
