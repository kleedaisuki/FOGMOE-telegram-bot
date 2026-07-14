"""@brief 个人 RPG 应用服务 / Personal-RPG application service."""

from __future__ import annotations

from fogmoe_bot.application.personal_rpg.models import (
    CraftPersonalRecipe,
    CreatePersonalCharacter,
    ExploreDaily,
    PersonalRpgResult,
)
from fogmoe_bot.application.personal_rpg.ports import PersonalRpgOperations
from fogmoe_bot.domain.world.scope import PersonalScope


PERSONAL_RPG_SERVICE_DATA_KEY = "personal_rpg.service"
"""@brief runtime capability 中个人 RPG 服务稳定键 / Stable personal-RPG service key in runtime capabilities."""


class PersonalRpgService:
    """@brief 编排严格私聊范围内的角色、探索和制作 / Orchestrate character, exploration, and crafting in a strictly private scope.

    Telegram 边界必须先确认消息来自私聊，再构造 ``PersonalScope``。服务只接收已经强类型化的
    个人范围，因而不提供任何 PVP、群组或话题入口。
    The Telegram boundary must first confirm that a message is private and then construct a
    ``PersonalScope``. The service receives only this strongly typed personal scope and therefore
    exposes no PVP, group, or topic entry point.

    @param operations 原子个人 RPG 持久化端口 / Atomic personal-RPG persistence port.
    """

    def __init__(self, *, operations: PersonalRpgOperations) -> None:
        """@brief 注入原子个人 RPG 操作端口 / Inject the atomic personal-RPG operations port.

        @param operations 原子个人 RPG 持久化端口 / Atomic personal-RPG persistence port.
        """

        self._operations = operations

    async def create_character(
        self,
        command: CreatePersonalCharacter,
    ) -> PersonalRpgResult:
        """@brief 创建一个个人角色 / Create one personal character.

        @param command 创建角色命令 / Character-creation command.
        @return 创建结果 / Creation result.
        """

        return await self._operations.create_character(command)

    async def explore_daily(self, command: ExploreDaily) -> PersonalRpgResult:
        """@brief 结算一次确定性每日探索 / Settle one deterministic daily exploration.

        @param command 每日探索命令 / Daily-exploration command.
        @return 探索结算结果 / Exploration settlement result.
        """

        return await self._operations.explore_daily(command)

    async def craft_recipe(self, command: CraftPersonalRecipe) -> PersonalRpgResult:
        """@brief 制作并收录一个收藏品 / Craft and record one collectible.

        @param command 制作配方命令 / Recipe-crafting command.
        @return 制作结算结果 / Crafting settlement result.
        """

        return await self._operations.craft_recipe(command)

    async def overview(self, scope: PersonalScope) -> PersonalRpgResult:
        """@brief 读取个人 RPG 概览 / Read personal-RPG overview.

        @param scope 仅限个人的范围 / Personal-only scope.
        @return 概览结果 / Overview result.
        @raise TypeError 调用方传入群组范围或裸 ID 时抛出 /
            Raised when caller passes a group scope or a bare ID.
        """

        if not isinstance(scope, PersonalScope):
            raise TypeError("Personal RPG overview must use PersonalScope")
        return await self._operations.overview(scope)
