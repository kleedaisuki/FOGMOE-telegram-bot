"""@brief PostgreSQL 猜拳原子适配器测试 / Tests for the PostgreSQL atomic RPS adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

from fogmoe_bot.application.games.rps_operations import (
    RpsMatchCode,
    RpsMutationCode,
)
from fogmoe_bot.domain.games import (
    Choice,
    GameId,
    GameSession,
    Player,
    UserId,
    WaitingRoom,
)
from fogmoe_bot.infrastructure.database import rps_ledger
from fogmoe_bot.infrastructure.database.repositories.user_repository import UserAccount
from fogmoe_bot.infrastructure.database.rps_ledger import PostgresRpsLedger
from fogmoe_bot.infrastructure.database.rps_codec import encode_session


class RecordingTransaction:
    """@brief 记录事务退出状态 / Record transaction exit state."""

    def __init__(self) -> None:
        """@brief 初始化事务记录 / Initialize transaction recording."""

        self.connection = object()
        self.exit_exception: type[BaseException] | None = None

    async def __aenter__(self) -> object:
        """@brief 进入事务 / Enter the transaction."""

        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """@brief 记录异常且不吞掉 / Record and propagate an exception."""

        del exc, traceback
        self.exit_exception = exc_type
        return False


def _account(user_id: int, coins: int) -> UserAccount:
    """@brief 创建账户快照 / Build an account snapshot."""

    return UserAccount(
        user_id=user_id,
        permission=0,
        coins=coins,
        coins_paid=0,
        permanent_records_limit=100,
        info="",
    )


def _room_and_session() -> tuple[WaitingRoom, GameSession]:
    """@brief 创建确定性等待房间和直接后继会话 / Build a deterministic room and direct successor."""

    now = datetime(2026, 7, 12, tzinfo=UTC)
    room = WaitingRoom.open(
        GameId("game_atomic1"),
        Player(UserId(20), "host"),
        now=now,
        wait_for=timedelta(minutes=10),
    )
    session = room.join(
        Player(UserId(10), "guest"),
        expected_version=room.version,
        now=now + timedelta(seconds=1),
        choose_for=timedelta(minutes=2),
    )
    return room, session


def test_start_game_locks_both_accounts_before_writes_and_commits_one_state(
    monkeypatch: Any,
) -> None:
    """@brief 匹配按稳定顺序锁定双方后才扣费并提交状态 / Matching locks both users stably before charging and committing state."""

    async def scenario() -> None:
        """@brief 驱动原子匹配 / Drive an atomic match."""

        transaction = RecordingTransaction()
        room, session = _room_and_session()
        reads: list[int] = []
        writes: list[int] = []
        updates: list[str] = []

        async def fake_lock_session(game_id: GameId, connection: object) -> Any:
            """@brief 返回锁定的等待行 / Return a locked waiting row."""

            assert game_id == room.game_id
            assert connection is transaction.connection
            return {"game_id": str(game_id), "status": "waiting", "version": 0}

        async def fake_fetch_account(
            user_id: int,
            *,
            connection: object,
            for_update: bool,
        ) -> UserAccount:
            """@brief 记录账户锁顺序 / Record account-lock order."""

            assert connection is transaction.connection
            assert for_update is True
            reads.append(user_id)
            return _account(user_id, 3)

        async def fake_set(
            user_id: int,
            coins: int,
            coins_paid: int,
            user_plan: str,
            *,
            connection: object,
        ) -> None:
            """@brief 记录扣费写入 / Record charge writes."""

            assert connection is transaction.connection
            assert (coins, coins_paid, user_plan) == (2, 0, "free")
            writes.append(user_id)

        async def fake_fetch_one(
            sql: str,
            params: object,
            *,
            connection: object,
        ) -> tuple[int]:
            """@brief 接受 guest 槽插入 / Accept the guest-slot insert."""

            del params
            assert "rps_player_slots" in sql
            assert connection is transaction.connection
            return (10,)

        async def fake_execute(
            sql: str,
            params: object,
            *,
            connection: object,
        ) -> int:
            """@brief 接受会话 CAS / Accept the session CAS."""

            del params
            assert connection is transaction.connection
            updates.append(sql)
            return 1

        monkeypatch.setattr(
            rps_ledger.db_connection,
            "transaction",
            lambda: transaction,
        )
        monkeypatch.setattr(rps_ledger, "_lock_session", fake_lock_session)
        monkeypatch.setattr(
            rps_ledger.user_repository,
            "fetch_user_account",
            fake_fetch_account,
        )
        monkeypatch.setattr(
            rps_ledger.user_repository,
            "set_coin_balances_and_plan",
            fake_set,
        )
        monkeypatch.setattr(rps_ledger.db_connection, "fetch_one", fake_fetch_one)
        monkeypatch.setattr(rps_ledger.db_connection, "execute", fake_execute)

        result = await PostgresRpsLedger().start_game(
            room,
            session,
            started_at=session.started_at,
        )

        assert result.code is RpsMatchCode.STARTED
        assert reads == [10, 20]
        assert set(writes) == {10, 20}
        assert len(updates) == 1
        assert transaction.exit_exception is None

    asyncio.run(scenario())


def test_terminal_choice_replay_does_not_pay_twice(monkeypatch: Any) -> None:
    """@brief 终局 CAS 重放不会重复发奖 / Replaying a terminal CAS does not pay twice."""

    async def scenario() -> None:
        """@brief 连续提交同一终局选择 / Commit the same terminal choice twice."""

        room, session = _room_and_session()
        first_choice = session.choose(
            session.player_one.user_id,
            Choice.ROCK,
            expected_version=session.version,
            now=session.started_at + timedelta(seconds=1),
        )
        finished = first_choice.choose(
            session.player_two.user_id,
            Choice.SCISSORS,
            expected_version=first_choice.version,
            now=session.started_at + timedelta(seconds=2),
        )
        rows = iter(
            (
                {
                    "game_id": str(room.game_id),
                    "status": "choosing",
                    "version": first_choice.version.value,
                },
                {
                    "game_id": str(room.game_id),
                    "status": "finished",
                    "version": finished.version.value,
                    "state": encode_session(finished),
                },
            )
        )
        paid = 0

        async def fake_lock_session(game_id: GameId, connection: object) -> Any:
            """@brief 首次返回活动行，重放返回终态行 / Return active then terminal state."""

            del game_id, connection
            return next(rows)

        async def fake_credit(payouts: object, connection: object) -> None:
            """@brief 记录一次奖金应用 / Record one payout application."""

            del payouts, connection
            nonlocal paid
            paid += 1

        async def fake_execute(
            sql: str,
            params: object,
            *,
            connection: object,
        ) -> int:
            """@brief 接受终局 CAS 与槽删除 / Accept terminal CAS and slot deletion."""

            del sql, params, connection
            return 1

        monkeypatch.setattr(
            rps_ledger.db_connection,
            "transaction",
            lambda: RecordingTransaction(),
        )
        monkeypatch.setattr(rps_ledger, "_lock_session", fake_lock_session)
        monkeypatch.setattr(rps_ledger, "_credit", fake_credit)
        monkeypatch.setattr(rps_ledger.db_connection, "execute", fake_execute)

        adapter = PostgresRpsLedger()
        applied = await adapter.commit_choice(
            first_choice,
            finished,
            committed_at=session.started_at + timedelta(seconds=2),
        )
        replay = await adapter.commit_choice(
            first_choice,
            finished,
            committed_at=session.started_at + timedelta(seconds=3),
        )

        assert applied.code is RpsMutationCode.APPLIED
        assert replay.code is RpsMutationCode.APPLIED
        assert paid == 1

    asyncio.run(scenario())
