"""@brief Schedule lease fencing 的真实 PostgreSQL 契约 / Real-PostgreSQL contract for schedule lease fencing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest

from fogmoe_bot.domain.scheduling import (
    JobKind,
    Recurrence,
    ScheduleClaim,
    ScheduledJob,
    StaleScheduleClaimError,
    to_storage_datetime,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories.schedule_repository import (
    ScheduleRepository,
    insert_schedule,
)


def test_reclaimed_schedule_lease_fences_the_old_token() -> None:
    """@brief lease recovery 后只有新 token 可终结 / Only the new token can finalize after lease recovery."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        """@brief 领取、回收、重领并尝试两个 token / Claim, recover, reclaim, and try both tokens.

        @return None / None.
        """

        repository = ScheduleRepository()
        now = datetime.now(UTC)
        user_id = 8_100_000_000_000_000_000 + int(uuid4().hex[:10], 16)
        async with db_connection.transaction() as connection:
            schedule_id = await insert_schedule(
                user_id=user_id,
                run_at=now - timedelta(minutes=1),
                recurrence_unit="none",
                recurrence_interval=1,
                trigger_reason="fencing-test",
                context_text=None,
                instruction_text="test",
                connection=connection,
            )
        assert schedule_id is not None
        try:
            job = ScheduledJob(
                schedule_id,
                user_id,
                JobKind("prompt.turn"),
                now - timedelta(minutes=1),
                now,
                Recurrence(),
                None,
            )
            first = ScheduleClaim(
                job,
                str(uuid4()),
                now + timedelta(seconds=1),
            )
            await db_connection.execute(
                "UPDATE ai_schedules SET status = 'executing', "
                "claim_token = CAST(%s AS UUID), lease_expires_at = %s WHERE id = %s",
                (
                    first.token,
                    to_storage_datetime(first.lease_expires_at),
                    schedule_id,
                ),
            )

            recovered_at = now + timedelta(seconds=2)
            assert await repository.recover_stale(recovered_at) >= 1
            second = ScheduleClaim(
                job,
                str(uuid4()),
                recovered_at + timedelta(minutes=1),
            )
            await db_connection.execute(
                "UPDATE ai_schedules SET status = 'executing', "
                "claim_token = CAST(%s AS UUID), lease_expires_at = %s WHERE id = %s",
                (
                    second.token,
                    to_storage_datetime(second.lease_expires_at),
                    schedule_id,
                ),
            )
            assert second.token != first.token

            with pytest.raises(StaleScheduleClaimError):
                await repository.mark_executed(first)
            await repository.mark_executed(second)
            row = await db_connection.fetch_one(
                "SELECT status, claim_token FROM ai_schedules WHERE id = %s",
                (schedule_id,),
            )
            assert row is not None
            assert str(row[0]) == "executed"
            assert row[1] is None
        finally:
            await db_connection.execute(
                "DELETE FROM ai_schedules WHERE id = %s",
                (schedule_id,),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
