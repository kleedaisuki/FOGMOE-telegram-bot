"""@brief 个人 RPG 纯领域模型测试 / Pure personal-RPG domain-model tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from fogmoe_bot.domain.personal_rpg.catalog import (
    CollectibleKind,
    MaterialBundle,
    MaterialInventory,
    MaterialKind,
    RecipeCode,
    recipe_for,
)
from fogmoe_bot.domain.personal_rpg.character import PersonalCharacter
from fogmoe_bot.domain.personal_rpg.exploration import (
    DailyExploration,
    ExplorationRoute,
    create_daily_exploration,
)
from fogmoe_bot.domain.personal_rpg.profile import PersonalRpgProfile
from fogmoe_bot.domain.town.scope import TownScope
from fogmoe_bot.domain.world.scope import PersonalScope


DAY = date(2026, 7, 14)
"""@brief 个人 RPG 测试使用的稳定 UTC 业务日 / Stable UTC business day used by personal-RPG tests."""

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
"""@brief 个人 RPG 测试使用的稳定 UTC 时刻 / Stable UTC instant used by personal-RPG tests."""

SCOPE = PersonalScope(42)
"""@brief 测试个人范围 / Test personal scope."""


def _profile(*, materials: MaterialInventory | None = None) -> PersonalRpgProfile:
    """@brief 创建测试个人 RPG 进度 / Build test personal-RPG progression.

    @param materials 可选测试材料库存 / Optional test material inventory.
    @return 初始个人 RPG 进度 / Initial personal-RPG progression.
    """

    return PersonalRpgProfile(
        character=PersonalCharacter(scope=SCOPE, name="可莉"),
        materials=MaterialInventory() if materials is None else materials,
    )


def _exploration(
    *,
    day: date = DAY,
    route: ExplorationRoute = ExplorationRoute.WOODLAND,
) -> DailyExploration:
    """@brief 创建测试每日探索 / Build test daily exploration.

    @param day 探索 UTC 业务日 / Exploration UTC business day.
    @param route 探索路线 / Exploration route.
    @return 可审计每日探索 / Auditable daily exploration.
    """

    return create_daily_exploration(
        exploration_id=uuid4(),
        scope=SCOPE,
        day=day,
        route=route,
        explored_at=NOW + timedelta(days=(day - DAY).days),
    )


def test_personal_rpg_rejects_group_scope_and_bare_user_id() -> None:
    """@brief 个人 RPG 只能使用 PersonalScope / Personal RPG accepts only PersonalScope.

    @return None / None.
    """

    with pytest.raises(TypeError, match="PersonalScope"):
        PersonalCharacter(scope=TownScope(-100_123_456), name="错误")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="PersonalScope"):
        PersonalCharacter(scope=42, name="错误")  # type: ignore[arg-type]


def test_daily_exploration_has_fixed_rewards_and_verifiable_audit_digest() -> None:
    """@brief 每日探索奖励固定且摘要可独立复验 / Daily rewards are fixed and digest is independently verifiable.

    @return None / None.
    """

    exploration_id = uuid4()
    first = create_daily_exploration(
        exploration_id=exploration_id,
        scope=SCOPE,
        day=DAY,
        route=ExplorationRoute.WOODLAND,
        explored_at=NOW,
    )
    second = create_daily_exploration(
        exploration_id=exploration_id,
        scope=SCOPE,
        day=DAY,
        route=ExplorationRoute.WOODLAND,
        explored_at=NOW,
    )

    assert first == second
    assert first.verify()
    assert first.reward.experience == 12
    assert first.reward.materials.amount_of(MaterialKind.FIBER) == 2
    assert first.reward.materials.amount_of(MaterialKind.HERB) == 1
    with pytest.raises(ValueError, match="fixed route reward"):
        replace(
            first,
            reward=replace(first.reward, experience=99),
        )


def test_profile_applies_one_daily_exploration_and_collects_materials() -> None:
    """@brief 进度一次性应用探索并采集材料 / Progress applies exploration once and gathers materials.

    @return None / None.
    """

    initial = _profile()
    exploration = _exploration()
    updated = initial.apply_exploration(exploration)

    assert updated.character.experience == exploration.reward.experience
    assert updated.materials.amount_of(MaterialKind.FIBER) == 2
    assert updated.materials.amount_of(MaterialKind.HERB) == 1
    assert updated.last_exploration_day == DAY
    with pytest.raises(ValueError, match="already has an exploration"):
        updated.apply_exploration(exploration)


def test_crafting_consumes_materials_and_records_a_nonduplicating_compendium_entry() -> (
    None
):
    """@brief 制作消耗材料并写入不可重复图鉴 / Crafting consumes materials and writes a nonduplicating compendium entry.

    @return None / None.
    """

    inventory = MaterialInventory(
        {
            MaterialKind.FIBER: 2,
            MaterialKind.HERB: 2,
        }
    )
    recipe = recipe_for(RecipeCode.HERBAL_LANTERN)
    crafted = _profile(materials=inventory).craft(recipe)

    assert crafted.materials.quantities == {}
    assert crafted.compendium.contains(CollectibleKind.HERBAL_LANTERN)
    assert crafted.compendium.completed_count == 1
    assert crafted.compendium.total_count == 3
    with pytest.raises(ValueError, match="already recorded"):
        crafted.craft(recipe)


def test_crafting_rejects_insufficient_materials() -> None:
    """@brief 材料不足不能制作 / Insufficient materials cannot craft.

    @return None / None.
    """

    profile = _profile(
        materials=MaterialInventory({MaterialKind.FIBER: 1, MaterialKind.HERB: 2})
    )
    with pytest.raises(ValueError, match="insufficient"):
        profile.craft(recipe_for(RecipeCode.HERBAL_LANTERN))


def test_material_bundle_requires_only_positive_material_quantities() -> None:
    """@brief 材料束拒绝零与负数量 / Material bundles reject zero and negative quantities.

    @return None / None.
    """

    with pytest.raises(ValueError, match="positive"):
        MaterialBundle({MaterialKind.ORE: 0})
    with pytest.raises(ValueError, match="positive"):
        MaterialBundle({MaterialKind.ORE: -1})
