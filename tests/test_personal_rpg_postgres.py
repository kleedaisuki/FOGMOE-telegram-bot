"""@brief PostgreSQL 个人 RPG 适配器单元测试 / Unit tests for the PostgreSQL personal-RPG adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
import json
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.personal_rpg.models import (
    CraftPersonalRecipe,
    ExploreDaily,
    PersonalRpgCode,
    PersonalRpgResult,
)
from fogmoe_bot.domain.personal_rpg.catalog import (
    MaterialInventory,
    MaterialKind,
    RecipeCode,
)
from fogmoe_bot.domain.personal_rpg.character import PersonalCharacter
from fogmoe_bot.domain.personal_rpg.exploration import ExplorationRoute
from fogmoe_bot.domain.personal_rpg.profile import PersonalRpgProfile
from fogmoe_bot.domain.world.scope import PersonalScope
from fogmoe_bot.infrastructure.database import personal_rpg as postgres_module


DAY = date(2030, 1, 2)
"""@brief 适配器测试使用的稳定 UTC 业务日 / Stable UTC business day used by adapter tests."""

NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
"""@brief 适配器测试使用的稳定 UTC 时刻 / Stable UTC instant used by adapter tests."""

SCOPE = PersonalScope(42)
"""@brief 适配器测试个人范围 / Adapter-test personal scope."""


def _blank_profile() -> PersonalRpgProfile:
    """@brief 创建空白测试个人 RPG 档案 / Build blank test personal-RPG profile.

    @return 空白个人 RPG 档案 / Blank personal-RPG profile.
    """

    return PersonalRpgProfile(PersonalCharacter(scope=SCOPE, name="可莉"))


def test_complete_json_receipts_round_trip_exploration_and_crafting_results() -> None:
    """@brief 完整 JSON 回执可还原探索与制作结果 / Complete JSON receipts restore exploration and crafting results.

    @return None / None.
    """

    exploration_command = ExploreDaily(
        exploration_id=uuid4(),
        scope=SCOPE,
        day=DAY,
        route=ExplorationRoute.WOODLAND,
        explored_at=NOW,
        idempotency_key="test:personal-rpg:explore",
    )
    exploration = exploration_command.exploration()
    explored_profile = _blank_profile().apply_exploration(exploration)
    exploration_result = PersonalRpgResult(
        PersonalRpgCode.SUCCESS,
        profile=explored_profile,
        exploration=exploration,
    )

    craft_command = CraftPersonalRecipe(
        craft_id=uuid4(),
        scope=SCOPE,
        recipe_code=RecipeCode.HERBAL_LANTERN,
        crafted_at=NOW,
        idempotency_key="test:personal-rpg:craft",
    )
    recipe = craft_command.recipe()
    craft_profile = PersonalRpgProfile(
        PersonalCharacter(scope=SCOPE, name="可莉"),
        materials=MaterialInventory(recipe.ingredients.quantities),
    ).craft(recipe)
    craft_result = PersonalRpgResult(
        PersonalRpgCode.SUCCESS,
        profile=craft_profile,
        recipe=recipe,
    )

    for result in (exploration_result, craft_result):
        stored = json.loads(json.dumps(postgres_module._result_mapping(result)))
        replay = postgres_module._result_from_mapping(stored, replayed=True)

        assert replay.code is PersonalRpgCode.SUCCESS
        assert replay.replayed
        assert replay.profile == result.profile
        assert replay.exploration == result.exploration
        assert replay.recipe == result.recipe


def test_load_profile_locks_character_material_and_collection_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 写路径会锁定角色、材料和图鉴行 / Write path locks character, material, and collection rows.

    @param monkeypatch pytest monkeypatch fixture / Pytest monkeypatch fixture.
    @return None / None.
    """

    calls: list[str] = []
    """@brief 记录发出的查询文本 / Recorded emitted query text."""

    async def fetch_one(
        sql: str,
        params: object = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> tuple[object, ...] | None:
        """@brief 返回固定角色行 / Return fixed character row.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 固定角色行 / Fixed character row.
        """

        calls.append(sql)
        return (42, "可莉", 12, DAY, 1, 2)

    async def fetch_all(
        sql: str,
        params: object = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> list[tuple[object, ...]]:
        """@brief 按查询返回固定材料或图鉴行 / Return fixed material or collection rows by query.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 固定行集合 / Fixed row set.
        """

        calls.append(sql)
        if "personal_rpg.materials" in sql:
            return [("fiber", 2), ("herb", 1)]
        return [("herbal_lantern",)]

    monkeypatch.setattr(postgres_module.db_connection, "fetch_one", fetch_one)
    monkeypatch.setattr(postgres_module.db_connection, "fetch_all", fetch_all)

    profile = asyncio.run(
        postgres_module._load_profile(
            SCOPE,
            cast(AsyncConnection, object()),
            for_update=True,
        )
    )

    assert profile is not None
    assert profile.character.experience == 12
    assert profile.character.version == 1
    assert profile.version == 2
    assert profile.materials.amount_of(MaterialKind.FIBER) == 2
    assert profile.compendium.completed_count == 1
    assert len(calls) == 3
    assert all("FOR UPDATE" in sql for sql in calls)


def test_receipt_loader_rejects_changed_command_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 同一幂等键改变语义会被拒绝 / Changing semantics under one idempotency key is rejected.

    @param monkeypatch pytest monkeypatch fixture / Pytest monkeypatch fixture.
    @return None / None.
    """

    fingerprint = {"name": "可莉", "created_at": NOW.isoformat()}
    """@brief 预期命令语义指纹 / Expected command-semantics fingerprint."""
    stored_result = {
        "code": PersonalRpgCode.NOT_REGISTERED.value,
        "profile": None,
        "exploration": None,
        "recipe": None,
    }
    """@brief 最小 JSON 回执结果 / Minimal JSON receipt result."""

    async def fetch_one(
        sql: str,
        params: object = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> tuple[object, ...]:
        """@brief 返回固定回执行 / Return fixed receipt row.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 固定回执行 / Fixed receipt row.
        """

        return (
            "personal_rpg.create_character",
            42,
            json.dumps(fingerprint),
            json.dumps(stored_result),
        )

    monkeypatch.setattr(postgres_module.db_connection, "fetch_one", fetch_one)

    replay = asyncio.run(
        postgres_module._load_receipt(
            "test:receipt",
            "personal_rpg.create_character",
            actor_id=42,
            fingerprint=fingerprint,
            connection=cast(AsyncConnection, object()),
        )
    )
    assert replay == stored_result

    with pytest.raises(ValueError, match="changed command semantics"):
        asyncio.run(
            postgres_module._load_receipt(
                "test:receipt",
                "personal_rpg.create_character",
                actor_id=42,
                fingerprint={"name": "另一位可莉", "created_at": NOW.isoformat()},
                connection=cast(AsyncConnection, object()),
            )
        )
