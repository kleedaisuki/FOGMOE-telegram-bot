"""@brief 群组小镇应用服务测试 / Group-town application-service tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from fogmoe_bot.application.town.models import (
    CompleteTownProject,
    ContributeToTown,
    CreateTownProject,
    EnsureTown,
    TownCode,
    TownResult,
)
from fogmoe_bot.application.town.service import TownService
from fogmoe_bot.domain.banking.money import TokenAmount
from fogmoe_bot.domain.town.models import TownProjectKind
from fogmoe_bot.domain.town.scope import TownScope
from fogmoe_bot.domain.world.scope import PersonalScope


class _Operations:
    """@brief 记录小镇服务调用的内存端口 / In-memory port recording town-service calls."""

    def __init__(self) -> None:
        """@brief 初始化命令记录 / Initialize command recordings.

        @return None / None.
        """

        self.ensured: list[EnsureTown] = []
        self.projects: list[CreateTownProject] = []
        self.contributions: list[ContributeToTown] = []
        self.completions: list[CompleteTownProject] = []
        self.overviews: list[TownScope] = []

    async def ensure_town(self, command: EnsureTown) -> TownResult:
        """@brief 记录小镇创建读取 / Record town ensure.

        @param command 小镇读取或创建命令 / Town ensure command.
        @return 成功结果 / Successful result.
        """

        self.ensured.append(command)
        return TownResult(TownCode.SUCCESS)

    async def create_project(self, command: CreateTownProject) -> TownResult:
        """@brief 记录项目提议 / Record project proposal.

        @param command 项目提议命令 / Project-proposal command.
        @return 成功结果 / Successful result.
        """

        self.projects.append(command)
        return TownResult(TownCode.SUCCESS)

    async def contribute(self, command: ContributeToTown) -> TownResult:
        """@brief 记录小镇贡献 / Record town contribution.

        @param command 小镇贡献命令 / Town-contribution command.
        @return 成功结果 / Successful result.
        """

        self.contributions.append(command)
        return TownResult(TownCode.SUCCESS)

    async def complete_project(self, command: CompleteTownProject) -> TownResult:
        """@brief 记录项目结算 / Record project completion.

        @param command 项目结算命令 / Project-completion command.
        @return 成功结果 / Successful result.
        """

        self.completions.append(command)
        return TownResult(TownCode.SUCCESS)

    async def overview(self, town: TownScope) -> TownResult:
        """@brief 记录小镇概览读取 / Record town-overview read.

        @param town 群组小镇范围 / Group-town scope.
        @return 成功结果 / Successful result.
        """

        self.overviews.append(town)
        return TownResult(TownCode.SUCCESS)


class _Authorization:
    """@brief 可配置的小镇成员和治理授权替身 / Configurable town-membership and governance authorization double."""

    def __init__(self, *, contribute: bool, manage: bool) -> None:
        """@brief 设置预设授权结果 / Set preset authorization results.

        @param contribute 是否允许贡献 / Whether contributions are allowed.
        @param manage 是否允许治理 / Whether management is allowed.
        """

        self.contribute = contribute
        self.manage = manage
        self.contribution_checks: list[tuple[PersonalScope, TownScope]] = []
        self.management_checks: list[tuple[PersonalScope, TownScope]] = []

    async def may_contribute(
        self,
        *,
        actor: PersonalScope,
        town: TownScope,
    ) -> bool:
        """@brief 记录并返回贡献授权 / Record and return contribution authorization.

        @param actor 个人范围 / Personal scope.
        @param town 群组小镇范围 / Group-town scope.
        @return 预设贡献授权 / Preset contribution authorization.
        """

        self.contribution_checks.append((actor, town))
        return self.contribute

    async def may_manage(
        self,
        *,
        actor: PersonalScope,
        town: TownScope,
    ) -> bool:
        """@brief 记录并返回治理授权 / Record and return governance authorization.

        @param actor 个人范围 / Personal scope.
        @param town 群组小镇范围 / Group-town scope.
        @return 预设治理授权 / Preset governance authorization.
        """

        self.management_checks.append((actor, town))
        return self.manage


def test_service_gates_member_and_governance_writes_without_losing_scope_types() -> None:
    """@brief 服务先授权再写入，且始终传递显式小镇与个人范围 / Service authorizes before writes while preserving explicit town and personal scopes.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行群组小镇授权场景 / Execute group-town authorization scenario.

        @return None / None.
        """

        now = datetime.now(UTC)
        town = TownScope(-100_123_456_789)
        player = PersonalScope(42)
        project_id = uuid4()
        ensure = EnsureTown(town, "雾萌小镇", now, "test:town:ensure")
        project = CreateTownProject(
            town=town,
            proposer=player,
            project_id=project_id,
            kind=TownProjectKind.GARDEN,
            title="月光花园",
            required_amount=TokenAmount(8),
            created_at=now,
            idempotency_key="test:town:project",
        )
        contribution = ContributeToTown(
            town=town,
            contributor=player,
            contribution_id=uuid4(),
            amount=TokenAmount(3),
            requested_at=now,
            idempotency_key="test:town:contribution",
            project_id=project_id,
        )
        completion = CompleteTownProject(
            town=town,
            operator=player,
            project_id=project_id,
            completed_at=now,
            idempotency_key="test:town:complete",
        )
        operations = _Operations()
        authorization = _Authorization(contribute=False, manage=False)
        service = TownService(operations=operations, authorization=authorization)

        assert (await service.ensure_town(ensure)).code is TownCode.SUCCESS
        assert (await service.create_project(project)).code is TownCode.FORBIDDEN
        assert (await service.contribute(contribution)).code is TownCode.FORBIDDEN
        assert (await service.complete_project(completion)).code is TownCode.FORBIDDEN
        assert operations.projects == []
        assert operations.contributions == []
        assert operations.completions == []
        assert authorization.management_checks == [(player, town), (player, town)]
        assert authorization.contribution_checks == [(player, town)]

        authorization.manage = True
        authorization.contribute = True
        assert (await service.create_project(project)).code is TownCode.SUCCESS
        assert (await service.contribute(contribution)).code is TownCode.SUCCESS
        assert (await service.complete_project(completion)).code is TownCode.SUCCESS
        assert (await service.overview(town)).code is TownCode.SUCCESS
        assert operations.ensured == [ensure]
        assert operations.projects == [project]
        assert operations.contributions == [contribution]
        assert operations.completions == [completion]
        assert operations.overviews == [town]

    asyncio.run(scenario())


def test_service_overview_rejects_bare_group_id_and_personal_scope() -> None:
    """@brief 概览接口拒绝裸群 ID 和个人范围 / Overview interface rejects bare group IDs and personal scopes.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行范围拒绝场景 / Execute scope-rejection scenario.

        @return None / None.
        """

        service = TownService(
            operations=_Operations(),
            authorization=_Authorization(contribute=True, manage=True),
        )
        with pytest.raises(TypeError, match="TownScope"):
            await service.overview(-100_123_456_789)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="TownScope"):
            await service.overview(PersonalScope(42))  # type: ignore[arg-type]

    asyncio.run(scenario())
