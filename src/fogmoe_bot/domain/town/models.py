"""@brief 群组小镇、金库、项目与贡献领域模型 / Group-town, treasury, project, and contribution domain models."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from fogmoe_bot.domain.banking.money import TokenAmount
from fogmoe_bot.domain.town._validation import normalize_instant, normalize_text
from fogmoe_bot.domain.town.scope import TownScope
from fogmoe_bot.domain.world.scope import PersonalScope


class TownProjectKind(StrEnum):
    """@brief 小镇可建设项目类别 / Buildable group-town project categories."""

    COMMUNITY_HALL = "community_hall"
    """@brief 公共会馆 / Community hall."""

    WORKSHOP = "workshop"
    """@brief 协作工坊 / Cooperative workshop."""

    GARDEN = "garden"
    """@brief 社区花园 / Community garden."""

    OBSERVATORY = "observatory"
    """@brief 观测站 / Observatory."""


class TownProjectStatus(StrEnum):
    """@brief 小镇项目生命周期状态 / Group-town project lifecycle state."""

    FUNDING = "funding"
    """@brief 正在筹集小镇金库额度 / Collecting a town-treasury allocation."""

    READY = "ready"
    """@brief 已足额，可由管理员结算建成 / Fully funded and ready for administrator settlement."""

    COMPLETED = "completed"
    """@brief 已通过账本结算并建成 / Settled through the ledger and built."""


@dataclass(frozen=True, slots=True)
class TownTreasury:
    """@brief 与银行群组账户同步的小镇金库摘要 / Town-treasury summary synchronized with the bank group account.

    ``balance`` 是已确认到账但尚未由已完成项目结算的免费金币；``reserved`` 是已承诺给
    项目的子集。因此可自由分配额度为 ``balance - reserved``。
    ``balance`` is confirmed free-token funding not yet settled by completed projects; ``reserved``
    is the subset committed to projects. The freely allocatable amount is therefore
    ``balance - reserved``.

    @param balance 已确认的金库余额 / Confirmed treasury balance.
    @param reserved 已为项目保留的余额 / Balance reserved for projects.
    @param lifetime_contributed 历史确认入账总额 / Lifetime confirmed inflow.
    @param lifetime_settled 历史已结算项目总额 / Lifetime project settlement total.
    @param contribution_count 已确认贡献次数 / Confirmed contribution count.
    """

    balance: int = 0
    """@brief 已确认金库余额 / Confirmed treasury balance."""

    reserved: int = 0
    """@brief 已承诺项目的余额 / Project-committed balance."""

    lifetime_contributed: int = 0
    """@brief 历史确认入账总额 / Lifetime confirmed inflow."""

    lifetime_settled: int = 0
    """@brief 历史项目结算总额 / Lifetime project settlement total."""

    contribution_count: int = 0
    """@brief 历史确认贡献次数 / Lifetime confirmed contribution count."""

    def __post_init__(self) -> None:
        """@brief 验证金库守恒与非负不变量 / Validate treasury conservation and non-negative invariants.

        @return None / None.
        @raise TypeError 金额或次数不是严格整数时抛出 / Raised when an amount or count is not a strict integer.
        @raise ValueError 金额、保留关系或守恒关系非法时抛出 /
            Raised when amounts, reservation relation, or conservation relation is invalid.
        """

        values = (
            self.balance,
            self.reserved,
            self.lifetime_contributed,
            self.lifetime_settled,
            self.contribution_count,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("Town treasury values must be integers")
        if any(value < 0 for value in values):
            raise ValueError("Town treasury values cannot be negative")
        if self.reserved > self.balance:
            raise ValueError("Town treasury reservation cannot exceed balance")
        if self.lifetime_contributed != self.balance + self.lifetime_settled:
            raise ValueError("Town treasury inflow must equal balance plus settlements")

    @property
    def available_balance(self) -> int:
        """@brief 返回未被项目承诺的余额 / Return balance not committed to a project.

        @return 可自由分配的非负余额 / Freely allocatable non-negative balance.
        """

        return self.balance - self.reserved

    def credit(self, amount: TokenAmount) -> TownTreasury:
        """@brief 记录一笔已由银行确认的贡献 / Record one contribution already confirmed by the bank.

        @param amount 已确认到账的正数金额 / Positive amount confirmed as credited.
        @return 更新后的金库摘要 / Updated treasury summary.
        @raise TypeError 金额不是 ``TokenAmount`` 时抛出 / Raised when amount is not a ``TokenAmount``.
        """

        if not isinstance(amount, TokenAmount):
            raise TypeError("Town treasury credit must use TokenAmount")
        return replace(
            self,
            balance=self.balance + amount.value,
            lifetime_contributed=self.lifetime_contributed + amount.value,
            contribution_count=self.contribution_count + 1,
        )

    def reserve(self, amount: TokenAmount) -> TownTreasury:
        """@brief 将可用金库余额承诺给一个项目 / Commit available treasury balance to a project.

        @param amount 待承诺的正数金额 / Positive amount to reserve.
        @return 更新后的金库摘要 / Updated treasury summary.
        @raise TypeError 金额不是 ``TokenAmount`` 时抛出 / Raised when amount is not a ``TokenAmount``.
        @raise ValueError 可用余额不足时抛出 / Raised when available balance is insufficient.
        """

        if not isinstance(amount, TokenAmount):
            raise TypeError("Town treasury reservation must use TokenAmount")
        if amount.value > self.available_balance:
            raise ValueError("Town treasury has insufficient available balance")
        return replace(self, reserved=self.reserved + amount.value)

    def settle_reservation(self, amount: TokenAmount) -> TownTreasury:
        """@brief 在银行账本结算后消耗项目保留额度 / Consume a project reservation after bank-ledger settlement.

        @param amount 已由银行账本结算的正数金额 / Positive amount settled by the bank ledger.
        @return 更新后的金库摘要 / Updated treasury summary.
        @raise TypeError 金额不是 ``TokenAmount`` 时抛出 / Raised when amount is not a ``TokenAmount``.
        @raise ValueError 结算金额超过保留或余额时抛出 /
            Raised when settlement exceeds the reservation or balance.
        """

        if not isinstance(amount, TokenAmount):
            raise TypeError("Town treasury settlement must use TokenAmount")
        if amount.value > self.reserved or amount.value > self.balance:
            raise ValueError("Town treasury settlement exceeds the reserved balance")
        return replace(
            self,
            balance=self.balance - amount.value,
            reserved=self.reserved - amount.value,
            lifetime_settled=self.lifetime_settled + amount.value,
        )


@dataclass(frozen=True, slots=True)
class TownProject:
    """@brief 一项由群组共同出资的小镇建设项目 / One group-funded town construction project.

    @param project_id 项目稳定标识 / Stable project identity.
    @param kind 项目类别 / Project category.
    @param title 面向成员的项目名称 / Member-facing project name.
    @param required_amount 完成项目所需免费金币 / Free tokens required to complete the project.
    @param created_by 提议项目的个人范围 / Personal scope proposing the project.
    @param created_at 提议时刻 / Proposal instant.
    @param prosperity_reward 建成时增加的小镇繁荣度 / Town prosperity added on completion.
    @param funded_amount 已保留给此项目的金币 / Tokens already reserved for this project.
    @param status 项目当前状态 / Current project status.
    @param completed_at 可选建成时刻 / Optional completion instant.
    @param settlement_ledger_entry_id 可选银行结算分录 / Optional bank settlement ledger entry.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    """

    project_id: UUID
    """@brief 项目稳定标识 / Stable project identity."""

    kind: TownProjectKind
    """@brief 项目类别 / Project category."""

    title: str
    """@brief 项目名称 / Project name."""

    required_amount: TokenAmount
    """@brief 所需正数金币 / Required positive token amount."""

    created_by: PersonalScope
    """@brief 提议者个人范围 / Proposer personal scope."""

    created_at: datetime
    """@brief 提议时刻 / Proposal instant."""

    prosperity_reward: int = 1
    """@brief 建成繁荣度奖励 / Completion prosperity reward."""

    funded_amount: int = 0
    """@brief 已保留资金 / Reserved funding."""

    status: TownProjectStatus = TownProjectStatus.FUNDING
    """@brief 项目状态 / Project status."""

    completed_at: datetime | None = None
    """@brief 可选建成时刻 / Optional completion instant."""

    settlement_ledger_entry_id: UUID | None = None
    """@brief 可选银行结算分录 / Optional bank settlement ledger entry."""

    version: int = 0
    """@brief 乐观并发版本 / Optimistic-concurrency version."""

    def __post_init__(self) -> None:
        """@brief 验证项目生命周期不变量 / Validate project lifecycle invariants.

        @return None / None.
        @raise TypeError 标识、范围、金额、枚举或数字类型非法时抛出 /
            Raised when identity, scope, amount, enum, or numeric types are invalid.
        @raise ValueError 项目金额、时间或状态字段不一致时抛出 /
            Raised when project amount, time, or state fields are inconsistent.
        """

        if not isinstance(self.project_id, UUID):
            raise TypeError("Town project ID must be a UUID")
        if not isinstance(self.kind, TownProjectKind):
            raise TypeError("Town project kind must be a TownProjectKind")
        if not isinstance(self.required_amount, TokenAmount):
            raise TypeError("Town project required amount must be TokenAmount")
        if not isinstance(self.created_by, PersonalScope):
            raise TypeError("Town project creator must be a PersonalScope")
        if isinstance(self.prosperity_reward, bool) or not isinstance(
            self.prosperity_reward, int
        ):
            raise TypeError("Town project prosperity reward must be an integer")
        if self.prosperity_reward <= 0:
            raise ValueError("Town project prosperity reward must be positive")
        if isinstance(self.funded_amount, bool) or not isinstance(self.funded_amount, int):
            raise TypeError("Town project funded amount must be an integer")
        if not 0 <= self.funded_amount <= self.required_amount.value:
            raise ValueError("Town project funding must be within its required amount")
        if not isinstance(self.status, TownProjectStatus):
            raise TypeError("Town project status must be a TownProjectStatus")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Town project version must be an integer")
        if self.version < 0:
            raise ValueError("Town project version cannot be negative")

        created_at = normalize_instant(self.created_at, field="Town project creation time")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(
            self,
            "title",
            normalize_text(
                self.title,
                field="Town project title",
                minimum_length=1,
                maximum_length=120,
            ),
        )

        if self.status is TownProjectStatus.FUNDING:
            if self.funded_amount >= self.required_amount.value:
                raise ValueError("Funding projects must remain below their target")
            if self.completed_at is not None or self.settlement_ledger_entry_id is not None:
                raise ValueError("Funding projects cannot have completion settlement data")
            return
        if self.status is TownProjectStatus.READY:
            if self.funded_amount != self.required_amount.value:
                raise ValueError("Ready projects must be fully funded")
            if self.completed_at is not None or self.settlement_ledger_entry_id is not None:
                raise ValueError("Ready projects cannot have completion settlement data")
            return
        if self.status is TownProjectStatus.COMPLETED:
            if self.funded_amount != self.required_amount.value:
                raise ValueError("Completed projects must remain fully funded")
            if self.completed_at is None or self.settlement_ledger_entry_id is None:
                raise ValueError("Completed projects need settlement data")
            if not isinstance(self.settlement_ledger_entry_id, UUID):
                raise TypeError("Town project settlement ID must be a UUID")
            completed_at = normalize_instant(
                self.completed_at,
                field="Town project completion time",
            )
            if completed_at < created_at:
                raise ValueError("Town project completion cannot precede creation")
            object.__setattr__(self, "completed_at", completed_at)
            return
        raise AssertionError("Unhandled town project status")

    @property
    def remaining_amount(self) -> int:
        """@brief 返回尚需保留的金币数 / Return tokens still needing reservation.

        @return 非负剩余金币数 / Non-negative remaining token count.
        """

        return self.required_amount.value - self.funded_amount

    def fund(self, amount: TokenAmount) -> TownProject:
        """@brief 为筹资项目保留贡献额度 / Reserve a contribution for a funding project.

        @param amount 待保留的正数金币 / Positive token amount to reserve.
        @return 新的项目状态 / New project state.
        @raise TypeError 金额不是 ``TokenAmount`` 时抛出 / Raised when amount is not a ``TokenAmount``.
        @raise ValueError 项目不可继续筹资或贡献会超额时抛出 /
            Raised when the project cannot accept funding or the contribution would overfund it.
        """

        if not isinstance(amount, TokenAmount):
            raise TypeError("Town project funding must use TokenAmount")
        if self.status is not TownProjectStatus.FUNDING:
            raise ValueError("Only funding town projects can accept contributions")
        if amount.value > self.remaining_amount:
            raise ValueError("Town project contribution would overfund the project")
        funded_amount = self.funded_amount + amount.value
        status = (
            TownProjectStatus.READY
            if funded_amount == self.required_amount.value
            else TownProjectStatus.FUNDING
        )
        return replace(
            self,
            funded_amount=funded_amount,
            status=status,
            version=self.version + 1,
        )

    def complete(
        self,
        *,
        completed_at: datetime,
        settlement_ledger_entry_id: UUID,
    ) -> TownProject:
        """@brief 记录已由银行账本结算的项目建成 / Record project completion settled by the bank ledger.

        @param completed_at 建成时刻 / Completion instant.
        @param settlement_ledger_entry_id 金库支出的银行分录 / Bank ledger entry for the treasury spend.
        @return 已建成项目 / Completed project.
        @raise TypeError 结算分录标识非法时抛出 / Raised when settlement ledger identity is invalid.
        @raise ValueError 项目尚未足额时抛出 / Raised when the project is not ready.
        """

        if self.status is not TownProjectStatus.READY:
            raise ValueError("Only ready town projects can be completed")
        if not isinstance(settlement_ledger_entry_id, UUID):
            raise TypeError("Town project settlement ID must be a UUID")
        return replace(
            self,
            status=TownProjectStatus.COMPLETED,
            completed_at=completed_at,
            settlement_ledger_entry_id=settlement_ledger_entry_id,
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True)
class TownContribution:
    """@brief 已由银行账本确认的一次群组贡献 / One group contribution confirmed by the bank ledger.

    @param contribution_id 贡献稳定标识 / Stable contribution identity.
    @param town 目标群组小镇范围 / Target group-town scope.
    @param contributor 贡献者个人范围 / Contributor personal scope.
    @param amount 已转入金库的正数免费金币 / Positive free tokens transferred into treasury.
    @param contributed_at 贡献确认时刻 / Contribution confirmation instant.
    @param ledger_entry_id 对应银行双重记账分录 / Corresponding bank double-entry ledger entry.
    @param project_id 可选定向项目标识 / Optional targeted project identity.
    """

    contribution_id: UUID
    """@brief 贡献稳定标识 / Stable contribution identity."""

    town: TownScope
    """@brief 目标小镇范围 / Target town scope."""

    contributor: PersonalScope
    """@brief 贡献者个人范围 / Contributor personal scope."""

    amount: TokenAmount
    """@brief 已确认正数金额 / Confirmed positive amount."""

    contributed_at: datetime
    """@brief 贡献确认时刻 / Contribution confirmation instant."""

    ledger_entry_id: UUID
    """@brief 银行账本分录标识 / Bank ledger-entry identity."""

    project_id: UUID | None = None
    """@brief 可选定向项目标识 / Optional targeted project identity."""

    def __post_init__(self) -> None:
        """@brief 验证贡献的跨上下文边界 / Validate contribution cross-context boundaries.

        @return None / None.
        @raise TypeError 范围、金额或标识类型非法时抛出 /
            Raised when scope, amount, or identity types are invalid.
        """

        if not isinstance(self.contribution_id, UUID):
            raise TypeError("Town contribution ID must be a UUID")
        if not isinstance(self.town, TownScope):
            raise TypeError("Town contribution must use a TownScope")
        if not isinstance(self.contributor, PersonalScope):
            raise TypeError("Town contribution must use a PersonalScope contributor")
        if not isinstance(self.amount, TokenAmount):
            raise TypeError("Town contribution amount must be TokenAmount")
        if not isinstance(self.ledger_entry_id, UUID):
            raise TypeError("Town contribution ledger entry ID must be a UUID")
        if self.project_id is not None and not isinstance(self.project_id, UUID):
            raise TypeError("Town contribution project ID must be a UUID")
        object.__setattr__(
            self,
            "contributed_at",
            normalize_instant(
                self.contributed_at,
                field="Town contribution time",
            ),
        )


@dataclass(frozen=True, slots=True)
class Town:
    """@brief 一个群组唯一拥有的一座共同生活小镇 / One shared-living town uniquely owned by a group.

    该聚合只接受 ``TownScope``，因此个人 RPG 的 ``PersonalScope`` 不可能被误作群组状态。
    The aggregate accepts only ``TownScope``, so a personal-RPG ``PersonalScope`` cannot be
    mistaken for group state.

    @param scope 小镇所属群组范围 / Owning group-town scope.
    @param title 小镇展示名称 / Town display name.
    @param created_at 小镇创建时刻 / Town creation instant.
    @param treasury 与银行群组账户同步的摘要 / Summary synchronized with bank group account.
    @param projects 已提议的建设项目 / Proposed construction projects.
    @param prosperity 已完成项目带来的繁荣度 / Prosperity granted by completed projects.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    """

    scope: TownScope
    """@brief 所属群组小镇范围 / Owning group-town scope."""

    title: str
    """@brief 小镇展示名称 / Town display name."""

    created_at: datetime
    """@brief 小镇创建时刻 / Town creation instant."""

    treasury: TownTreasury = field(default_factory=TownTreasury)
    """@brief 小镇金库摘要 / Town-treasury summary."""

    projects: tuple[TownProject, ...] = ()
    """@brief 已提议项目 / Proposed projects."""

    prosperity: int = 0
    """@brief 已获得繁荣度 / Earned prosperity."""

    version: int = 0
    """@brief 乐观并发版本 / Optimistic-concurrency version."""

    def __post_init__(self) -> None:
        """@brief 验证小镇、项目与金库不变量 / Validate town, project, and treasury invariants.

        @return None / None.
        @raise TypeError 范围、金库、项目或数值类型非法时抛出 /
            Raised when scope, treasury, project, or numeric types are invalid.
        @raise ValueError 项目标识重复或数值非法时抛出 /
            Raised when project identities repeat or numeric values are invalid.
        """

        if not isinstance(self.scope, TownScope):
            raise TypeError("Town must use a TownScope")
        if not isinstance(self.treasury, TownTreasury):
            raise TypeError("Town treasury must be a TownTreasury")
        if not isinstance(self.projects, tuple) or any(
            not isinstance(project, TownProject) for project in self.projects
        ):
            raise TypeError("Town projects must be a tuple of TownProject values")
        project_ids = tuple(project.project_id for project in self.projects)
        if len(set(project_ids)) != len(project_ids):
            raise ValueError("Town cannot contain duplicate project IDs")
        if isinstance(self.prosperity, bool) or not isinstance(self.prosperity, int):
            raise TypeError("Town prosperity must be an integer")
        if self.prosperity < 0:
            raise ValueError("Town prosperity cannot be negative")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("Town version must be an integer")
        if self.version < 0:
            raise ValueError("Town version cannot be negative")
        object.__setattr__(
            self,
            "title",
            normalize_text(
                self.title,
                field="Town title",
                minimum_length=1,
                maximum_length=120,
            ),
        )
        object.__setattr__(
            self,
            "created_at",
            normalize_instant(self.created_at, field="Town creation time"),
        )

    def create_project(self, project: TownProject) -> Town:
        """@brief 将一项新项目加入此小镇 / Add a new project to this town.

        @param project 待提议项目 / Project to propose.
        @return 版本递增的小镇 / Version-incremented town.
        @raise TypeError 项目类型非法时抛出 / Raised when project type is invalid.
        @raise ValueError 项目标识已存在时抛出 / Raised when project identity already exists.
        """

        if not isinstance(project, TownProject):
            raise TypeError("Town project must be a TownProject")
        if any(existing.project_id == project.project_id for existing in self.projects):
            raise ValueError("Town project ID already exists")
        return replace(self, projects=(*self.projects, project), version=self.version + 1)

    def record_contribution(self, contribution: TownContribution) -> Town:
        """@brief 应用一笔已确认到账的贡献 / Apply one contribution confirmed as credited.

        定向项目贡献会同时增加金库并保留同额预算；未定向贡献仅增加可用金库。
        A targeted contribution both credits the treasury and reserves equal budget; an untargeted
        contribution only credits the available treasury.

        @param contribution 已由银行账本确认的贡献 / Contribution confirmed by the bank ledger.
        @return 版本递增的小镇 / Version-incremented town.
        @raise TypeError 贡献类型非法时抛出 / Raised when contribution type is invalid.
        @raise ValueError 贡献的小镇范围不匹配或项目不能接受资金时抛出 /
            Raised when town scope does not match or project cannot accept funding.
        @note 贡献 ID 与账本分录 ID 的全局去重由原子应用端口和数据库唯一约束保证。/
            Global deduplication of contribution and ledger-entry identities is guaranteed by the
            atomic application port and database uniqueness constraints.
        """

        if not isinstance(contribution, TownContribution):
            raise TypeError("Town contribution must be a TownContribution")
        if contribution.town != self.scope:
            raise ValueError("Town contribution scope does not match this town")

        treasury = self.treasury.credit(contribution.amount)
        if contribution.project_id is None:
            return replace(self, treasury=treasury, version=self.version + 1)

        project = self._project_by_id(contribution.project_id)
        updated_project = project.fund(contribution.amount)
        treasury = treasury.reserve(contribution.amount)
        return replace(
            self,
            treasury=treasury,
            projects=self._replace_project(updated_project),
            version=self.version + 1,
        )

    def complete_project(
        self,
        *,
        project_id: UUID,
        completed_at: datetime,
        settlement_ledger_entry_id: UUID,
    ) -> Town:
        """@brief 完成一项已由银行结算的建设项目 / Complete one construction project settled by the bank.

        @param project_id 待建成项目标识 / Project identity to complete.
        @param completed_at 建成时刻 / Completion instant.
        @param settlement_ledger_entry_id 金库支出对应的银行分录 / Bank entry representing treasury spend.
        @return 版本递增的小镇 / Version-incremented town.
        @raise TypeError 项目或结算标识类型非法时抛出 /
            Raised when project or settlement identity types are invalid.
        @raise ValueError 项目不存在或未满足建成条件时抛出 /
            Raised when project is absent or is not ready to complete.
        """

        if not isinstance(project_id, UUID):
            raise TypeError("Town project ID must be a UUID")
        if not isinstance(settlement_ledger_entry_id, UUID):
            raise TypeError("Town project settlement ID must be a UUID")
        project = self._project_by_id(project_id)
        completed_project = project.complete(
            completed_at=completed_at,
            settlement_ledger_entry_id=settlement_ledger_entry_id,
        )
        return replace(
            self,
            treasury=self.treasury.settle_reservation(project.required_amount),
            projects=self._replace_project(completed_project),
            prosperity=self.prosperity + project.prosperity_reward,
            version=self.version + 1,
        )

    def _project_by_id(self, project_id: UUID) -> TownProject:
        """@brief 按稳定标识查找项目 / Find a project by stable identity.

        @param project_id 项目稳定标识 / Stable project identity.
        @return 匹配项目 / Matching project.
        @raise ValueError 项目不存在时抛出 / Raised when project is absent.
        """

        for project in self.projects:
            if project.project_id == project_id:
                return project
        raise ValueError("Town project does not exist")

    def _replace_project(self, replacement: TownProject) -> tuple[TownProject, ...]:
        """@brief 以同标识新项目替换旧项目 / Replace an existing project with its same-identity successor.

        @param replacement 新项目状态 / New project state.
        @return 保持顺序的项目元组 / Order-preserving project tuple.
        @raise ValueError 替换项目不存在时抛出 / Raised when replacement project is absent.
        """

        replaced = False
        projects: list[TownProject] = []
        for project in self.projects:
            if project.project_id == replacement.project_id:
                projects.append(replacement)
                replaced = True
            else:
                projects.append(project)
        if not replaced:
            raise ValueError("Town project to replace does not exist")
        return tuple(projects)
