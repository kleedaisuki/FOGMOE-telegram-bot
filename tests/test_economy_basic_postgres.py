"""@brief 基础经济命令的真实 PostgreSQL 契约 / Real-PostgreSQL contracts for basic economy commands."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from postgres_test_support import configure_bot_database

from fogmoe_bot.application.economy.check_in import CheckInCommand
from fogmoe_bot.application.economy.common import EconomyCode
from fogmoe_bot.application.economy.community import (
    GiftCommand,
    LeaderboardCommand,
)
from fogmoe_bot.application.economy.lottery import (
    ClaimLotteryCommand,
    LotteryAlreadyClaimedResult,
    LotteryGrantedResult,
    LotteryNotRegisteredResult,
)
from fogmoe_bot.domain.banking.ledger import LedgerAccount, LedgerReason
from fogmoe_bot.domain.banking.money import SystemAccountKind, TokenAmount, TokenBucket
from fogmoe_bot.domain.economy.identity import EconomyAccountId
from fogmoe_bot.domain.economy.lottery import LotteryClaimInstant, LotteryPrize
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.banking import (
    load_bank_overview,
    post_bank_transfer,
)
from fogmoe_bot.infrastructure.database.economy.community import (
    PostgresCommunityOperations,
)
from fogmoe_bot.infrastructure.database.economy.check_in import (
    PostgresCheckInOperations,
)
from fogmoe_bot.infrastructure.database.economy.lottery import (
    PostgresLotteryClaimTransaction,
)


def _postgres_url() -> str:
    """@brief 读取显式测试 DSN / Read an explicit test DSN.

    @return async SQLAlchemy URL / Async SQLAlchemy URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    pytest.skip("set FOGMOE_TEST_DATABASE_URL to run the real PostgreSQL contract")


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

        await db.dispose_current_engine()
        configure_bot_database(_postgres_url())
        sender_id = _test_user_id()
        recipient_id = _test_user_id()
        suffix = uuid4().hex
        sender_name = f"sender_{suffix}"
        recipient_name = f"recipient_{suffix}"
        lottery_key = f"pg-basic:lottery:{suffix}"
        gift_key = f"pg-basic:gift:{suffix}"
        now = datetime.now(UTC)
        rewards = PostgresLotteryClaimTransaction()
        community = PostgresCommunityOperations()
        try:
            async with db.transaction() as connection:
                await db.execute(
                    "INSERT INTO identity.users "
                    "(id, tg_uid, provider, name) "
                    "VALUES (%s, %s, 'telegram', %s), "
                    "(%s, %s, 'telegram', %s)",
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
                await post_bank_transfer(
                    namespace="test-economy-seed",
                    source_idempotency_key=f"{suffix}:initial",
                    reason=LedgerReason.BANK_ISSUANCE,
                    source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
                    destination=LedgerAccount.user(sender_id, TokenBucket.FREE),
                    amount=TokenAmount(100),
                    created_at=now,
                    actor_id=None,
                    connection=connection,
                )

            lottery = ClaimLotteryCommand(
                account_id=EconomyAccountId(sender_id),
                proposed_prize=LotteryPrize(7),
                claimed_at=LotteryClaimInstant(now),
                idempotency_key=lottery_key,
            )
            first_lottery, second_lottery = await asyncio.gather(
                rewards.claim_lottery(lottery),
                rewards.claim_lottery(lottery),
            )
            assert isinstance(first_lottery, LotteryGrantedResult)
            assert isinstance(second_lottery, LotteryGrantedResult)
            assert {first_lottery.replayed, second_lottery.replayed} == {False, True}
            changed_lottery = await rewards.claim_lottery(
                ClaimLotteryCommand(
                    account_id=EconomyAccountId(sender_id),
                    proposed_prize=LotteryPrize(13),
                    claimed_at=LotteryClaimInstant(now + timedelta(seconds=1)),
                    idempotency_key=lottery_key,
                )
            )
            assert isinstance(changed_lottery, LotteryGrantedResult)
            assert changed_lottery.replayed
            assert changed_lottery.prize == first_lottery.prize == LotteryPrize(7)

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
            assert all(isinstance(entry.coins, int) for entry in leaderboard.entries)
            async with db.transaction() as connection:
                await post_bank_transfer(
                    namespace="test-economy-seed",
                    source_idempotency_key=f"{suffix}:later",
                    reason=LedgerReason.BANK_ISSUANCE,
                    source=LedgerAccount.system(SystemAccountKind.ISSUANCE),
                    destination=LedgerAccount.user(recipient_id, TokenBucket.FREE),
                    amount=TokenAmount(100),
                    created_at=now + timedelta(seconds=1),
                    actor_id=None,
                    connection=connection,
                )
            leaderboard_replay = await community.leaderboard(leaderboard_command)
            assert leaderboard_replay.replayed
            assert leaderboard_replay.entries == leaderboard.entries

            async with db.connect() as connection:
                sender_balance = await load_bank_overview(sender_id, connection)
                recipient_balance = await load_bank_overview(recipient_id, connection)
                assert sender_balance.total == 95
                assert recipient_balance.total == 110
        finally:
            async with db.transaction() as connection:
                await db.execute(
                    "DELETE FROM economy.operation_receipts WHERE user_id IN (%s, %s)",
                    (sender_id, recipient_id),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM economy.user_give_daily WHERE user_id = %s",
                    (sender_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM economy.user_lottery WHERE user_id = %s",
                    (sender_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM identity.users WHERE id IN (%s, %s)",
                    (sender_id, recipient_id),
                    connection=connection,
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())


def test_real_postgres_lottery_maps_domain_cooldown_boundaries_once() -> None:
    """@brief PostgreSQL 只映射领域的冷却边界且拒绝不发币 /
    PostgreSQL only maps domain cooldown boundaries and a rejection grants no tokens.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行首次、边界前与恰好边界领取 / Execute first, just-before-boundary, and exact-boundary claims.

        @return None / None.
        """

        await db.dispose_current_engine()
        configure_bot_database(_postgres_url())
        user_id = _test_user_id()
        suffix = uuid4().hex
        account_id = EconomyAccountId(user_id)
        operations = PostgresLotteryClaimTransaction()
        first_at = datetime.now(UTC)
        first_command = ClaimLotteryCommand(
            account_id=account_id,
            proposed_prize=LotteryPrize(7),
            claimed_at=LotteryClaimInstant(first_at),
            idempotency_key=f"pg-lottery:first:{suffix}",
        )
        try:
            missing = await operations.claim_lottery(first_command)
            assert isinstance(missing, LotteryNotRegisteredResult)
            async with db.connect() as connection:
                missing_receipt = await db.fetch_one(
                    "SELECT 1 FROM economy.operation_receipts "
                    "WHERE idempotency_key = %s",
                    (first_command.idempotency_key,),
                    connection=connection,
                )
            assert missing_receipt is None

            async with db.transaction() as connection:
                await db.execute(
                    "INSERT INTO identity.users (id, tg_uid, provider, name) "
                    "VALUES (%s, %s, 'telegram', %s)",
                    (user_id, user_id, f"lottery_{suffix}"),
                    connection=connection,
                )

            first = await operations.claim_lottery(first_command)
            early_command = ClaimLotteryCommand(
                account_id=account_id,
                proposed_prize=LotteryPrize(19),
                claimed_at=LotteryClaimInstant(
                    first_at + timedelta(hours=24) - timedelta(microseconds=1)
                ),
                idempotency_key=f"pg-lottery:early:{suffix}",
            )
            early = await operations.claim_lottery(early_command)
            early_replay = await operations.claim_lottery(
                ClaimLotteryCommand(
                    account_id=account_id,
                    proposed_prize=LotteryPrize(1),
                    claimed_at=LotteryClaimInstant(first_at + timedelta(days=2)),
                    idempotency_key=early_command.idempotency_key,
                )
            )
            boundary_at = first_at + timedelta(hours=24)
            boundary = await operations.claim_lottery(
                ClaimLotteryCommand(
                    account_id=account_id,
                    proposed_prize=LotteryPrize(13),
                    claimed_at=LotteryClaimInstant(boundary_at),
                    idempotency_key=f"pg-lottery:boundary:{suffix}",
                )
            )

            assert isinstance(first, LotteryGrantedResult)
            assert isinstance(early, LotteryAlreadyClaimedResult)
            assert early.next_eligible_at == LotteryClaimInstant(boundary_at)
            assert isinstance(early_replay, LotteryAlreadyClaimedResult)
            assert early_replay.replayed
            assert early_replay.next_eligible_at == early.next_eligible_at
            assert isinstance(boundary, LotteryGrantedResult)
            assert boundary.prize == LotteryPrize(13)
            async with db.connect() as connection:
                row = await db.fetch_one(
                    "SELECT last_lottery_date FROM economy.user_lottery "
                    "WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                balance = await load_bank_overview(user_id, connection)
            assert row is not None
            assert row[0] == boundary_at.replace(tzinfo=None)
            assert balance.free.value == 20
        finally:
            async with db.transaction() as connection:
                await db.execute(
                    "DELETE FROM economy.operation_receipts WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM economy.user_lottery WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM identity.users WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())


def test_real_postgres_check_in_maps_domain_lifecycle_once() -> None:
    """@brief PostgreSQL 只映射领域签到决策且重放不重复发币 /
    PostgreSQL only maps domain check-in decisions and replay never grants twice.

    @return None / None.
    """

    async def scenario() -> None:
        """@brief 执行首次、连续、重复与断签状态转换 / Execute first, consecutive, repeated, and gap transitions.

        @return None / None.
        """

        await db.dispose_current_engine()
        configure_bot_database(_postgres_url())
        user_id = _test_user_id()
        suffix = uuid4().hex
        account_id = EconomyAccountId(user_id)
        operations = PostgresCheckInOperations()
        first_day = date(2030, 1, 1)
        try:
            async with db.transaction() as connection:
                await db.execute(
                    "INSERT INTO identity.users (id, tg_uid, provider, name) "
                    "VALUES (%s, %s, 'telegram', %s)",
                    (user_id, user_id, f"checkin_{suffix}"),
                    connection=connection,
                )

            first_command = CheckInCommand(
                account_id=account_id,
                day=first_day,
                idempotency_key=f"pg-checkin:first:{suffix}",
            )
            first, replay = await asyncio.gather(
                operations.check_in(first_command),
                operations.check_in(first_command),
            )
            assert first.code is EconomyCode.SUCCESS
            assert replay.code is EconomyCode.SUCCESS
            assert {first.replayed, replay.replayed} == {False, True}
            assert first.consecutive_days == replay.consecutive_days == 1
            assert first.reward == replay.reward == 1

            consecutive = await operations.check_in(
                CheckInCommand(
                    account_id=account_id,
                    day=first_day + timedelta(days=1),
                    idempotency_key=f"pg-checkin:second:{suffix}",
                )
            )
            assert consecutive.code is EconomyCode.SUCCESS
            assert consecutive.consecutive_days == 2
            assert consecutive.reward == 1

            after_gap = await operations.check_in(
                CheckInCommand(
                    account_id=account_id,
                    day=first_day + timedelta(days=3),
                    idempotency_key=f"pg-checkin:gap:{suffix}",
                )
            )
            assert after_gap.code is EconomyCode.SUCCESS
            assert after_gap.consecutive_days == 1
            assert after_gap.reward == 1

            async with db.connect() as connection:
                row = await db.fetch_one(
                    "SELECT last_checkin_date, consecutive_days "
                    "FROM economy.user_checkin WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                balance = await load_bank_overview(user_id, connection)
            assert row is not None
            assert row[0] == first_day + timedelta(days=3)
            assert row[1] == 1
            assert balance.free.value == 3
        finally:
            async with db.transaction() as connection:
                await db.execute(
                    "DELETE FROM economy.operation_receipts WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM economy.user_checkin WHERE user_id = %s",
                    (user_id,),
                    connection=connection,
                )
                await db.execute(
                    "DELETE FROM identity.users WHERE id = %s",
                    (user_id,),
                    connection=connection,
                )
            await db.dispose_current_engine()

    asyncio.run(scenario())
