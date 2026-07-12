"""@brief PostgreSQL schedule fencing adapter tests / PostgreSQL schedule-fencing adapter tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import pytest

from fogmoe_bot.domain.scheduling import (
    JobKind,
    Recurrence,
    ScheduleClaim,
    ScheduledJob,
    StaleScheduleClaimError,
)
from fogmoe_bot.infrastructure.database.repositories import schedule_repository
from fogmoe_bot.infrastructure.database.repositories.schedule_repository import (
    ScheduleRepository,
)


def _claim() -> ScheduleClaim[object]:
    """@brief 构造固定 schedule claim / Build a fixed schedule claim.

    @return 固定领取凭证 / Fixed claim.
    """

    now = datetime(2030, 1, 1, tzinfo=UTC)
    return ScheduleClaim(
        ScheduledJob(7, 42, JobKind("prompt.turn"), now, now, Recurrence(), None),
        "00000000-0000-0000-0000-000000000007",
        now + timedelta(minutes=5),
    )


def test_all_finalizers_reject_a_reclaimed_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 三种终结都观察 rowcount 并拒绝旧 token / Every finalizer observes rowcount and rejects an old token."""

    statements: list[str] = []

    async def execute(sql: str, params: object = None, **kwargs: object) -> int:
        """@brief 模拟旧 token 未命中 / Simulate an old token matching no row.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param kwargs 可选数据库参数 / Optional database arguments.
        @return 零影响行 / Zero affected rows.
        """

        del params, kwargs
        statements.append(sql)
        return 0

    monkeypatch.setattr(schedule_repository.db_connection, "execute", execute)
    repository = ScheduleRepository()
    claim = _claim()

    async def scenario() -> None:
        """@brief 尝试三种陈旧终结 / Attempt all three stale finalizations.

        @return None / None.
        """

        with pytest.raises(StaleScheduleClaimError):
            await repository.mark_executed(claim)
        with pytest.raises(StaleScheduleClaimError):
            await repository.reschedule(
                claim,
                last_run_at=claim.job.run_at,
                next_run_at=claim.job.run_at + timedelta(hours=1),
            )
        with pytest.raises(StaleScheduleClaimError):
            await repository.mark_failed(claim, "boom")

    asyncio.run(scenario())

    assert len(statements) == 3
    assert all("status = 'executing'" in statement for statement in statements)
    assert all(
        "claim_token = CAST(%s AS uuid)" in statement for statement in statements
    )


def test_finalizer_accepts_the_current_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """@brief 唯一当前 token 可正常终结 / The one current token finalizes normally."""

    async def execute(sql: str, params: object = None, **kwargs: object) -> int:
        """@brief 模拟唯一命中 / Simulate exactly one matching row.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param kwargs 可选数据库参数 / Optional database arguments.
        @return 一影响行 / One affected row.
        """

        del sql, params, kwargs
        return 1

    monkeypatch.setattr(schedule_repository.db_connection, "execute", execute)
    asyncio.run(ScheduleRepository().mark_executed(_claim()))
