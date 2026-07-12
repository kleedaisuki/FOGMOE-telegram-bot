"""@brief RPS 崩溃恢复的真实 PostgreSQL 契约 / Real-PostgreSQL contract for RPS crash recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest

from fogmoe_bot.application.games.rps_service import (
    GameCancelled,
    GameDelivery,
    GameSettled,
    MessageAddress,
    PlayerMessage,
    RpsService,
    ServiceState,
    WaitingCancelled,
)
from fogmoe_bot.application.games.rps_operations import (
    RpsMatchCode,
    RpsMutationCode,
)
from fogmoe_bot.domain.games import (
    Choice,
    GameId,
    Player,
    UserId,
    WaitingRoom,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.rps_ledger import PostgresRpsLedger


class RecordingSink:
    """@brief 记录恢复后的超时事件 / Record timeout events after recovery."""

    def __init__(self) -> None:
        """@brief 初始化事件门 / Initialize the event gate."""

        self.cancelled: list[GameCancelled] = []
        self.event = asyncio.Event()

    async def waiting_expired(self, event: WaitingCancelled) -> None:
        """@brief 忽略等待过期 / Ignore waiting expiry."""

        del event

    async def game_cancelled(self, event: GameCancelled) -> None:
        """@brief 记录游戏取消 / Record game cancellation."""

        self.cancelled.append(event)
        self.event.set()


def _user_id(offset: int) -> int:
    """@brief 生成测试 BIGINT 用户 ID / Generate a test BIGINT user ID."""

    return 8_400_000_000_000_000_000 + int(uuid4().hex[:10], 16) * 10 + offset


async def _run_service(service: RpsService) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """@brief 启动服务并等待恢复完成 / Start a service and await recovery."""

    stop = asyncio.Event()
    task = asyncio.create_task(service.run(stop))
    async with asyncio.timeout(5):
        while service.state is ServiceState.NEW:
            await asyncio.sleep(0)
    return stop, task


def test_rps_match_settlement_timeout_and_restart_are_atomic() -> None:
    """@brief 匹配、结算、超时退款与重启恢复保持一次性 / Match, settlement, timeout refund, and restart recovery remain exactly once."""

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")

    async def scenario() -> None:
        """@brief 驱动两次跨服务实例生命周期 / Drive two cross-service-instance lifecycles."""

        first_id, second_id = _user_id(1), _user_id(2)
        users = (first_id, second_id)
        now = datetime.now(UTC)
        adapter = PostgresRpsLedger()
        first = Player(UserId(first_id), "first")
        second = Player(UserId(second_id), "second")
        try:
            await db_connection.execute(
                "INSERT INTO identity.users "
                "(id, tg_uid, provider, name, coins, coins_paid, user_plan) VALUES "
                "(%s, %s, 'telegram', %s, 3, 0, 'free'), "
                "(%s, %s, 'telegram', %s, 3, 0, 'free')",
                (
                    first_id,
                    first_id,
                    f"rps-first-{uuid4().hex}",
                    second_id,
                    second_id,
                    f"rps-second-{uuid4().hex}",
                ),
            )

            room = WaitingRoom.open(
                GameId(f"pg{uuid4().hex[:16]}"),
                first,
                now=now,
                wait_for=timedelta(minutes=10),
            )
            assert await adapter.create_waiting(room)
            invitation = MessageAddress(-100, 100)
            assert await adapter.bind_waiting_delivery(
                room.game_id,
                room.version,
                invitation,
            )
            session = room.join(
                second,
                expected_version=room.version,
                now=now + timedelta(seconds=1),
                choose_for=timedelta(minutes=2),
            )
            started = await adapter.start_game(
                room,
                session,
                started_at=session.started_at,
            )
            assert started.code is RpsMatchCode.STARTED
            recomputed = room.join(
                second,
                expected_version=room.version,
                now=now + timedelta(seconds=2),
                choose_for=timedelta(minutes=2),
            )
            match_replay = await adapter.start_game(
                room,
                recomputed,
                started_at=recomputed.started_at,
            )
            assert match_replay.code is RpsMatchCode.STARTED
            assert match_replay.session == session
            charged_once = await db_connection.fetch_all(
                "SELECT id, coins + coins_paid FROM identity.users "
                "WHERE id = ANY(%s) ORDER BY id",
                (users,),
            )
            assert [int(row[1]) for row in charged_once] == [2, 2]
            delivery = GameDelivery(
                announcement=invitation,
                player_messages=(
                    PlayerMessage(first.user_id, MessageAddress(first_id, 101)),
                    PlayerMessage(second.user_id, MessageAddress(second_id, 102)),
                ),
            )
            assert await adapter.bind_game_delivery(
                session.game_id,
                session.version,
                delivery,
            )
            first_choice = session.choose(
                first.user_id,
                Choice.ROCK,
                expected_version=session.version,
                now=session.started_at + timedelta(seconds=1),
            )
            persisted = await adapter.commit_choice(
                session,
                first_choice,
                committed_at=session.started_at + timedelta(seconds=1),
            )
            assert persisted.code is RpsMutationCode.APPLIED

            recovered_service = RpsService(
                ledger=PostgresRpsLedger(),
                clock=lambda: session.started_at + timedelta(seconds=2),
            )
            stop, task = await _run_service(recovered_service)
            settled = await recovered_service.choose(
                second.user_id,
                session.game_id,
                first_choice.version,
                Choice.SCISSORS,
            )
            assert isinstance(settled, GameSettled)
            stop.set()
            await task

            replay = await adapter.commit_choice(
                first_choice,
                settled.session,
                committed_at=session.started_at + timedelta(seconds=3),
            )
            assert replay.code is RpsMutationCode.APPLIED
            balances = await db_connection.fetch_all(
                "SELECT id, coins + coins_paid FROM identity.users "
                "WHERE id = ANY(%s) ORDER BY id",
                (users,),
            )
            expected = sorted(((first_id, 4), (second_id, 2)))
            assert [(int(row[0]), int(row[1])) for row in balances] == expected

            timeout_room = WaitingRoom.open(
                GameId(f"pg{uuid4().hex[:16]}"),
                first,
                now=now + timedelta(minutes=1),
                wait_for=timedelta(minutes=10),
            )
            assert await adapter.create_waiting(timeout_room)
            timeout_session = timeout_room.join(
                second,
                expected_version=timeout_room.version,
                now=now + timedelta(minutes=1, seconds=1),
                choose_for=timedelta(seconds=5),
            )
            timeout_started = await adapter.start_game(
                timeout_room,
                timeout_session,
                started_at=timeout_session.started_at,
            )
            assert timeout_started.code is RpsMatchCode.STARTED
            sink = RecordingSink()
            timeout_service = RpsService(
                ledger=PostgresRpsLedger(),
                lifecycle_sink=sink,
                clock=lambda: timeout_session.expires_at + timedelta(seconds=1),
            )
            timeout_stop, timeout_task = await _run_service(timeout_service)
            async with asyncio.timeout(5):
                await sink.event.wait()
            timeout_stop.set()
            await timeout_task
            assert len(sink.cancelled) == 1

            refund_replay = await adapter.cancel_game(
                timeout_session,
                sink.cancelled[0].session,
                committed_at=timeout_session.expires_at + timedelta(seconds=2),
            )
            assert refund_replay.code is RpsMutationCode.APPLIED
            balances = await db_connection.fetch_all(
                "SELECT id, coins + coins_paid FROM identity.users "
                "WHERE id = ANY(%s) ORDER BY id",
                (users,),
            )
            assert [(int(row[0]), int(row[1])) for row in balances] == expected
            recovery = await adapter.load_recovery_state(tombstone_limit=10)
            assert recovery.waiting is None
            assert recovery.games == ()
            assert {game_id for game_id, _version in recovery.tombstones} >= {
                session.game_id,
                timeout_session.game_id,
            }
        finally:
            await db_connection.execute(
                "DELETE FROM game.rps_sessions WHERE player_one_id = ANY(%s) "
                "OR player_two_id = ANY(%s)",
                (users, users),
            )
            await db_connection.execute(
                "DELETE FROM identity.users WHERE id = ANY(%s)",
                (users,),
            )
            await db.dispose_current_engine()

    asyncio.run(scenario())
