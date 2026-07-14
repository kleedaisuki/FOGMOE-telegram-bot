"""@brief 群组小镇应用服务 / Group-town application service."""

from __future__ import annotations

from fogmoe_bot.application.town.models import (
    CompleteTownProject,
    ContributeToTown,
    CreateTownProject,
    EnsureTown,
    TownCode,
    TownResult,
)
from fogmoe_bot.application.town.ports import TownAuthorization, TownOperations
from fogmoe_bot.domain.town.scope import TownScope


TOWN_SERVICE_DATA_KEY = "town.service"
"""@brief runtime capability 中小镇服务稳定键 / Stable town-service key in runtime capabilities."""


class TownService:
    """@brief 编排群组小镇而不混入个人 RPG / Orchestrate group towns without mixing in personal RPG.

    @note 每个改变状态的命令都同时携带 ``TownScope`` 和 ``PersonalScope``；服务不会接收
        一个裸群组 ID，也不会根据个人 RPG 角色推断小镇。/ Every state-changing command
        carries both ``TownScope`` and ``PersonalScope``; the service accepts neither a bare group
        identifier nor a town inferred from a personal-RPG character.
    """

    def __init__(
        self,
        *,
        operations: TownOperations,
        authorization: TownAuthorization,
    ) -> None:
        """@brief 注入原子小镇端口和群组授权能力 / Inject atomic town operations and group authorization.

        @param operations 原子小镇与银行协作端口 / Atomic town-and-bank coordination port.
        @param authorization 群组成员资格与治理授权 / Group membership and governance authorization.
        """

        self._operations = operations
        self._authorization = authorization

    async def ensure_town(self, command: EnsureTown) -> TownResult:
        """@brief 读取或创建群组唯一小镇 / Read or create the unique town of a group.

        @param command 小镇读取或创建命令 / Town ensure command.
        @return 稳定小镇结果 / Stable town result.
        """

        return await self._operations.ensure_town(command)

    async def create_project(self, command: CreateTownProject) -> TownResult:
        """@brief 在授权后提议项目 / Propose a project after authorization.

        @param command 项目提议命令 / Project-proposal command.
        @return 项目提议结果 / Project-proposal result.
        """

        if not await self._authorization.may_manage(
            actor=command.proposer,
            town=command.town,
        ):
            return TownResult(TownCode.FORBIDDEN)
        return await self._operations.create_project(command)

    async def contribute(self, command: ContributeToTown) -> TownResult:
        """@brief 在成员授权后贡献免费金币 / Contribute free tokens after member authorization.

        @param command 小镇贡献命令 / Town-contribution command.
        @return 小镇贡献结果 / Town-contribution result.
        """

        if not await self._authorization.may_contribute(
            actor=command.contributor,
            town=command.town,
        ):
            return TownResult(TownCode.FORBIDDEN)
        return await self._operations.contribute(command)

    async def complete_project(self, command: CompleteTownProject) -> TownResult:
        """@brief 在治理授权后结算项目 / Settle a project after governance authorization.

        @param command 项目结算命令 / Project-completion command.
        @return 项目结算结果 / Project-completion result.
        """

        if not await self._authorization.may_manage(
            actor=command.operator,
            town=command.town,
        ):
            return TownResult(TownCode.FORBIDDEN)
        return await self._operations.complete_project(command)

    async def overview(self, town: TownScope) -> TownResult:
        """@brief 读取指定群组的小镇概览 / Read the town overview of a given group.

        @param town 群组小镇范围 / Group-town scope.
        @return 小镇概览结果 / Town-overview result.
        @raise TypeError 调用方传入裸 ID 或个人范围时抛出 /
            Raised when caller passes a bare ID or a personal scope.
        """

        if not isinstance(town, TownScope):
            raise TypeError("Town overview must use TownScope")
        return await self._operations.overview(town)
