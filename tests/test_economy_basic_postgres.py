"""@brief 基础经济命令的真实 PostgreSQL 契约 / Real-PostgreSQL contracts for basic economy commands."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import os
from pathlib import Path
from uuid import uuid4

import pytest

from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.community import (
    GiftCommand,
    LeaderboardCommand,
)
from fogmoe_bot.application.economy.rewards import LotteryCommand
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.economy.community import (
    PostgresCommunityOperations,
)
from fogmoe_bot.infrastructure.database.economy.rewards import (
    PostgresRewardOperations,
)
from fogmoe_dbctl.postgres import read_service, service_sqlalchemy_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


def _postgres_url() -> str:
    """@brief 读取显式测试 DSN 或 automation service / Read an explicit test DSN or automation service.

    @return async SQLAlchemy URL / Async SQLAlchemy URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")
    config_dir = PROJECT_ROOT / "var/psql"
    if not (config_dir / "pg_service.conf").is_file():
        pytest.skip("local PostgreSQL service configuration is unavailable")
    return service_sqlalchemy_url(read_service(config_dir, "fogmoe_automation"))


def _test_user_id() -> int:
    """@brief 生成正 BIGINT 测试 ID / Generate a positive BIGINT test ID.

    @return disjoint user ID / Disjoint user ID.
    """

    return 7_000_000_000_000_000_000 + int(uuid4().hex[:12], 16)


def test_real_postgres_lottery_and_gift_replay_without_double_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 并发重放只抽奖、赠送各一次，异义 gift 被拒绝 / Concurrent replay grants lottery and gift once and rejects a changed gift.

    @param monkeypatch 临时绑定测试 DSN / Temporarily bind the test DSN.
    """

    async def scenario() -> None:
        """@brief 执行并发与冲突场景 / Execute concurrency and conflict scenarios.

        @return None / None.
        """

        monkeypatch.setattr(config, "SQLALCHEMY_DATABASE_URI", _postgres_url())
        await db.dispose_current_engine()
        sender_id = _test_user_id()
        recipient_id = _test_user_id()
        suffix = uuid4().hex
        sender_name = f"sender_{suffix}"
        recipient_name = f"recipient_{suffix}"
        lottery_key = f"pg-basic:lottery:{suffix}"
        gift_key = f"pg-basic:gift:{suffix}"
        now = datetime.now(UTC)
        rewards = PostgresRewardOperations()
        community = PostgresCommunityOperations(admin_user_id=1)
        try:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "INSERT INTO identity.users "
                    "(id, tg_uid, provider, name, coins, coins_paid, user_plan) "
                    "VALUES (%s, %s, 'telegram', %s, 100, 0, 'free'), "
                    "(%s, %s, 'telegram', %s, 0, 0, 'free')",
                    (
                        sender_id,
                        sender_id,
                        sender_name,
                        recipient_id,
                        recipient_id,
                        recipient_name,
                    ),
                    connection=connection,
                )

            lottery = LotteryCommand(
                user_id=sender_id,
                prize=7,
                claimed_at=now,
                cooldown=timedelta(hours=24),
                idempotency_key=lottery_key,
            )
            first_lottery, second_lottery = await asyncio.gather(
                rewards.claim_lottery(lottery),
                rewards.claim_lottery(lottery),
            )
            assert first_lottery.code is EconomyCode.SUCCESS
            assert second_lottery.code is EconomyCode.SUCCESS
            assert {first_lottery.replayed, second_lottery.replayed} == {False, True}

            gift = GiftCommand(
                sender_id=sender_id,
                target_name=recipient_name,
                amount=10,
                fee=2,
                business_date=date.today(),
                daily_limit=5,
                idempotency_key=gift_key,
            )
            first_gift, second_gift = await asyncio.gather(
                community.give(gift),
                community.give(gift),
            )
            assert first_gift.code is EconomyCode.SUCCESS
            assert second_gift.code is EconomyCode.SUCCESS
            assert {first_gift.replayed, second_gift.replayed} == {False, True}

            changed = GiftCommand(
                sender_id=sender_id,
                target_name=recipient_name,
                amount=11,
                fee=2,
                business_date=gift.business_date,
                daily_limit=5,
                idempotency_key=gift_key,
            )
            with pytest.raises(ValueError, match="changed command semantics"):
                await community.give(changed)

            leaderboard_key = f"pg-basic:leaderboard:{suffix}"
            leaderboard_command = LeaderboardCommand(
                requester_id=sender_id,
                limit=5,
                idempotency_key=leaderboard_key,
            )
            leaderboard = await community.leaderboard(leaderboard_command)
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "UPDATE identity.users SET coins = coins + 100 WHERE id = %s",
                    (recipient_id,),
                    connection=connection,
                )
            leaderboard_replay = await community.leaderboard(leaderboard_command)
            assert leaderboard_replay.replayed
            assert leaderboard_replay.entries == leaderboard.entries

            async with db_connection.connect() as connection:
                sender_balance = await db_connection.fetch_one(
                    "SELECT coins + coins_paid FROM identity.users WHERE id = %s",
                    (sender_id,),
                    connection=connection,
                )
                recipient_balance = await db_connection.fetch_one(
                    "SELECT coins + coins_paid FROM identity.users WHERE id = %s",
                    (recipient_id,),
                    connection=connection,
                )
                assert sender_balance is not None and sender_balance[0] == 95
                assert recipient_balance is not None and recipient_balance[0] == 110
        finally:
            async with db_connection.transaction() as connection:
                await db_connection.execute(
                    "DELETE FROM economy.operation_receipts WHERE user_id IN (%s, %s)",
                    (sender_id, recipient_id),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM economy.user_give_daily WHERE user_id = %s",
                    (sender_id,),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM economy.user_lottery WHERE user_id = %s",
                    (sender_id,),
                    connection=connection,
                )
                await db_connection.execute(
                    "DELETE FROM identity.users WHERE id IN (%s, %s)",
                    (sender_id, recipient_id),
                    connection=connection,
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())
