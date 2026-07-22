"""@brief 群组小镇纯领域模型测试 / Pure group-town domain-model tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from fogmoe_bot.domain.banking.money import TokenAmount
from fogmoe_bot.domain.town.models import (
    Town,
    TownContribution,
    TownProject,
    TownProjectKind,
    TownProjectStatus,
)
from fogmoe_bot.domain.town.scope import TownScope
from fogmoe_bot.domain.world.scope import PersonalScope

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
"""@brief 小镇测试使用的稳定 UTC 时刻 / Stable UTC instant used by town tests."""

TOWN = TownScope(-100_123_456_789)
"""@brief 测试小镇群组范围 / Test group-town scope."""

OTHER_TOWN = TownScope(-100_987_654_321)
"""@brief 另一座测试小镇群组范围 / Another test group-town scope."""

PLAYER = PersonalScope(42)
"""@brief 测试个人 RPG 范围 / Test personal-RPG scope."""


def _town() -> Town:
    """@brief 创建空白测试小镇 / Build an empty test town.

    @return 新建小镇聚合 / Newly created town aggregate.
    """

    return Town(scope=TOWN, title="雾萌小镇", created_at=NOW)


def _project(*, required_amount: int = 10) -> TownProject:
    """@brief 创建测试建设项目 / Build a test construction project.

    @param required_amount 所需正数金币 / Required positive token amount.
    @return 正在筹资的测试项目 / Funding test project.
    """

    return TownProject(
        project_id=uuid4(),
        kind=TownProjectKind.OBSERVATORY,
        title="星象观测台",
        required_amount=TokenAmount(required_amount),
        created_by=PLAYER,
        created_at=NOW,
        prosperity_reward=3,
    )


def _contribution(
    *,
    town: TownScope,
    amount: int,
    project_id: UUID | None,
    offset_seconds: int,
) -> TownContribution:
    """@brief 创建已由银行确认的测试贡献 / Build a bank-confirmed test contribution.

    @param town 目标小镇范围 / Target town scope.
    @param amount 已确认正数金额 / Confirmed positive amount.
    @param project_id 可选项目标识 / Optional project identity.
    @param offset_seconds 相对于稳定时刻的秒偏移 / Seconds offset from stable instant.
    @return 测试贡献 / Test contribution.
    """

    return TownContribution(
        contribution_id=uuid4(),
        town=town,
        contributor=PLAYER,
        amount=TokenAmount(amount),
        contributed_at=NOW + timedelta(seconds=offset_seconds),
        ledger_entry_id=uuid4(),
        project_id=project_id,
    )


def test_town_uses_a_group_scope_and_rejects_personal_or_bare_contexts() -> None:
    """@brief 小镇只接受群组范围，个人或裸上下文会被拒绝 / A town accepts only group scope and rejects personal or bare context.

    @return None / None.
    """

    assert TOWN != PLAYER
    with pytest.raises(TypeError, match="TownScope"):
        Town(scope=PLAYER, title="错误", created_at=NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="TownScope"):
        Town(scope=-100_123, title="错误", created_at=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot be zero"):
        TownScope(0)
    with pytest.raises(ValueError, match="positive"):
        PersonalScope(0)


def test_targeted_contributions_reserve_treasury_then_complete_project() -> None:
    """@brief 定向贡献先保留金库，结算后才消耗余额并增加繁荣度 / Targeted contributions reserve treasury, then settlement consumes balance and adds prosperity.

    @return None / None.
    """

    initial = _town()
    project = _project()
    proposed = initial.create_project(project)
    first = proposed.record_contribution(
        _contribution(
            town=TOWN,
            amount=4,
            project_id=project.project_id,
            offset_seconds=1,
        )
    )
    ready = first.record_contribution(
        _contribution(
            town=TOWN,
            amount=6,
            project_id=project.project_id,
            offset_seconds=2,
        )
    )
    completed = ready.complete_project(
        project_id=project.project_id,
        completed_at=NOW + timedelta(seconds=3),
        settlement_ledger_entry_id=uuid4(),
    )

    assert initial.projects == ()
    assert proposed.treasury.balance == 0
    assert first.treasury.balance == 4
    assert first.treasury.reserved == 4
    assert first.treasury.available_balance == 0
    assert ready.projects[0].funded_amount == 10
    assert ready.projects[0].status is TownProjectStatus.READY
    assert ready.treasury.balance == 10
    assert ready.treasury.reserved == 10
    assert completed.projects[0].status is TownProjectStatus.COMPLETED
    assert completed.treasury.balance == 0
    assert completed.treasury.reserved == 0
    assert completed.treasury.lifetime_contributed == 10
    assert completed.treasury.lifetime_settled == 10
    assert completed.prosperity == 3


def test_contribution_cannot_cross_town_or_overfund_a_project() -> None:
    """@brief 跨小镇贡献和项目超额筹资均被拒绝 / Cross-town contributions and project overfunding are rejected.

    @return None / None.
    """

    project = _project(required_amount=5)
    town = _town().create_project(project)

    with pytest.raises(ValueError, match="scope does not match"):
        town.record_contribution(
            _contribution(
                town=OTHER_TOWN,
                amount=1,
                project_id=project.project_id,
                offset_seconds=1,
            )
        )
    with pytest.raises(ValueError, match="overfund"):
        town.record_contribution(
            _contribution(
                town=TOWN,
                amount=6,
                project_id=project.project_id,
                offset_seconds=1,
            )
        )
