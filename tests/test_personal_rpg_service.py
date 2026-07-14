"""@brief 个人 RPG 应用服务测试 / Personal-RPG application-service tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from fogmoe_bot.application.personal_rpg.models import (
    CraftPersonalRecipe,
    CreatePersonalCharacter,
    ExploreDaily,
    PersonalRpgCode,
    PersonalRpgResult,
)
from fogmoe_bot.application.personal_rpg.service import PersonalRpgService
from fogmoe_bot.domain.personal_rpg.catalog import RecipeCode
from fogmoe_bot.domain.personal_rpg.exploration import ExplorationRoute
from fogmoe_bot.domain.town.scope import TownScope
from fogmoe_bot.domain.world.scope import PersonalScope


DAY = date(2026, 7, 14)
"""@brief 服务测试使用的稳定 UTC 业务日 / Stable UTC business day used by service tests."""

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
"""@brief 服务测试使用的稳定 UTC 时刻 / Stable UTC instant used by service tests."""

SCOPE = PersonalScope(42)
"""@brief 服务测试个人范围 / Service-test personal scope."""


class _Operations:
    """@brief 记录个人 RPG 服务调用的内存端口 / In-memory port recording personal-RPG service calls."""

    def __init__(self) -> None:
        """@brief 初始化命令记录 / Initialize command recordings.

        @return None / None.
        """

        self.creations: list[CreatePersonalCharacter] = []
        self.explorations: list[ExploreDaily] = []
        self.crafts: list[CraftPersonalRecipe] = []
        self.overviews: list[PersonalScope] = []

    async def create_character(
        self,
        command: CreatePersonalCharacter,
    ) -> PersonalRpgResult:
        """@brief 记录创建角色调用 / Record character-creation call.

        @param command 创建角色命令 / Character-creation command.
        @return 成功结果 / Successful result.
        """

        self.creations.append(command)
        return PersonalRpgResult(PersonalRpgCode.SUCCESS)

    async def explore_daily(self, command: ExploreDaily) -> PersonalRpgResult:
        """@brief 记录每日探索调用 / Record daily-exploration call.

        @param command 每日探索命令 / Daily-exploration command.
        @return 成功结果 / Successful result.
        """

        self.explorations.append(command)
        return PersonalRpgResult(PersonalRpgCode.SUCCESS)

    async def craft_recipe(self, command: CraftPersonalRecipe) -> PersonalRpgResult:
        """@brief 记录制作配方调用 / Record recipe-crafting call.

        @param command 制作配方命令 / Recipe-crafting command.
        @return 成功结果 / Successful result.
        """

        self.crafts.append(command)
        return PersonalRpgResult(PersonalRpgCode.SUCCESS)

    async def overview(self, scope: PersonalScope) -> PersonalRpgResult:
        """@brief 记录概览读取调用 / Record overview-read call.

        @param scope 个人范围 / Personal scope.
        @return 成功结果 / Successful result.
        """

        self.overviews.append(scope)
        return PersonalRpgResult(PersonalRpgCode.SUCCESS)


def test_service_routes_private_commands_and_rejects_group_scope_on_reads() -> None:
    """@brief 服务路由私聊命令并拒绝群组范围读取 / Service routes private commands and rejects group-scope reads.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行个人 RPG 服务路由场景 / Execute personal-RPG service routing scenario.

        @return None / None.
        """

        operations = _Operations()
        service = PersonalRpgService(operations=operations)
        creation = CreatePersonalCharacter(
            scope=SCOPE,
            name="可莉",
            created_at=NOW,
            idempotency_key="test:personal-rpg:create",
        )
        exploration = ExploreDaily(
            exploration_id=uuid4(),
            scope=SCOPE,
            day=DAY,
            route=ExplorationRoute.WOODLAND,
            explored_at=NOW,
            idempotency_key="test:personal-rpg:explore",
        )
        crafting = CraftPersonalRecipe(
            craft_id=uuid4(),
            scope=SCOPE,
            recipe_code=RecipeCode.HERBAL_LANTERN,
            crafted_at=NOW,
            idempotency_key="test:personal-rpg:craft",
        )

        assert (
            await service.create_character(creation)
        ).code is PersonalRpgCode.SUCCESS
        assert (
            await service.explore_daily(exploration)
        ).code is PersonalRpgCode.SUCCESS
        assert (await service.craft_recipe(crafting)).code is PersonalRpgCode.SUCCESS
        assert (await service.overview(SCOPE)).code is PersonalRpgCode.SUCCESS
        assert operations.creations == [creation]
        assert operations.explorations == [exploration]
        assert operations.crafts == [crafting]
        assert operations.overviews == [SCOPE]
        with pytest.raises(TypeError, match="PersonalScope"):
            await service.overview(TownScope(-100_123_456))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="PersonalScope"):
            await service.overview(42)  # type: ignore[arg-type]

    asyncio.run(scenario())


def test_commands_reject_group_scope_before_reaching_operations() -> None:
    """@brief 命令在端口前拒绝群组范围 / Commands reject group scope before operations port.

    @return None / None.
    """

    with pytest.raises(TypeError, match="PersonalScope"):
        CreatePersonalCharacter(
            scope=TownScope(-100_123_456),  # type: ignore[arg-type]
            name="错误",
            created_at=NOW,
            idempotency_key="test:personal-rpg:group",
        )
