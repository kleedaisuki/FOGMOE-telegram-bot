"""@brief Crypto bounded context 的真实 PostgreSQL 并发契约 / Real-PostgreSQL concurrency contracts for the Crypto bounded context."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.crypto.workflow import (
    BindChartToken,
    ClearChartToken,
    CreateBtcPrediction,
    CryptoResultCode,
    SubmitTokenSwap,
)
from fogmoe_bot.domain.crypto import (
    Blockchain,
    ChartToken,
    CoinStake,
    ContractAddress,
    PredictionDirection,
    PriceQuote,
    SolanaWalletAddress,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundDraft,
    OutboundEnqueueResult,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.crypto_operations.chart import (
    PostgresChartOperations,
)
from fogmoe_bot.infrastructure.database.crypto_operations.prediction import (
    PostgresPredictionOperations,
)
from fogmoe_bot.infrastructure.database.crypto_operations.swap import (
    PostgresSwapOperations,
)


class _FailingOutbox:
    """@brief 在 enqueue 时失败的事务 outbox / Transactional outbox that fails during enqueue."""

    async def enqueue_standalone_outbound_in_transaction(
        self,
        connection: AsyncConnection,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 模拟 outbox 写失败 / Simulate an outbox write failure.

        @param connection 当前事务 / Current transaction.
        @param draft 出站草稿 / Outbound draft.
        @raise RuntimeError 始终抛出 / Always raised.
        """

        del connection, draft
        raise RuntimeError("injected outbox failure")


def _user_id() -> int:
    """@brief 生成不与 Telegram 用户冲突的正 BIGINT / Generate a positive BIGINT disjoint from Telegram users.

    @return 测试用户 ID / Test user ID.
    """

    return 8_100_000_000_000_000_000 + int(uuid4().hex[:12], 16)


def test_crypto_writes_are_cross_process_safe_and_prediction_result_is_durable() -> (
    None
):
    """@brief 并发 swap/prediction 仅扣一次，结算与 outbox 原子且可重放 / Concurrent swap/prediction charges once, while settlement and outbox are atomic and replayable."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        swap_user = _user_id()
        prediction_user = _user_id()
        chart_group = -_user_id()
        suffix = uuid4().hex
        wallet = SolanaWalletAddress("5iz3epFDf9SKvLNHWQ42f4wMMrENaudE9eMkxfBLFd2n")
        charts = PostgresChartOperations()
        predictions = PostgresPredictionOperations(admin_user_id=1)
        swaps = PostgresSwapOperations(admin_user_id=1)
        now = datetime.now(UTC)
        prediction_conversation = f"crypto:prediction:user:{prediction_user}"
        try:
            await db_connection.execute(
                "INSERT INTO identity.users "
                "(id, tg_uid, provider, name, coins, coins_paid, user_plan) "
                "VALUES (%s, %s, 'telegram', 'crypto-swap', 20000, 0, 'free'), "
                "(%s, %s, 'telegram', 'crypto-prediction', 20, 0, 'free')",
                (swap_user, swap_user, prediction_user, prediction_user),
            )
            first_swap = SubmitTokenSwap(
                swap_user,
                "crypto-test",
                wallet,
                CoinStake(10_000),
                f"crypto-pg:swap:a:{suffix}",
            )
            second_swap = SubmitTokenSwap(
                swap_user,
                "crypto-test",
                wallet,
                CoinStake(10_000),
                f"crypto-pg:swap:b:{suffix}",
            )
            swap_results = await asyncio.wait_for(
                asyncio.gather(
                    swaps.submit_swap(first_swap),
                    swaps.submit_swap(second_swap),
                ),
                timeout=3,
            )
            assert {result.code for result in swap_results} == {
                CryptoResultCode.SUCCESS,
                CryptoResultCode.PENDING_SWAP,
            }
            replay_key = next(
                command
                for command, result in zip(
                    (first_swap, second_swap), swap_results, strict=True
                )
                if result.code is CryptoResultCode.SUCCESS
            )
            replay = await swaps.submit_swap(replay_key)
            assert replay.code is CryptoResultCode.SUCCESS and replay.replayed
            swap_balance = await db_connection.fetch_one(
                "SELECT coins + coins_paid FROM identity.users WHERE id = %s",
                (swap_user,),
            )
            assert swap_balance is not None and int(swap_balance[0]) == 10_000
            pending_count = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM crypto.token_swap_requests "
                "WHERE user_id = %s AND status = 'pending'",
                (swap_user,),
            )
            assert pending_count is not None and int(pending_count[0]) == 1

            prediction_commands = tuple(
                CreateBtcPrediction(
                    prediction_user,
                    prediction_user,
                    direction,
                    CoinStake(20),
                    now,
                    f"crypto-pg:prediction:{index}:{suffix}",
                )
                for index, direction in enumerate(
                    (PredictionDirection.UP, PredictionDirection.DOWN)
                )
            )
            prediction_results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        predictions.create_prediction(
                            command,
                            quote=PriceQuote(Decimal("100")),
                        )
                        for command in prediction_commands
                    )
                ),
                timeout=3,
            )
            assert {result.code for result in prediction_results} == {
                CryptoResultCode.SUCCESS,
                CryptoResultCode.ACTIVE_PREDICTION,
            }
            await db_connection.execute(
                "UPDATE crypto.user_btc_predictions SET end_time = %s "
                "WHERE user_id = %s",
                (
                    (now + timedelta(minutes=1)).astimezone().replace(tzinfo=None),
                    prediction_user,
                ),
            )
            settled = await predictions.settle_due_predictions(
                quote=PriceQuote(Decimal("101")),
                settled_at=now + timedelta(minutes=11),
                limit=10,
            )
            assert settled == 1
            assert (
                await predictions.settle_due_predictions(
                    quote=PriceQuote(Decimal("102")),
                    settled_at=now + timedelta(minutes=12),
                    limit=10,
                )
                == 0
            )
            outcome = await db_connection.fetch_one(
                "SELECT is_completed, end_price, is_correct, reward "
                "FROM crypto.user_btc_predictions WHERE user_id = %s",
                (prediction_user,),
            )
            assert outcome is not None
            assert bool(outcome[0])
            assert Decimal(str(outcome[1])) == Decimal("101")
            winning_direction = next(
                command.direction
                for command, result in zip(
                    prediction_commands, prediction_results, strict=True
                )
                if result.code is CryptoResultCode.SUCCESS
            )
            expected_correct = winning_direction is PredictionDirection.UP
            assert bool(outcome[2]) is expected_correct
            assert int(outcome[3]) == (36 if expected_correct else 0)
            prediction_balance = await db_connection.fetch_one(
                "SELECT coins + coins_paid FROM identity.users WHERE id = %s",
                (prediction_user,),
            )
            assert prediction_balance is not None
            assert int(prediction_balance[0]) == (36 if expected_correct else 0)
            outbox_count = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM conversation.outbound_messages "
                "WHERE conversation_id = %s",
                (prediction_conversation,),
            )
            assert outbox_count is not None and int(outbox_count[0]) == 1

            first_token = ChartToken(
                Blockchain.SOLANA,
                ContractAddress("2z9nPFtFRFwTTpQ6RpamUzsMfmF65Y3g14wu5FLj5rWC"),
            )
            bind = BindChartToken(
                chart_group,
                swap_user,
                first_token,
                f"crypto-pg:chart-bind:{suffix}",
            )
            clear = ClearChartToken(
                chart_group,
                swap_user,
                f"crypto-pg:chart-clear:{suffix}",
            )
            await charts.bind_chart(bind)
            await charts.clear_chart(clear)
            bind_replay = await charts.bind_chart(bind)
            assert bind_replay.replayed and bind_replay.token == first_token
            assert await charts.chart_token(chart_group) is None
        finally:
            await db_connection.execute(
                "DELETE FROM conversation.outbound_messages WHERE conversation_id = %s",
                (prediction_conversation,),
            )
            await db_connection.execute(
                "DELETE FROM crypto.operation_receipts WHERE actor_id IN (%s, %s)",
                (swap_user, prediction_user),
            )
            await db_connection.execute(
                "DELETE FROM crypto.group_chart_tokens WHERE group_id = %s",
                (chart_group,),
            )
            await db_connection.execute(
                "DELETE FROM crypto.user_btc_predictions WHERE user_id = %s",
                (prediction_user,),
            )
            await db_connection.execute(
                "DELETE FROM crypto.token_swap_requests WHERE user_id = %s",
                (swap_user,),
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id IN (%s, %s)",
                (swap_user, prediction_user),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())


def test_prediction_settlement_rolls_back_if_outbox_cannot_commit() -> None:
    """@brief outbox 写失败会回滚 outcome 与奖励 / An outbox write failure rolls back both outcome and reward."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        user_id = _user_id()
        now = datetime.now(UTC)
        normal = PostgresPredictionOperations(admin_user_id=1)
        failing = PostgresPredictionOperations(
            admin_user_id=1,
            outbox=_FailingOutbox(),
        )
        try:
            await db_connection.execute(
                "INSERT INTO identity.users "
                "(id, tg_uid, provider, name, coins, coins_paid, user_plan) "
                "VALUES (%s, %s, 'telegram', 'crypto-rollback', 20, 0, 'free')",
                (user_id, user_id),
            )
            await normal.create_prediction(
                CreateBtcPrediction(
                    user_id,
                    user_id,
                    PredictionDirection.UP,
                    CoinStake(20),
                    now,
                    f"crypto-pg:rollback:{uuid4().hex}",
                ),
                quote=PriceQuote(Decimal("100")),
            )
            await db_connection.execute(
                "UPDATE crypto.user_btc_predictions SET end_time = %s "
                "WHERE user_id = %s",
                (
                    (now + timedelta(minutes=1)).astimezone().replace(tzinfo=None),
                    user_id,
                ),
            )
            with pytest.raises(RuntimeError, match="outbox failure"):
                await failing.settle_due_predictions(
                    quote=PriceQuote(Decimal("101")),
                    settled_at=now + timedelta(minutes=11),
                    limit=1,
                )
            prediction = await db_connection.fetch_one(
                "SELECT is_completed, end_price, reward "
                "FROM crypto.user_btc_predictions WHERE user_id = %s",
                (user_id,),
            )
            assert prediction is not None
            assert not bool(prediction[0])
            assert prediction[1] is None and prediction[2] is None
            account = await db_connection.fetch_one(
                "SELECT coins + coins_paid FROM identity.users WHERE id = %s",
                (user_id,),
            )
            assert account is not None and int(account[0]) == 0
        finally:
            await db_connection.execute(
                "DELETE FROM crypto.operation_receipts WHERE actor_id = %s",
                (user_id,),
            )
            await db_connection.execute(
                "DELETE FROM crypto.user_btc_predictions WHERE user_id = %s",
                (user_id,),
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id = %s",
                (user_id,),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())


def test_prediction_workers_use_account_first_skip_locked_batches() -> None:
    """@brief 多 worker 用账户优先 SKIP LOCKED 并行结算不重叠批次 / Multiple workers settle disjoint batches with account-first SKIP LOCKED locking."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        user_ids = tuple(_user_id() for _ in range(4))
        now = datetime.now(UTC)
        predictions = PostgresPredictionOperations(admin_user_id=1)
        try:
            for index, user_id in enumerate(user_ids):
                await db_connection.execute(
                    "INSERT INTO identity.users "
                    "(id, tg_uid, provider, name, coins, coins_paid, user_plan) "
                    "VALUES (%s, %s, 'telegram', %s, 20, 0, 'free')",
                    (user_id, user_id, f"crypto-batch-{index}"),
                )
                await predictions.create_prediction(
                    CreateBtcPrediction(
                        user_id,
                        user_id,
                        PredictionDirection.UP,
                        CoinStake(20),
                        now,
                        f"crypto-pg:batch:{index}:{uuid4().hex}",
                    ),
                    quote=PriceQuote(Decimal("100")),
                )
            await db_connection.execute(
                "UPDATE crypto.user_btc_predictions SET end_time = %s "
                "WHERE user_id IN (%s, %s, %s, %s)",
                (
                    (now + timedelta(minutes=1)).astimezone().replace(tzinfo=None),
                    *user_ids,
                ),
            )
            counts = await asyncio.wait_for(
                asyncio.gather(
                    predictions.settle_due_predictions(
                        quote=PriceQuote(Decimal("101")),
                        settled_at=now + timedelta(minutes=11),
                        limit=2,
                    ),
                    predictions.settle_due_predictions(
                        quote=PriceQuote(Decimal("101")),
                        settled_at=now + timedelta(minutes=11),
                        limit=2,
                    ),
                ),
                timeout=3,
            )
            assert sum(counts) == 4
            completed = await db_connection.fetch_one(
                "SELECT COUNT(*) FROM crypto.user_btc_predictions "
                "WHERE user_id IN (%s, %s, %s, %s) AND is_completed = TRUE",
                user_ids,
            )
            assert completed is not None and int(completed[0]) == 4
        finally:
            for user_id in user_ids:
                await db_connection.execute(
                    "DELETE FROM conversation.outbound_messages "
                    "WHERE conversation_id = %s",
                    (f"crypto:prediction:user:{user_id}",),
                )
            await db_connection.execute(
                "DELETE FROM crypto.operation_receipts "
                "WHERE actor_id IN (%s, %s, %s, %s)",
                user_ids,
            )
            await db_connection.execute(
                "DELETE FROM crypto.user_btc_predictions "
                "WHERE user_id IN (%s, %s, %s, %s)",
                user_ids,
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id IN (%s, %s, %s, %s)",
                user_ids,
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
