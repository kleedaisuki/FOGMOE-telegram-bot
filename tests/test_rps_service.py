"""@brief 猜拳有界应用服务与并发测试 / Tests for the bounded concurrent RPS application service."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import fogmoe_bot.application.games.rps_service as service_module
from fogmoe_bot.application.games.rps_service import (
    ChoiceRecorded,
    GameCancelled,
    GameDelivery,
    GameSettled,
    MatchStarted,
    MessageAddress,
    PlayerMessage,
    Rejected,
    RejectionCode,
    RpsService,
    ServiceState,
    WaitingCancelled,
    WaitingCreated,
)
from fogmoe_bot.application.games.rps_operations import (
    RestoredGame,
    RestoredWaiting,
    RpsMatchCode,
    RpsMatchResult,
    RpsMutationCode,
    RpsMutationResult,
    RpsRecoveryState,
    WaitingTerminalStatus,
)
from fogmoe_bot.domain.games import (
    AccountStatus,
    Choice,
    GameCancellation,
    GameId,
    GameSession,
    GameVersion,
    Payout,
    Player,
    UserId,
    WaitingRoom,
)


class FakeLedger:
    """@brief 内存测试结算端口 / In-memory test settlement port."""

    def __init__(self, balances: dict[UserId, int]) -> None:
        """@brief 创建余额账本 / Create a balance ledger.

        @param balances 初始余额 / Initial balances.
        @return None / None.
        """

        self.balances = dict(balances)
        """@brief 当前余额 / Current balances."""
        self.credit_gate: asyncio.Event | None = None
        """@brief 可选结算阻塞门 / Optional payout blocking gate."""
        self.charge_gate: asyncio.Event | None = None
        """@brief 可选入场扣费阻塞门 / Optional entry-charge blocking gate."""
        self.charge_entered = asyncio.Event()
        """@brief 入场扣费已开始通知 / Notification that entry charging began."""
        self.credit_started = 0
        """@brief 已进入结算调用数 / Number of entered payout calls."""
        self.active_credits = 0
        """@brief 当前并发结算数 / Current concurrent payout count."""
        self.maximum_active_credits = 0
        """@brief 观察到的最大并发结算数 / Maximum observed concurrent payout count."""
        self.waiting: RestoredWaiting | None = None
        """@brief 持久化等待房间 / Persisted waiting room."""
        self.games: dict[GameId, RestoredGame] = {}
        """@brief 持久化活动对局 / Persisted active games."""
        self.tombstones: dict[GameId, GameVersion] = {}
        """@brief 持久化终态版本 / Persisted terminal versions."""

    async def status(self, user_id: UserId) -> AccountStatus:
        """@brief 读取内存账户状态 / Read in-memory account status.

        @param user_id 玩家身份 / Player identity.
        @return 账户状态 / Account status.
        """

        return AccountStatus(user_id in self.balances, self.balances.get(user_id, 0))

    async def load_recovery_state(self, *, tombstone_limit: int) -> RpsRecoveryState:
        """@brief 返回内存耐久快照 / Return the in-memory durable snapshot."""

        tombstones = tuple(self.tombstones.items())[-tombstone_limit:]
        return RpsRecoveryState(
            self.waiting,
            tuple(self.games.values()),
            tombstones,
        )

    async def create_waiting(self, room: WaitingRoom) -> bool:
        """@brief 创建唯一等待房间 / Create the sole waiting room."""

        if self.waiting is not None:
            return False
        active_players = {
            player.user_id
            for restored in self.games.values()
            for player in restored.session.players
        }
        if room.host.user_id in active_players:
            return False
        self.waiting = RestoredWaiting(room, None)
        return True

    async def finish_waiting(
        self,
        room: WaitingRoom,
        status: WaitingTerminalStatus,
        *,
        finished_at: datetime,
    ) -> RpsMutationResult:
        """@brief 结束等待房间 / Finish a waiting room."""

        del status, finished_at
        current = self.waiting
        if current is None:
            return RpsMutationResult(RpsMutationCode.NOT_FOUND)
        if current.room.game_id != room.game_id or current.room.version != room.version:
            return RpsMutationResult(
                RpsMutationCode.STALE,
                current.room.version,
            )
        self.waiting = None
        self.tombstones[room.game_id] = room.version
        return RpsMutationResult(RpsMutationCode.APPLIED, room.version)

    async def start_game(
        self,
        room: WaitingRoom,
        session: GameSession,
        *,
        started_at: datetime,
    ) -> RpsMatchResult:
        """@brief 原子匹配并扣除双方入场费 / Atomically match and charge both entries."""

        del started_at
        self.charge_entered.set()
        if self.charge_gate is not None:
            await self.charge_gate.wait()
        current = self.waiting
        if current is None:
            return RpsMatchResult(RpsMatchCode.NOT_FOUND)
        if current.room.game_id != room.game_id or current.room.version != room.version:
            return RpsMatchResult(RpsMatchCode.STALE, current.room.version)
        first = session.player_one.user_id
        second = session.player_two.user_id
        if self.balances.get(first, 0) < 1:
            self.waiting = None
            self.tombstones[room.game_id] = room.version
            return RpsMatchResult(RpsMatchCode.FIRST_UNAVAILABLE, room.version)
        if self.balances.get(second, 0) < 1:
            return RpsMatchResult(RpsMatchCode.SECOND_UNAVAILABLE, room.version)
        if any(
            second in {player.user_id for player in restored.session.players}
            for restored in self.games.values()
        ):
            return RpsMatchResult(RpsMatchCode.PLAYER_BUSY, room.version)
        self.balances[first] -= 1
        self.balances[second] -= 1
        self.waiting = None
        self.games[session.game_id] = RestoredGame(session, None)
        return RpsMatchResult(RpsMatchCode.STARTED, session.version, session)

    async def commit_choice(
        self,
        previous: GameSession,
        updated: GameSession,
        *,
        committed_at: datetime,
    ) -> RpsMutationResult:
        """@brief 持久化选择并结算终局 / Persist a choice and settle a terminal game."""

        del committed_at
        restored = self.games.get(previous.game_id)
        if restored is None:
            return RpsMutationResult(RpsMutationCode.NOT_FOUND)
        if restored.session.version != previous.version:
            return RpsMutationResult(
                RpsMutationCode.STALE,
                restored.session.version,
            )
        if updated.outcome is not None:
            await self._credit(updated.outcome.payouts)
            self.games.pop(updated.game_id)
            self.tombstones[updated.game_id] = updated.version
        else:
            self.games[updated.game_id] = RestoredGame(updated, restored.delivery)
        return RpsMutationResult(RpsMutationCode.APPLIED, updated.version)

    async def cancel_game(
        self,
        previous: GameSession,
        cancelled: GameSession,
        *,
        committed_at: datetime,
    ) -> RpsMutationResult:
        """@brief 持久化取消并退款 / Persist cancellation and refunds."""

        del committed_at
        restored = self.games.get(previous.game_id)
        if restored is None:
            return RpsMutationResult(RpsMutationCode.NOT_FOUND)
        if restored.session.version != previous.version:
            return RpsMutationResult(
                RpsMutationCode.STALE,
                restored.session.version,
            )
        await self._credit(cancelled.refunds)
        self.games.pop(cancelled.game_id)
        self.tombstones[cancelled.game_id] = cancelled.version
        return RpsMutationResult(RpsMutationCode.APPLIED, cancelled.version)

    async def bind_waiting_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        invitation: MessageAddress,
    ) -> bool:
        """@brief 绑定等待邀请地址 / Bind a waiting invitation."""

        current = self.waiting
        if (
            current is None
            or current.room.game_id != game_id
            or current.room.version != expected_version
        ):
            return False
        self.waiting = RestoredWaiting(current.room, invitation)
        return True

    async def bind_game_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        delivery: GameDelivery,
    ) -> bool:
        """@brief 绑定活动对局投递地址 / Bind active-game delivery addresses."""

        current = self.games.get(game_id)
        if current is None or current.session.version != expected_version:
            return False
        self.games[game_id] = RestoredGame(current.session, delivery)
        return True

    async def _credit(self, payouts: tuple[Payout, ...]) -> None:
        """@brief 应用结算并记录并发度 / Apply payouts while recording concurrency.

        @param payouts 领域结算 / Domain payouts.
        @return None / None.
        """

        self.credit_started += 1
        self.active_credits += 1
        self.maximum_active_credits = max(
            self.maximum_active_credits, self.active_credits
        )
        try:
            if self.credit_gate is not None:
                await self.credit_gate.wait()
            for payout in payouts:
                self.balances[payout.user_id] = (
                    self.balances.get(payout.user_id, 0) + payout.coins
                )
        finally:
            self.active_credits -= 1


class TransientWaitingFinishLedger(FakeLedger):
    """@brief 首次等待过期提交失败的账本 / Ledger whose first waiting-expiry commit fails."""

    def __init__(self, balances: dict[UserId, int]) -> None:
        """@brief 初始化瞬态故障计数 / Initialize the transient-failure counter.

        @param balances 初始余额 / Initial balances.
        """

        super().__init__(balances)
        self.finish_waiting_attempts = 0
        """@brief 等待终态提交次数 / Number of waiting-terminal commit attempts."""

    async def finish_waiting(
        self,
        room: WaitingRoom,
        status: WaitingTerminalStatus,
        *,
        finished_at: datetime,
    ) -> RpsMutationResult:
        """@brief 首次抛出瞬态错误，后续正常提交 / Raise transiently once, then commit normally."""

        self.finish_waiting_attempts += 1
        if self.finish_waiting_attempts == 1:
            raise RuntimeError("transient waiting expiry failure")
        return await super().finish_waiting(
            room,
            status,
            finished_at=finished_at,
        )


class RecordingSink:
    """@brief 记录 deadline 生命周期事件的测试端口 / Test port recording deadline lifecycle events."""

    def __init__(self) -> None:
        """@brief 初始化事件记录 / Initialize event recording.

        @return None / None.
        """

        self.waiting_events: list[WaitingCancelled] = []
        """@brief 等待过期事件 / Waiting-expiration events."""
        self.game_events: list[GameCancelled] = []
        """@brief 对局取消事件 / Game-cancellation events."""
        self.game_event = asyncio.Event()
        """@brief 首个对局取消通知 / Notification for first game cancellation."""

    async def waiting_expired(self, event: WaitingCancelled) -> None:
        """@brief 记录等待过期 / Record waiting expiration.

        @param event 等待过期事件 / Waiting-expiration event.
        @return None / None.
        """

        self.waiting_events.append(event)

    async def game_cancelled(self, event: GameCancelled) -> None:
        """@brief 记录对局取消 / Record game cancellation.

        @param event 对局取消事件 / Game-cancellation event.
        @return None / None.
        """

        self.game_events.append(event)
        self.game_event.set()


class SequentialIds:
    """@brief 产生确定性 callback-safe 游戏身份 / Produce deterministic callback-safe game identities."""

    def __init__(self) -> None:
        """@brief 初始化序号 / Initialize the sequence.

        @return None / None.
        """

        self._next = 0
        """@brief 下一序号 / Next sequence."""

    def __call__(self) -> GameId:
        """@brief 返回下一游戏身份 / Return the next game identity.

        @return 确定性游戏身份 / Deterministic game identity.
        """

        value = GameId(f"game_{self._next:04d}")
        self._next += 1
        return value


class FailingSupervisorRpsService(RpsService):
    """@brief 注入 deadline 监督器故障的服务 / Service injecting a deadline-supervisor failure."""

    async def _deadline_loop(self) -> None:
        """@brief 在一次调度后抛出监督器故障 / Raise a supervisor failure after one scheduling turn.

        @return 不返回 / Does not return.
        @raises RuntimeError 固定测试故障 / Fixed test failure.
        """

        await asyncio.sleep(0)
        raise RuntimeError("deadline supervisor failed")


def _players(*values: int) -> tuple[Player, ...]:
    """@brief 由整数创建测试玩家 / Build test players from integers.

    @param values 用户标识 / User identifiers.
    @return 玩家元组 / Player tuple.
    """

    return tuple(Player(UserId(value), f"user{value}") for value in values)


def _delivery(session_players: Iterable[Player], game_number: int) -> GameDelivery:
    """@brief 创建一局测试投递地址 / Build test delivery addresses for one game.

    @param session_players 两位玩家 / Two players.
    @param game_number 消息序号基数 / Message-sequence base.
    @return 完整投递地址 / Complete delivery addresses.
    """

    first, second = tuple(session_players)
    return GameDelivery(
        announcement=MessageAddress(-100, game_number),
        player_messages=(
            PlayerMessage(
                first.user_id, MessageAddress(int(first.user_id), game_number + 1)
            ),
            PlayerMessage(
                second.user_id, MessageAddress(int(second.user_id), game_number + 2)
            ),
        ),
    )


async def _start_match(
    service: RpsService,
    first: Player,
    second: Player,
    *,
    message_number: int,
) -> MatchStarted:
    """@brief 创建、匹配并绑定一局测试游戏 / Open, match, and bind one test game.

    @param service 猜拳应用服务 / RPS service.
    @param first 第一位玩家 / First player.
    @param second 第二位玩家 / Second player.
    @param message_number 投递消息序号 / Delivery message sequence.
    @return 已绑定匹配结果 / Bound match result.
    """

    waiting = await service.request_game(first)
    assert isinstance(waiting, WaitingCreated)
    assert await service.bind_waiting_delivery(
        waiting.room.game_id,
        waiting.room.version,
        MessageAddress(-100, message_number),
    )
    match = await service.request_game(second)
    assert isinstance(match, MatchStarted)
    assert await service.bind_game_delivery(
        match.session.game_id,
        match.session.version,
        _delivery(match.session.players, message_number),
    )
    return match


async def _run_service(
    service: RpsService,
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """@brief 通过唯一 BackgroundService 契约运行测试服务 / Run a test service through the sole BackgroundService contract.

    @param service 猜拳应用服务 / RPS application service.
    @return 停止信号与结构化运行任务 / Stop signal and structured run task.
    """

    stop_event = asyncio.Event()
    run_task = asyncio.create_task(service.run(stop_event))
    while service.state is ServiceState.NEW:
        await asyncio.sleep(0)
    assert service.state is ServiceState.RUNNING
    return stop_event, run_task


async def _stop_service(
    stop_event: asyncio.Event,
    run_task: asyncio.Task[None],
) -> None:
    """@brief 请求正常停止并等待排空 / Request normal stop and await draining.

    @param stop_event 停止信号 / Stop signal.
    @param run_task 服务运行任务 / Service run task.
    @return None / None.
    """

    stop_event.set()
    await run_task


def test_run_stops_immediately_when_the_runtime_signal_is_already_set() -> None:
    """@brief 已置位停止信号不会启动游离监督器 / An already-set stop signal leaves no detached supervisor."""

    async def scenario() -> None:
        """@brief 以早停信号运行一次服务 / Run one service with an early stop signal.

        @return None / None.
        """

        service = RpsService(ledger=FakeLedger({}))
        stop_event = asyncio.Event()
        stop_event.set()

        await service.run(stop_event)

        assert service.state is ServiceState.CLOSED
        assert service.session_count == 0

    asyncio.run(scenario())


def test_run_rejects_concurrent_and_post_close_reuse() -> None:
    """@brief 同一实例不能并发运行或关闭后复用 / One instance rejects concurrent runs and post-close reuse."""

    async def scenario() -> None:
        """@brief 验证一次性生命周期 / Verify the one-shot lifecycle.

        @return None / None.
        """

        service = RpsService(ledger=FakeLedger({}))
        stop_event, run_task = await _run_service(service)

        with pytest.raises(RuntimeError, match="cannot run from running"):
            await service.run(asyncio.Event())
        await _stop_service(stop_event, run_task)
        with pytest.raises(RuntimeError, match="cannot run from closed"):
            await service.run(asyncio.Event())

    asyncio.run(scenario())


def test_deadline_supervisor_failure_propagates_after_structured_cleanup() -> None:
    """@brief TaskGroup 子任务故障向拥有者传播且服务终结 / A TaskGroup child failure propagates and closes the service."""

    async def scenario() -> None:
        """@brief 注入监督器故障 / Inject a supervisor failure.

        @return None / None.
        """

        service = FailingSupervisorRpsService(ledger=FakeLedger({}))

        with pytest.raises(ExceptionGroup) as raised:
            await service.run(asyncio.Event())

        assert any(
            isinstance(error, RuntimeError)
            and str(error) == "deadline supervisor failed"
            for error in raised.value.exceptions
        )
        assert service.state is ServiceState.CLOSED

    asyncio.run(scenario())


def test_registry_capacity_and_player_index_are_explicit_rejections() -> None:
    """@brief 有界容量与唯一玩家索引不会退化成隐式字典行为 / Bounded capacity and unique player indexing return explicit rejections."""

    async def scenario() -> None:
        """@brief 驱动容量场景 / Drive the capacity scenario.

        @return None / None.
        """

        players = _players(1, 2, 3)
        ledger = FakeLedger({player.user_id: 5 for player in players})
        service = RpsService(
            ledger=ledger,
            max_sessions=1,
            game_id_factory=SequentialIds(),
        )
        stop_event, run_task = await _run_service(service)
        try:
            match = await _start_match(
                service, players[0], players[1], message_number=10
            )
            duplicate = await service.request_game(players[0])
            full = await service.request_game(players[2])
            assert isinstance(duplicate, Rejected)
            assert duplicate.code is RejectionCode.ALREADY_IN_GAME
            assert duplicate.game_id == match.session.game_id
            assert isinstance(full, Rejected)
            assert full.code is RejectionCode.CAPACITY_REACHED
            assert service.session_count == 1
        finally:
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_same_game_callbacks_linearize_and_reject_the_losing_stale_version() -> None:
    """@brief 同局并发 callback 只接受一个版本转移 / Concurrent callbacks for one game accept only one version transition."""

    async def scenario() -> None:
        """@brief 驱动同局竞争 / Drive the same-game race.

        @return None / None.
        """

        first, second = _players(11, 22)
        ledger = FakeLedger({first.user_id: 5, second.user_id: 5})
        service = RpsService(ledger=ledger, game_id_factory=SequentialIds())
        stop_event, run_task = await _run_service(service)
        try:
            match = await _start_match(service, first, second, message_number=20)
            results = await asyncio.gather(
                service.choose(
                    first.user_id,
                    match.session.game_id,
                    match.session.version,
                    Choice.ROCK,
                ),
                service.choose(
                    second.user_id,
                    match.session.game_id,
                    match.session.version,
                    Choice.PAPER,
                ),
            )
            assert sum(isinstance(result, ChoiceRecorded) for result in results) == 1
            stale = next(result for result in results if isinstance(result, Rejected))
            assert stale.code is RejectionCode.STALE_VERSION
            assert stale.current_version == GameVersion(2)
        finally:
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_distinct_games_settle_concurrently_instead_of_sharing_a_global_lock() -> None:
    """@brief 不同游戏可并发结算而非共享全局锁 / Distinct games settle concurrently instead of sharing one global lock."""

    async def scenario() -> None:
        """@brief 驱动跨局并发结算 / Drive cross-game concurrent settlement.

        @return None / None.
        """

        first, second, third, fourth = _players(31, 32, 33, 34)
        ledger = FakeLedger(
            {player.user_id: 5 for player in (first, second, third, fourth)}
        )
        service = RpsService(
            ledger=ledger,
            max_sessions=4,
            game_id_factory=SequentialIds(),
        )
        stop_event, run_task = await _run_service(service)
        try:
            match_one = await _start_match(service, first, second, message_number=30)
            match_two = await _start_match(service, third, fourth, message_number=40)
            first_one = await service.choose(
                first.user_id,
                match_one.session.game_id,
                match_one.session.version,
                Choice.ROCK,
            )
            first_two = await service.choose(
                third.user_id,
                match_two.session.game_id,
                match_two.session.version,
                Choice.PAPER,
            )
            assert isinstance(first_one, ChoiceRecorded)
            assert isinstance(first_two, ChoiceRecorded)

            ledger.credit_gate = asyncio.Event()
            settle_one = asyncio.create_task(
                service.choose(
                    second.user_id,
                    match_one.session.game_id,
                    first_one.session.version,
                    Choice.SCISSORS,
                )
            )
            settle_two = asyncio.create_task(
                service.choose(
                    fourth.user_id,
                    match_two.session.game_id,
                    first_two.session.version,
                    Choice.ROCK,
                )
            )
            async with asyncio.timeout(1):
                while ledger.active_credits < 2:
                    await asyncio.sleep(0)
            assert ledger.maximum_active_credits == 2
            ledger.credit_gate.set()
            results = await asyncio.gather(settle_one, settle_two)
            assert all(isinstance(result, GameSettled) for result in results)
        finally:
            if ledger.credit_gate is not None:
                ledger.credit_gate.set()
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_slow_match_charge_does_not_hold_the_global_registry_lock() -> None:
    """@brief 一个等待槽的慢数据库扣费不阻塞无关活动游戏 / Slow DB charging for one waiting slot does not block an unrelated active game."""

    async def scenario() -> None:
        """@brief 驱动匹配 reservation 与无关选择并发 / Drive match reservation concurrently with an unrelated choice.

        @return None / None.
        """

        first, second, third, fourth = _players(61, 62, 63, 64)
        ledger = FakeLedger(
            {player.user_id: 5 for player in (first, second, third, fourth)}
        )
        service = RpsService(
            ledger=ledger,
            max_sessions=4,
            game_id_factory=SequentialIds(),
        )
        stop_event, run_task = await _run_service(service)
        matching_task: asyncio.Task[object] | None = None
        try:
            active = await _start_match(service, first, second, message_number=70)
            waiting = await service.request_game(third)
            assert isinstance(waiting, WaitingCreated)

            ledger.charge_entered.clear()
            ledger.charge_gate = asyncio.Event()
            matching_task = asyncio.create_task(service.request_game(fourth))
            async with asyncio.timeout(1):
                await ledger.charge_entered.wait()

            async with asyncio.timeout(0.2):
                choice = await service.choose(
                    first.user_id,
                    active.session.game_id,
                    active.session.version,
                    Choice.ROCK,
                )
            assert isinstance(choice, ChoiceRecorded)

            ledger.charge_gate.set()
            match = await matching_task
            assert isinstance(match, MatchStarted)
        finally:
            if ledger.charge_gate is not None:
                ledger.charge_gate.set()
            if matching_task is not None:
                await matching_task
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_host_cancellation_wins_the_match_cas_and_releases_the_guest_reservation() -> (
    None
):
    """@brief 房主取消可赢得匹配 CAS，已扣金币获补偿且 guest reservation 释放 / Host cancellation can win the match CAS, compensate charges, and release the guest reservation."""

    async def scenario() -> None:
        """@brief 在扣费阻塞时取消等待房间 / Cancel the waiting room while charging is blocked.

        @return None / None.
        """

        host, guest = _players(71, 72)
        ledger = FakeLedger({host.user_id: 3, guest.user_id: 3})
        service = RpsService(ledger=ledger, game_id_factory=SequentialIds())
        stop_event, run_task = await _run_service(service)
        matching_task: asyncio.Task[object] | None = None
        try:
            waiting = await service.request_game(host)
            assert isinstance(waiting, WaitingCreated)
            ledger.charge_entered.clear()
            ledger.charge_gate = asyncio.Event()
            matching_task = asyncio.create_task(service.request_game(guest))
            async with asyncio.timeout(1):
                await ledger.charge_entered.wait()

            cancelled = await service.cancel_waiting(
                host.user_id,
                waiting.room.game_id,
                waiting.room.version,
            )
            assert isinstance(cancelled, WaitingCancelled)
            ledger.charge_gate.set()
            stale_match = await matching_task
            assert isinstance(stale_match, Rejected)
            assert stale_match.code is RejectionCode.STALE_VERSION
            assert ledger.balances == {host.user_id: 3, guest.user_id: 3}

            guest_waiting = await service.request_game(guest)
            assert isinstance(guest_waiting, WaitingCreated)
        finally:
            if ledger.charge_gate is not None:
                ledger.charge_gate.set()
            if matching_task is not None:
                await matching_task
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_shutdown_waits_for_inflight_match_compensation_before_closing() -> None:
    """@brief shutdown 等待锁外扣费完成补偿后才进入 CLOSED / Shutdown waits for out-of-lock charge compensation before becoming CLOSED."""

    async def scenario() -> None:
        """@brief 在扣费阻塞期间启动 shutdown / Start shutdown while entry charging is blocked.

        @return None / None.
        """

        host, guest = _players(81, 82)
        ledger = FakeLedger({host.user_id: 2, guest.user_id: 2})
        service = RpsService(ledger=ledger, game_id_factory=SequentialIds())
        stop_event, run_task = await _run_service(service)
        waiting = await service.request_game(host)
        assert isinstance(waiting, WaitingCreated)
        ledger.charge_entered.clear()
        ledger.charge_gate = asyncio.Event()
        matching_task = asyncio.create_task(service.request_game(guest))
        async with asyncio.timeout(1):
            await ledger.charge_entered.wait()

        stop_event.set()
        await asyncio.sleep(0)
        assert not run_task.done()
        ledger.charge_gate.set()
        match_result, _shutdown_result = await asyncio.gather(
            matching_task,
            run_task,
        )

        assert isinstance(match_result, Rejected)
        assert match_result.code is RejectionCode.SERVICE_UNAVAILABLE
        assert service.state is ServiceState.CLOSED
        assert ledger.balances == {host.user_id: 2, guest.user_id: 2}

    asyncio.run(scenario())


def test_timeout_supervisor_cancels_refunds_notifies_and_leaves_a_stale_tombstone() -> (
    None
):
    """@brief 单一 timeout 监督器取消、退款、通知并保留有界墓碑 / The sole timeout supervisor cancels, refunds, notifies, and leaves a bounded tombstone."""

    async def scenario() -> None:
        """@brief 驱动短 deadline 场景 / Drive a short-deadline scenario.

        @return None / None.
        """

        first, second = _players(41, 42)
        ledger = FakeLedger({first.user_id: 3, second.user_id: 3})
        sink = RecordingSink()
        service = RpsService(
            ledger=ledger,
            lifecycle_sink=sink,
            choice_timeout=timedelta(milliseconds=20),
            game_id_factory=SequentialIds(),
        )
        stop_event, run_task = await _run_service(service)
        try:
            match = await _start_match(service, first, second, message_number=50)
            assert ledger.balances == {first.user_id: 2, second.user_id: 2}
            async with asyncio.timeout(1):
                await sink.game_event.wait()
            assert len(sink.game_events) == 1
            cancelled = sink.game_events[0]
            assert cancelled.session.cancellation is GameCancellation.TIMEOUT
            assert ledger.balances == {first.user_id: 3, second.user_id: 3}
            stale = await service.choose(
                first.user_id,
                match.session.game_id,
                match.session.version,
                Choice.ROCK,
            )
            assert isinstance(stale, Rejected)
            assert stale.code is RejectionCode.STALE_VERSION
            assert service.session_count == 0
        finally:
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_waiting_expiry_retries_after_transient_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 等待房间过期的瞬态 DB 故障不会杀死监督器 / A transient waiting-expiry DB failure does not kill the supervisor."""

    monkeypatch.setattr(service_module, "_RETRY_FAILED_EXPIRY_AFTER", 0.001)

    async def scenario() -> None:
        """@brief 驱动一次失败后成功的等待过期 / Drive waiting expiry through one failure and a successful retry."""

        (host,) = _players(43)
        ledger = TransientWaitingFinishLedger({host.user_id: 3})
        sink = RecordingSink()
        service = RpsService(
            ledger=ledger,
            lifecycle_sink=sink,
            waiting_timeout=timedelta(milliseconds=10),
            game_id_factory=SequentialIds(),
        )
        stop_event, run_task = await _run_service(service)
        try:
            waiting = await service.request_game(host)
            assert isinstance(waiting, WaitingCreated)
            async with asyncio.timeout(1):
                while not sink.waiting_events:
                    await asyncio.sleep(0)
            assert ledger.finish_waiting_attempts == 2
            assert ledger.waiting is None
            assert service.session_count == 0
            assert not run_task.done()
        finally:
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_shutdown_owns_cancellation_and_leaves_no_detached_timeout_tasks() -> None:
    """@brief shutdown 退款活动游戏且实现中没有游离 create_task / Shutdown refunds active games and implementation has no detached create_task."""

    async def scenario() -> None:
        """@brief 驱动关停退款 / Drive shutdown refunding.

        @return None / None.
        """

        first, second = _players(51, 52)
        ledger = FakeLedger({first.user_id: 2, second.user_id: 2})
        sink = RecordingSink()
        service = RpsService(
            ledger=ledger,
            lifecycle_sink=sink,
            game_id_factory=SequentialIds(),
        )
        stop_event, run_task = await _run_service(service)
        await _start_match(service, first, second, message_number=60)
        await _stop_service(stop_event, run_task)

        assert service.state is ServiceState.CLOSED
        assert service.session_count == 0
        assert ledger.balances == {first.user_id: 2, second.user_id: 2}
        assert (
            sink.game_events[0].session.cancellation
            is GameCancellation.SERVICE_SHUTDOWN
        )

    asyncio.run(scenario())
    source = Path(service_module.__file__).read_text(encoding="utf-8")
    assert "asyncio.create_task(" not in source


def test_restart_recovers_active_version_delivery_and_settles_exactly_once() -> None:
    """@brief 重启恢复活动版本与投递地址且终局只结算一次 / Restart restores active version and delivery, then settles exactly once."""

    async def scenario() -> None:
        """@brief 由耐久快照启动新服务并完成对局 / Start a new service from a durable snapshot and finish the game."""

        first, second = _players(901, 902)
        now = datetime(2026, 7, 12, tzinfo=UTC)
        room = WaitingRoom.open(
            GameId("game_recover"),
            first,
            now=now,
            wait_for=timedelta(minutes=10),
        )
        session = room.join(
            second,
            expected_version=room.version,
            now=now + timedelta(seconds=1),
            choose_for=timedelta(minutes=2),
        )
        first_choice = session.choose(
            first.user_id,
            Choice.ROCK,
            expected_version=session.version,
            now=session.started_at + timedelta(seconds=1),
        )
        delivery = _delivery(first_choice.players, 900)
        ledger = FakeLedger({first.user_id: 2, second.user_id: 2})
        ledger.games[first_choice.game_id] = RestoredGame(first_choice, delivery)
        service = RpsService(
            ledger=ledger,
            clock=lambda: session.started_at + timedelta(seconds=2),
        )
        stop_event, run_task = await _run_service(service)
        try:
            settled = await service.choose(
                second.user_id,
                first_choice.game_id,
                first_choice.version,
                Choice.SCISSORS,
            )
            assert isinstance(settled, GameSettled)
            assert settled.delivery == delivery
            assert ledger.balances == {first.user_id: 4, second.user_id: 2}

            replay = await service.choose(
                second.user_id,
                first_choice.game_id,
                first_choice.version,
                Choice.SCISSORS,
            )
            assert isinstance(replay, Rejected)
            assert replay.code is RejectionCode.STALE_VERSION
            assert ledger.balances == {first.user_id: 4, second.user_id: 2}
        finally:
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_restart_immediately_refunds_an_expired_active_game_once() -> None:
    """@brief 重启立即回收过期活动局且退款幂等 / Restart immediately drains an expired active game with an idempotent refund."""

    async def scenario() -> None:
        """@brief 从已过期耐久快照启动监督器 / Start the supervisor from an expired durable snapshot."""

        first, second = _players(911, 912)
        now = datetime(2026, 7, 12, tzinfo=UTC)
        room = WaitingRoom.open(
            GameId("game_expired"),
            first,
            now=now,
            wait_for=timedelta(minutes=10),
        )
        session = room.join(
            second,
            expected_version=room.version,
            now=now + timedelta(seconds=1),
            choose_for=timedelta(seconds=5),
        )
        ledger = FakeLedger({first.user_id: 2, second.user_id: 2})
        ledger.games[session.game_id] = RestoredGame(
            session,
            _delivery(session.players, 910),
        )
        sink = RecordingSink()
        service = RpsService(
            ledger=ledger,
            lifecycle_sink=sink,
            clock=lambda: session.expires_at + timedelta(seconds=1),
        )
        stop_event, run_task = await _run_service(service)
        try:
            async with asyncio.timeout(1):
                await sink.game_event.wait()
            assert ledger.balances == {first.user_id: 3, second.user_id: 3}
            assert len(sink.game_events) == 1
            stale = await service.abort_game(session.game_id, session.version)
            assert isinstance(stale, Rejected)
            assert stale.code is RejectionCode.STALE_VERSION
            assert ledger.balances == {first.user_id: 3, second.user_id: 3}
        finally:
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())
