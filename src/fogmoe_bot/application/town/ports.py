"""@brief 群组小镇应用端口 / Group-town application ports."""

from __future__ import annotations

from typing import Protocol

from fogmoe_bot.application.town.models import (
    CompleteTownProject,
    ContributeToTown,
    CreateTownProject,
    EnsureTown,
    TownResult,
)
from fogmoe_bot.domain.town.scope import TownScope
from fogmoe_bot.domain.world.scope import PersonalScope


class TownAuthorization(Protocol):
    """@brief 群组成员资格与小镇治理授权 / Group membership and town-governance authorization.

    @note 此授权是提前拒绝层；持久化端口仍必须在事务内重新验证成员身份或使用可审计的
        权限快照。/ This authorization is an early-rejection layer; the persistence port must
        still revalidate membership in its transaction or use an auditable authorization snapshot.
    """

    async def may_contribute(
        self,
        *,
        actor: PersonalScope,
        town: TownScope,
    ) -> bool:
        """@brief 判断成员能否向此小镇贡献 / Check whether a member may contribute to this town.

        @param actor 个人范围 / Personal scope.
        @param town 群组小镇范围 / Group-town scope.
        @return 可贡献时为 True / True when contribution is permitted.
        """

        ...

    async def may_manage(
        self,
        *,
        actor: PersonalScope,
        town: TownScope,
    ) -> bool:
        """@brief 判断成员能否管理此小镇项目 / Check whether a member may manage this town's projects.

        @param actor 个人范围 / Personal scope.
        @param town 群组小镇范围 / Group-town scope.
        @return 可管理时为 True / True when management is permitted.
        """

        ...


class TownOperations(Protocol):
    """@brief 保证小镇与银行原子性的持久化能力 / Persistence capability preserving town and bank atomicity.

    @note 实现必须以 ``TownScope`` 锁定一座小镇，并按稳定顺序锁定个人免费钱包、群组
        金库、项目和回执；不得通过裸 chat ID 或个人 RPG 状态替代这些边界。/
        Implementations must lock one town by ``TownScope`` and lock the personal free wallet,
        group treasury, project, and receipt in stable order; they must not replace these
        boundaries with a bare chat ID or personal-RPG state.
    """

    async def ensure_town(self, command: EnsureTown) -> TownResult:
        """@brief 读取或创建一个群组唯一小镇 / Read or create one unique town for a group.

        @param command 小镇读取或创建命令 / Town ensure command.
        @return 稳定小镇结果 / Stable town result.
        """

        ...

    async def create_project(self, command: CreateTownProject) -> TownResult:
        """@brief 原子保存一个已授权项目提议 / Atomically save one authorized project proposal.

        @param command 项目提议命令 / Project-proposal command.
        @return 稳定小镇结果 / Stable town result.
        """

        ...

    async def contribute(self, command: ContributeToTown) -> TownResult:
        """@brief 原子转入金库并保存贡献 / Atomically transfer into treasury and save a contribution.

        @param command 小镇贡献命令 / Town-contribution command.
        @return 稳定小镇结果 / Stable town result.
        """

        ...

    async def complete_project(self, command: CompleteTownProject) -> TownResult:
        """@brief 原子结算金库支出并完成项目 / Atomically settle treasury spend and complete a project.

        @param command 项目结算命令 / Project-completion command.
        @return 稳定小镇结果 / Stable town result.
        """

        ...

    async def overview(self, town: TownScope) -> TownResult:
        """@brief 读取一个群组小镇的概览 / Read the overview of one group town.

        @param town 群组小镇范围 / Group-town scope.
        @return 小镇存在时的快照，或 ``NOT_FOUND`` / Snapshot when town exists, or ``NOT_FOUND``.
        """

        ...
