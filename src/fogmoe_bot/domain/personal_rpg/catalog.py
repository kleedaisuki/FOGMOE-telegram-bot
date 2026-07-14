"""@brief 个人 RPG 材料、配方与收藏图鉴 / Personal-RPG materials, recipes, and collection compendium."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from fogmoe_bot.domain.personal_rpg._validation import normalize_text


class MaterialKind(StrEnum):
    """@brief 可由每日探索采集的材料类别 / Material kinds gathered through daily exploration."""

    FIBER = "fiber"
    """@brief 纤维 / Fiber."""

    HERB = "herb"
    """@brief 药草 / Herb."""

    STONE = "stone"
    """@brief 石料 / Stone."""

    ORE = "ore"
    """@brief 矿石 / Ore."""

    SHELL = "shell"
    """@brief 贝壳 / Shell."""

    ALGAE = "algae"
    """@brief 海藻 / Algae."""


class CollectibleKind(StrEnum):
    """@brief 可由配方制作并记录到图鉴的收藏品 / Collectibles crafted by recipes and recorded in the compendium."""

    HERBAL_LANTERN = "herbal_lantern"
    """@brief 药草灯笼 / Herbal lantern."""

    RUNE_CHARM = "rune_charm"
    """@brief 符文护符 / Rune charm."""

    TIDAL_MOBILE = "tidal_mobile"
    """@brief 潮汐风铃 / Tidal mobile."""


class RecipeCode(StrEnum):
    """@brief 固定配方编码 / Stable recipe codes."""

    HERBAL_LANTERN = "herbal_lantern"
    """@brief 药草灯笼配方 / Herbal-lantern recipe."""

    RUNE_CHARM = "rune_charm"
    """@brief 符文护符配方 / Rune-charm recipe."""

    TIDAL_MOBILE = "tidal_mobile"
    """@brief 潮汐风铃配方 / Tidal-mobile recipe."""


def _normalize_quantities(
    quantities: Mapping[MaterialKind, int],
    *,
    field: str,
    allow_empty: bool,
) -> Mapping[MaterialKind, int]:
    """@brief 校验并冻结材料数量映射 / Validate and freeze a material-quantity mapping.

    @param quantities 原始材料数量映射 / Raw material-quantity mapping.
    @param field 错误信息中的字段名称 / Field name used in error messages.
    @param allow_empty 是否允许空映射 / Whether an empty mapping is permitted.
    @return 按材料编码稳定排序的不可变映射 / Immutable mapping stably ordered by material code.
    @raise TypeError 材料类别或数量类型非法时抛出 / Raised when a material kind or quantity type is invalid.
    @raise ValueError 数量为负、为零或映射为空时抛出 / Raised when a quantity is negative, zero, or the mapping is empty.
    """

    if not isinstance(quantities, Mapping):
        raise TypeError(f"{field} must be a mapping")
    normalized: dict[MaterialKind, int] = {}
    for kind, quantity in quantities.items():
        if not isinstance(kind, MaterialKind):
            raise TypeError(f"{field} keys must be MaterialKind values")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise TypeError(f"{field} quantities must be integers")
        if quantity <= 0:
            raise ValueError(f"{field} quantities must be positive")
        normalized[kind] = quantity
    if not normalized and not allow_empty:
        raise ValueError(f"{field} cannot be empty")
    return MappingProxyType(
        dict(sorted(normalized.items(), key=lambda item: item[0].value))
    )


@dataclass(frozen=True, slots=True)
class MaterialBundle:
    """@brief 一组严格正的材料数量 / A bundle of strictly positive material quantities.

    @param quantities 各材料的正数数量 / Positive quantity for each material.
    """

    quantities: Mapping[MaterialKind, int]
    """@brief 材料到正数数量的映射 / Mapping from material to positive quantity."""

    def __post_init__(self) -> None:
        """@brief 校验并冻结材料束 / Validate and freeze the material bundle.

        @return None / None.
        """

        object.__setattr__(
            self,
            "quantities",
            _normalize_quantities(
                self.quantities,
                field="Material bundle",
                allow_empty=False,
            ),
        )

    def amount_of(self, kind: MaterialKind) -> int:
        """@brief 获取指定材料数量 / Get the quantity of one material.

        @param kind 材料类别 / Material kind.
        @return 不存在时为零，否则为正数 / Zero when absent, otherwise a positive amount.
        @raise TypeError 材料类别非法时抛出 / Raised when the material kind is invalid.
        """

        if not isinstance(kind, MaterialKind):
            raise TypeError("Material lookup must use MaterialKind")
        return self.quantities.get(kind, 0)


@dataclass(frozen=True, slots=True)
class MaterialInventory:
    """@brief 个人角色持有的材料库存 / Material inventory held by a personal character.

    @param quantities 可为空的非零材料数量映射 / Possibly empty mapping of non-zero material quantities.
    """

    quantities: Mapping[MaterialKind, int] = MappingProxyType({})
    """@brief 材料到正数库存的映射 / Mapping from material to positive inventory quantity."""

    def __post_init__(self) -> None:
        """@brief 校验并冻结材料库存 / Validate and freeze the material inventory.

        @return None / None.
        """

        object.__setattr__(
            self,
            "quantities",
            _normalize_quantities(
                self.quantities,
                field="Material inventory",
                allow_empty=True,
            ),
        )

    def amount_of(self, kind: MaterialKind) -> int:
        """@brief 获取指定材料的库存 / Get inventory quantity for one material.

        @param kind 材料类别 / Material kind.
        @return 不存在时为零，否则为正数 / Zero when absent, otherwise a positive quantity.
        @raise TypeError 材料类别非法时抛出 / Raised when the material kind is invalid.
        """

        if not isinstance(kind, MaterialKind):
            raise TypeError("Material lookup must use MaterialKind")
        return self.quantities.get(kind, 0)

    def add(self, bundle: MaterialBundle) -> MaterialInventory:
        """@brief 将采集到的材料加入库存 / Add gathered materials to the inventory.

        @param bundle 待加入的非空材料束 / Non-empty material bundle to add.
        @return 更新后的材料库存 / Updated material inventory.
        @raise TypeError 参数不是材料束时抛出 / Raised when the argument is not a material bundle.
        """

        if not isinstance(bundle, MaterialBundle):
            raise TypeError("Inventory addition must use MaterialBundle")
        updated = dict(self.quantities)
        for kind, quantity in bundle.quantities.items():
            updated[kind] = updated.get(kind, 0) + quantity
        return MaterialInventory(updated)

    def covers(self, bundle: MaterialBundle) -> bool:
        """@brief 判断库存是否覆盖一个配方材料束 / Check whether inventory covers a recipe material bundle.

        @param bundle 待消耗的材料束 / Material bundle to consume.
        @return 每种材料均足够时为 True / True when every material quantity is sufficient.
        @raise TypeError 参数不是材料束时抛出 / Raised when the argument is not a material bundle.
        """

        if not isinstance(bundle, MaterialBundle):
            raise TypeError("Inventory coverage must use MaterialBundle")
        return all(
            self.amount_of(kind) >= quantity
            for kind, quantity in bundle.quantities.items()
        )

    def consume(self, bundle: MaterialBundle) -> MaterialInventory:
        """@brief 消耗配方所需材料 / Consume recipe-required materials.

        @param bundle 待消耗的材料束 / Material bundle to consume.
        @return 扣除材料后的库存 / Inventory after material consumption.
        @raise TypeError 参数不是材料束时抛出 / Raised when the argument is not a material bundle.
        @raise ValueError 库存不足时抛出 / Raised when inventory is insufficient.
        """

        if not isinstance(bundle, MaterialBundle):
            raise TypeError("Inventory consumption must use MaterialBundle")
        if not self.covers(bundle):
            raise ValueError("Material inventory is insufficient for this recipe")
        updated = dict(self.quantities)
        for kind, quantity in bundle.quantities.items():
            remaining = updated[kind] - quantity
            if remaining == 0:
                del updated[kind]
            else:
                updated[kind] = remaining
        return MaterialInventory(updated)


@dataclass(frozen=True, slots=True)
class CraftingRecipe:
    """@brief 一条固定且可审计的制作配方 / One fixed and auditable crafting recipe.

    @param code 稳定配方编码 / Stable recipe code.
    @param title 面向玩家的配方名称 / Player-facing recipe title.
    @param ingredients 待消耗材料 / Materials to consume.
    @param output 制作完成后加入图鉴的收藏品 / Collectible recorded when crafting succeeds.
    """

    code: RecipeCode
    """@brief 稳定配方编码 / Stable recipe code."""

    title: str
    """@brief 面向玩家的配方名称 / Player-facing recipe title."""

    ingredients: MaterialBundle
    """@brief 所需材料 / Required materials."""

    output: CollectibleKind
    """@brief 图鉴产物 / Compendium output."""

    def __post_init__(self) -> None:
        """@brief 验证配方不变量 / Validate recipe invariants.

        @return None / None.
        @raise TypeError 配方编码、材料或产物类型非法时抛出 / Raised when code, ingredients, or output has an invalid type.
        """

        if not isinstance(self.code, RecipeCode):
            raise TypeError("Crafting recipe code must be RecipeCode")
        if not isinstance(self.ingredients, MaterialBundle):
            raise TypeError("Crafting recipe ingredients must be MaterialBundle")
        if not isinstance(self.output, CollectibleKind):
            raise TypeError("Crafting recipe output must be CollectibleKind")
        object.__setattr__(
            self,
            "title",
            normalize_text(
                self.title,
                field="Crafting recipe title",
                minimum_length=1,
                maximum_length=120,
            ),
        )


@dataclass(frozen=True, slots=True)
class CollectionCompendium:
    """@brief 已制作收藏品的不可重复图鉴 / Non-duplicating compendium of crafted collectibles.

    @param discovered 已发现收藏品集合 / Set of discovered collectibles.
    """

    discovered: frozenset[CollectibleKind] = frozenset()
    """@brief 已发现收藏品 / Discovered collectibles."""

    def __post_init__(self) -> None:
        """@brief 验证图鉴集合类型 / Validate compendium set types.

        @return None / None.
        @raise TypeError 图鉴不是不可变集合或包含非法类别时抛出 /
            Raised when the compendium is not an immutable set or contains an invalid kind.
        """

        if not isinstance(self.discovered, frozenset):
            raise TypeError("Compendium discoveries must be a frozenset")
        if not all(isinstance(kind, CollectibleKind) for kind in self.discovered):
            raise TypeError("Compendium discoveries must be CollectibleKind values")

    @property
    def completed_count(self) -> int:
        """@brief 返回已完成图鉴条目数 / Return the number of completed compendium entries.

        @return 已发现收藏品数量 / Number of discovered collectibles.
        """

        return len(self.discovered)

    @property
    def total_count(self) -> int:
        """@brief 返回本规则版本图鉴总条目数 / Return total entries in this ruleset version.

        @return 固定图鉴条目数量 / Fixed number of compendium entries.
        """

        return len(CollectibleKind)

    def contains(self, collectible: CollectibleKind) -> bool:
        """@brief 判断图鉴是否已收录一个收藏品 / Check whether a collectible is already recorded.

        @param collectible 待检查收藏品 / Collectible to inspect.
        @return 已收录时为 True / True when already discovered.
        @raise TypeError 收藏品类型非法时抛出 / Raised when the collectible type is invalid.
        """

        if not isinstance(collectible, CollectibleKind):
            raise TypeError("Compendium lookup must use CollectibleKind")
        return collectible in self.discovered

    def record(self, collectible: CollectibleKind) -> CollectionCompendium:
        """@brief 将新制作收藏品记录到图鉴 / Record a newly crafted collectible in the compendium.

        @param collectible 待记录收藏品 / Collectible to record.
        @return 增加该条目的新图鉴 / New compendium containing the entry.
        @raise TypeError 收藏品类型非法时抛出 / Raised when the collectible type is invalid.
        @raise ValueError 收藏品已经收录时抛出 / Raised when the collectible is already discovered.
        """

        if not isinstance(collectible, CollectibleKind):
            raise TypeError("Compendium recording must use CollectibleKind")
        if collectible in self.discovered:
            raise ValueError("Collectible is already recorded in the compendium")
        return CollectionCompendium(self.discovered | {collectible})


HERBAL_LANTERN_RECIPE: Final[CraftingRecipe] = CraftingRecipe(
    code=RecipeCode.HERBAL_LANTERN,
    title="药草灯笼",
    ingredients=MaterialBundle({MaterialKind.FIBER: 2, MaterialKind.HERB: 2}),
    output=CollectibleKind.HERBAL_LANTERN,
)
"""@brief 药草灯笼的固定配方 / Fixed recipe for the herbal lantern."""

RUNE_CHARM_RECIPE: Final[CraftingRecipe] = CraftingRecipe(
    code=RecipeCode.RUNE_CHARM,
    title="符文护符",
    ingredients=MaterialBundle({MaterialKind.STONE: 2, MaterialKind.ORE: 2}),
    output=CollectibleKind.RUNE_CHARM,
)
"""@brief 符文护符的固定配方 / Fixed recipe for the rune charm."""

TIDAL_MOBILE_RECIPE: Final[CraftingRecipe] = CraftingRecipe(
    code=RecipeCode.TIDAL_MOBILE,
    title="潮汐风铃",
    ingredients=MaterialBundle({MaterialKind.SHELL: 2, MaterialKind.ALGAE: 2}),
    output=CollectibleKind.TIDAL_MOBILE,
)
"""@brief 潮汐风铃的固定配方 / Fixed recipe for the tidal mobile."""

RECIPES: Final[Mapping[RecipeCode, CraftingRecipe]] = MappingProxyType(
    {
        HERBAL_LANTERN_RECIPE.code: HERBAL_LANTERN_RECIPE,
        RUNE_CHARM_RECIPE.code: RUNE_CHARM_RECIPE,
        TIDAL_MOBILE_RECIPE.code: TIDAL_MOBILE_RECIPE,
    }
)
"""@brief 配方编码到不可变配方的索引 / Immutable index from recipe code to recipe."""


def recipe_for(code: RecipeCode) -> CraftingRecipe:
    """@brief 读取一个固定制作配方 / Load one fixed crafting recipe.

    @param code 稳定配方编码 / Stable recipe code.
    @return 对应固定配方 / Corresponding fixed recipe.
    @raise TypeError 配方编码类型非法时抛出 / Raised when the recipe-code type is invalid.
    """

    if not isinstance(code, RecipeCode):
        raise TypeError("Recipe lookup must use RecipeCode")
    return RECIPES[code]
