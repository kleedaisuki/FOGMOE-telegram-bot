"""@brief 个人 RPG 进度聚合 / Personal-RPG progression aggregate."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from fogmoe_bot.domain.personal_rpg._validation import normalize_day
from fogmoe_bot.domain.personal_rpg.catalog import (
    CollectionCompendium,
    CraftingRecipe,
    MaterialInventory,
)
from fogmoe_bot.domain.personal_rpg.character import PersonalCharacter
from fogmoe_bot.domain.personal_rpg.exploration import DailyExploration
from fogmoe_bot.domain.world.scope import PersonalScope


@dataclass(frozen=True, slots=True)
class PersonalRpgProfile:
    """@brief 一名用户私聊范围内的全部 RPG 进度 / All RPG progress within one user's private scope.

    该聚合只引用 ``PersonalScope``；不接受群 ID、话题 ID 或任何第二玩家，因此不能表示
    PVP 或群组城镇状态。
    This aggregate references only ``PersonalScope``. It accepts no group ID, topic ID, or second
    player, and consequently cannot represent PVP or group-town state.

    @param character 个人成长角色 / Personal-growth character.
    @param materials 个人材料库存 / Personal material inventory.
    @param compendium 个人收藏图鉴 / Personal collection compendium.
    @param last_exploration_day 上一次确认探索的 UTC 业务日 / UTC business day of last confirmed exploration.
    @param version 聚合乐观并发版本 / Aggregate optimistic-concurrency version.
    """

    character: PersonalCharacter
    """@brief 个人成长角色 / Personal-growth character."""

    materials: MaterialInventory = MaterialInventory()
    """@brief 个人材料库存 / Personal material inventory."""

    compendium: CollectionCompendium = CollectionCompendium()
    """@brief 个人收藏图鉴 / Personal collection compendium."""

    last_exploration_day: date | None = None
    """@brief 上一次确认探索的 UTC 业务日 / UTC business day of last confirmed exploration."""

    version: int = 0
    """@brief 聚合乐观并发版本 / Aggregate optimistic-concurrency version."""

    def __post_init__(self) -> None:
        """@brief 验证个人进度聚合不变量 / Validate personal-progression aggregate invariants.

        @return None / None.
        @raise TypeError 角色、库存、图鉴、日期或版本类型非法时抛出 /
            Raised when character, inventory, compendium, day, or version type is invalid.
        @raise ValueError 版本为负时抛出 / Raised when version is negative.
        """

        if not isinstance(self.character, PersonalCharacter):
            raise TypeError("Personal RPG profile must contain PersonalCharacter")
        if not isinstance(self.materials, MaterialInventory):
            raise TypeError("Personal RPG profile must contain MaterialInventory")
        if not isinstance(self.compendium, CollectionCompendium):
            raise TypeError("Personal RPG profile must contain CollectionCompendium")
        if self.last_exploration_day is not None:
            object.__setattr__(
                self,
                "last_exploration_day",
                normalize_day(
                    self.last_exploration_day,
                    field="Personal RPG last exploration day",
                ),
            )
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Personal RPG profile version must be an integer")
        if self.version < 0:
            raise ValueError("Personal RPG profile version cannot be negative")

    @property
    def scope(self) -> PersonalScope:
        """@brief 返回唯一所属个人范围 / Return the unique owning personal scope.

        @return 角色所属个人范围 / Personal scope belonging to the character.
        """

        return self.character.scope

    def apply_exploration(self, exploration: DailyExploration) -> PersonalRpgProfile:
        """@brief 原子应用一次经过审计的每日探索 / Atomically apply one audited daily exploration.

        @param exploration 已验证且固定奖励的每日探索 / Verified daily exploration with fixed reward.
        @return 角色经验与材料已更新的新进度 / New progression with updated character experience and materials.
        @raise TypeError 探索类型非法时抛出 / Raised when exploration type is invalid.
        @raise ValueError 探索归属错误、审计失败或当天/旧日重复时抛出 /
            Raised when ownership is wrong, audit fails, or a same/older day is repeated.
        """

        if not isinstance(exploration, DailyExploration):
            raise TypeError("Personal RPG exploration must use DailyExploration")
        if exploration.scope != self.scope:
            raise ValueError(
                "Daily exploration scope does not match the personal RPG profile"
            )
        if not exploration.verify():
            raise ValueError("Daily exploration audit verification failed")
        if (
            self.last_exploration_day is not None
            and exploration.day <= self.last_exploration_day
        ):
            raise ValueError(
                "Personal RPG already has an exploration on this or a later day"
            )
        return replace(
            self,
            character=self.character.gain_experience(exploration.reward.experience),
            materials=self.materials.add(exploration.reward.materials),
            last_exploration_day=exploration.day,
            version=self.version + 1,
        )

    def craft(self, recipe: CraftingRecipe) -> PersonalRpgProfile:
        """@brief 消耗材料并把配方产物写入图鉴 / Consume materials and record recipe output in compendium.

        @param recipe 固定制作配方 / Fixed crafting recipe.
        @return 材料已消耗、图鉴已更新的新进度 / New progression with consumed materials and updated compendium.
        @raise TypeError 配方类型非法时抛出 / Raised when recipe type is invalid.
        @raise ValueError 材料不足或图鉴已收录产物时抛出 /
            Raised when materials are insufficient or the output is already discovered.
        """

        if not isinstance(recipe, CraftingRecipe):
            raise TypeError("Personal RPG crafting must use CraftingRecipe")
        if self.compendium.contains(recipe.output):
            raise ValueError("Collectible is already recorded in the compendium")
        return replace(
            self,
            materials=self.materials.consume(recipe.ingredients),
            compendium=self.compendium.record(recipe.output),
            version=self.version + 1,
        )
