"""@brief 猜拳应用服务与有界会话注册表 / RPS application service and bounded session registry.

领域聚合保持纯净；本模块协调耐久事务、进程内活动索引、每局并发控制、单一超时
监督器以及确定性关停。/ Domain aggregates stay pure; this module coordinates durable
transactions, in-process active indices, per-game concurrency control, one timeout supervisor,
and deterministic shutdown.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
import secrets
from typing import Protocol, runtime_checkable

from fogmoe_bot.application.games.rps_delivery import (
    GameDelivery as GameDelivery,
    MessageAddress as MessageAddress,
    PlayerMessage as PlayerMessage,
)
from fogmoe_bot.application.games.rps_operations import (
    RpsMatchCode,
    RpsMutationCode,
    RpsOperations,
    WaitingTerminalStatus,
)

from fogmoe_bot.domain.games import (
    ENTRY_FEE,
    Choice,
    GameCancellation,
    GameId,
    GameSession,
    GameStatus,
    GameVersion,
    Player,
    RpsDomainError,
    StaleGameVersion,
    UserId,
    WaitingRoom,
)


RPS_SERVICE_DATA_KEY = "fogmoe.rps_service"
"""@brief 组合根保存猜拳服务的稳定键 / Stable composition-root key for the RPS service."""

DEFAULT_MAX_SESSIONS = 256
"""@brief 默认同时保留的等待房间与活动对局上限 / Default bound for waiting and active sessions."""

DEFAULT_MAX_TOMBSTONES = 512
"""@brief 默认终态版本墓碑上限 / Default bound for terminal-version tombstones."""

DEFAULT_WAITING_TIMEOUT = timedelta(minutes=10)
"""@brief 默认等待玩家时限 / Default opponent-wait timeout."""

DEFAULT_CHOICE_TIMEOUT = timedelta(minutes=2)
"""@brief 默认双方出招时限 / Default player-choice timeout."""

_RETRY_FAILED_EXPIRY_AFTER = 1.0
"""@brief 超时结算失败后的最小重试间隔 / Minimum retry delay after failed timeout settlement."""

logger = logging.getLogger(__name__)


type Clock = Callable[[], datetime]
"""@brief 返回带时区当前时间的时钟端口 / Clock port returning timezone-aware current time."""

type GameIdFactory = Callable[[], GameId]
"""@brief 创建 callback-safe 游戏身份的工厂端口 / Factory port for callback-safe game identities."""


class ServiceState(StrEnum):
    """@brief 猜拳应用服务生命周期 / RPS application-service lifecycle."""

    NEW = "new"
    """@brief 尚未启动 / Not started."""

    RUNNING = "running"
    """@brief 接受请求并监督超时 / Accepting requests and supervising deadlines."""

    CLOSING = "closing"
    """@brief 拒绝请求并退款清理 / Rejecting requests while refunding and cleaning up."""

    CLOSED = "closed"
    """@brief 已终止 / Terminated."""


class RejectionCode(StrEnum):
    """@brief 面向适配器的穷尽拒绝原因 / Exhaustive rejection reasons for adapters."""

    SERVICE_UNAVAILABLE = "service_unavailable"
    """@brief 服务未运行或正在关闭 / Service is not running."""

    NOT_REGISTERED = "not_registered"
    """@brief 玩家尚未注册 / Player is not registered."""

    INSUFFICIENT_COINS = "insufficient_coins"
    """@brief 玩家金币不足 / Player lacks entry coins."""

    ALREADY_WAITING = "already_waiting"
    """@brief 玩家已经创建等待房间 / Player already owns a waiting room."""

    ALREADY_IN_GAME = "already_in_game"
    """@brief 玩家已经参加活动对局 / Player already participates in an active game."""

    SELF_JOIN = "self_join"
    """@brief 房主尝试加入自己的邀请 / Host attempted to join their own invitation."""

    CAPACITY_REACHED = "capacity_reached"
    """@brief 有界注册表已满 / Bounded registry is full."""

    NOT_FOUND = "not_found"
    """@brief 游戏身份未知 / Game identity is unknown."""

    STALE_VERSION = "stale_version"
    """@brief callback 引用旧聚合版本 / Callback references an old aggregate version."""

    NOT_PARTICIPANT = "not_participant"
    """@brief 操作者不是本局玩家 / Actor is not a participant."""

    ALREADY_CHOSEN = "already_chosen"
    """@brief 玩家已经出招 / Player has already chosen."""

    GAME_NOT_READY = "game_not_ready"
    """@brief 选择消息尚未全部绑定 / Choice messages are not fully bound yet."""

    ROOM_INVALIDATED = "room_invalidated"
    """@brief 房主账户变化使邀请失效 / Host account changes invalidated the invitation."""


@dataclass(frozen=True, slots=True)
class Rejected:
    """@brief 可预期业务拒绝 / Expected business rejection.

    @param code 稳定拒绝原因 / Stable rejection reason.
    @param game_id 可选关联游戏 / Optional related game.
    @param current_version 可选最新版本，供 UI 恢复 / Optional latest version for UI recovery.
    """

    code: RejectionCode
    """@brief 拒绝原因 / Rejection reason."""

    game_id: GameId | None = None
    """@brief 关联游戏身份 / Related game identity."""

    current_version: GameVersion | None = None
    """@brief 当前聚合版本 / Current aggregate version."""


@dataclass(frozen=True, slots=True)
class WaitingCreated:
    """@brief 新等待房间结果 / Newly opened waiting-room result.

    @param room 领域等待房间 / Domain waiting room.
    """

    room: WaitingRoom
    """@brief 等待房间 / Waiting room."""


@dataclass(frozen=True, slots=True)
class MatchStarted:
    """@brief 成功匹配结果 / Successful match result.

    @param session 初始游戏会话 / Initial game session.
    @param invitation 原邀请消息 / Original invitation message.
    """

    session: GameSession
    """@brief 初始会话 / Initial session."""

    invitation: MessageAddress | None
    """@brief 原邀请地址 / Original invitation address."""


@dataclass(frozen=True, slots=True)
class WaitingCancelled:
    """@brief 等待房间取消结果 / Waiting-room cancellation result.

    @param room 被取消房间 / Cancelled room.
    @param invitation 原邀请地址 / Original invitation address.
    """

    room: WaitingRoom
    """@brief 被取消房间 / Cancelled room."""

    invitation: MessageAddress | None
    """@brief 原邀请地址 / Original invitation address."""


@dataclass(frozen=True, slots=True)
class WaitingInvalidated:
    """@brief 房主账户使邀请失效的结果 / Result when host account invalidates an invitation.

    @param room 已失效房间 / Invalidated room.
    @param invitation 原邀请地址 / Original invitation address.
    """

    room: WaitingRoom
    """@brief 已失效房间 / Invalidated room."""

    invitation: MessageAddress | None
    """@brief 原邀请地址 / Original invitation address."""


@dataclass(frozen=True, slots=True)
class ChoiceRecorded:
    """@brief 非终局选择已记录 / Non-terminal choice-recorded result.

    @param session 更新后的活动会话 / Updated active session.
    @param actor 出招玩家 / Acting player.
    @param delivery 当前投递地址 / Current delivery addresses.
    """

    session: GameSession
    """@brief 更新后会话 / Updated session."""

    actor: UserId
    """@brief 出招玩家 / Acting player."""

    delivery: GameDelivery | None
    """@brief 投递地址 / Delivery addresses."""


@dataclass(frozen=True, slots=True)
class GameSettled:
    """@brief 对局完成并已结算 / Game finished and payouts settled.

    @param session 完成会话 / Finished session.
    @param delivery 最终投递地址 / Final delivery addresses.
    """

    session: GameSession
    """@brief 完成会话 / Finished session."""

    delivery: GameDelivery | None
    """@brief 投递地址 / Delivery addresses."""


@dataclass(frozen=True, slots=True)
class GameCancelled:
    """@brief 对局取消且退款完成 / Game cancelled with refunds completed.

    @param session 已取消会话 / Cancelled session.
    @param delivery 投递地址 / Delivery addresses.
    """

    session: GameSession
    """@brief 已取消会话 / Cancelled session."""

    delivery: GameDelivery | None
    """@brief 投递地址 / Delivery addresses."""


type RequestGameResult = WaitingCreated | MatchStarted | WaitingInvalidated | Rejected
"""@brief `/rps_game` 请求的穷尽结果 / Exhaustive result of an `/rps_game` request."""

type JoinGameResult = MatchStarted | WaitingInvalidated | Rejected
"""@brief 加入 callback 的穷尽结果 / Exhaustive join-callback result."""

type CancelWaitingResult = WaitingCancelled | Rejected
"""@brief 取消等待 callback 的穷尽结果 / Exhaustive waiting-cancel result."""

type ChooseResult = ChoiceRecorded | GameSettled | Rejected
"""@brief 出招 callback 的穷尽结果 / Exhaustive choice result."""

type AbortGameResult = GameCancelled | Rejected
"""@brief 投递失败补偿的穷尽结果 / Exhaustive delivery-failure compensation result."""


@runtime_checkable
class RpsLifecycleSink(Protocol):
    """@brief 超时与关停事件的投递端口 / Delivery port for timeout and shutdown events."""

    async def waiting_expired(self, event: WaitingCancelled) -> None:
        """@brief 投递等待邀请过期 / Deliver waiting-invitation expiration.

        @param event 已移除等待房间 / Removed waiting room.
        @return None / None.
        """

        ...

    async def game_cancelled(self, event: GameCancelled) -> None:
        """@brief 投递对局取消 / Deliver game cancellation.

        @param event 已退款取消事件 / Refunded cancellation event.
        @return None / None.
        """

        ...


class NullRpsLifecycleSink:
    """@brief 不执行外部投递的生命周期端口 / Lifecycle sink performing no external delivery."""

    async def waiting_expired(self, event: WaitingCancelled) -> None:
        """@brief 忽略等待过期 / Ignore waiting expiration.

        @param event 等待过期事件 / Waiting-expiration event.
        @return None / None.
        """

        del event

    async def game_cancelled(self, event: GameCancelled) -> None:
        """@brief 忽略对局取消 / Ignore game cancellation.

        @param event 对局取消事件 / Game-cancellation event.
        @return None / None.
        """

        del event


@dataclass(slots=True)
class _ManagedWaiting:
    """@brief 注册表内部等待房间 / Internal managed waiting room.

    @param room 领域房间 / Domain room.
    @param invitation 可选投递地址 / Optional delivery address.
    @param matching_guest 正在执行锁外扣费的加入玩家 / Guest whose charge is in progress outside the registry lock.
    @param lock 仅串行化这个等待房间的匹配尝试 / Lock serializing match attempts for this waiting room only.
    """

    room: WaitingRoom
    """@brief 领域房间 / Domain room."""

    invitation: MessageAddress | None = None
    """@brief 邀请消息地址 / Invitation message address."""

    matching_guest: UserId | None = None
    """@brief 当前匹配 reservation 的加入玩家 / Joining player owning the current match reservation."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    """@brief 等待槽位局部锁 / Waiting-slot-local lock."""


@dataclass(slots=True)
class _ManagedGame:
    """@brief 注册表内部活动对局槽位 / Internal active-game slot.

    @param session 领域会话 / Domain session.
    @param delivery 可选投递地址 / Optional delivery addresses.
    @param lock 仅串行化本局转移与结算 / Lock serializing transitions and settlement for this game only.
    """

    session: GameSession
    """@brief 领域会话 / Domain session."""

    delivery: GameDelivery | None = None
    """@brief 投递地址 / Delivery addresses."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    """@brief 每局状态锁 / Per-game state lock."""


class RpsService:
    """@brief 有界且拥有完整生命周期的猜拳应用服务 / Bounded RPS service owning its full lifecycle.

    同一局通过局部锁线性化，不同局可以并发；一个受 ``TaskGroup`` 所有的监督任务管理
    全部 deadline，避免每局游离 ``create_task``。/ A per-game lock linearizes one aggregate
    while distinct games proceed concurrently; one ``TaskGroup``-owned supervisor manages every
    deadline without detached per-game tasks.
    """

    def __init__(
        self,
        *,
        ledger: RpsOperations,
        lifecycle_sink: RpsLifecycleSink | None = None,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_tombstones: int = DEFAULT_MAX_TOMBSTONES,
        waiting_timeout: timedelta = DEFAULT_WAITING_TIMEOUT,
        choice_timeout: timedelta = DEFAULT_CHOICE_TIMEOUT,
        clock: Clock | None = None,
        game_id_factory: GameIdFactory | None = None,
    ) -> None:
        """@brief 配置服务边界与端口 / Configure service bounds and ports.

        @param ledger 账户结算端口 / Account-settlement port.
        @param lifecycle_sink 超时和关停投递端口 / Timeout and shutdown delivery port.
        @param max_sessions 等待加活动对局上限 / Bound across waiting and active sessions.
        @param max_tombstones 终态版本墓碑上限 / Terminal-version tombstone bound.
        @param waiting_timeout 等待玩家时限 / Opponent-wait timeout.
        @param choice_timeout 出招时限 / Choice timeout.
        @param clock 可测试时钟 / Testable clock.
        @param game_id_factory 可测试游戏 ID 工厂 / Testable game-ID factory.
        @return None / None.
        @raises ValueError 容量或时限不为正 / If capacities or timeouts are not positive.
        """

        if not isinstance(ledger, RpsOperations):
            raise TypeError("ledger must implement RpsOperations")
        sink = lifecycle_sink or NullRpsLifecycleSink()
        if not isinstance(sink, RpsLifecycleSink):
            raise TypeError("lifecycle_sink must implement RpsLifecycleSink")
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        if max_tombstones <= 0:
            raise ValueError("max_tombstones must be positive")
        if waiting_timeout <= timedelta(0):
            raise ValueError("waiting_timeout must be positive")
        if choice_timeout <= timedelta(0):
            raise ValueError("choice_timeout must be positive")

        self._operations = ledger
        """@brief 状态与金币的原子事务端口 / Atomic state-and-coins transaction port."""
        self._sink = sink
        """@brief 生命周期投递端口 / Lifecycle delivery port."""
        self._max_sessions = max_sessions
        self._max_tombstones = max_tombstones
        self._waiting_timeout = waiting_timeout
        self._choice_timeout = choice_timeout
        self._clock = clock or _utc_now
        self._game_id_factory = game_id_factory or _new_game_id

        self._state = ServiceState.NEW
        """@brief 当前生命周期 / Current lifecycle."""
        self._registry_lock = asyncio.Lock()
        """@brief 保护注册表结构与索引 / Protects registry structure and indices."""
        self._wake = asyncio.Event()
        """@brief deadline 或生命周期变化通知 / Deadline or lifecycle-change notification."""
        self._waiting: _ManagedWaiting | None = None
        """@brief 唯一开放等待房间 / Sole open waiting room."""
        self._games: dict[GameId, _ManagedGame] = {}
        """@brief 活动对局注册表 / Active-game registry."""
        self._player_index: dict[UserId, GameId] = {}
        """@brief 玩家到等待/活动游戏的唯一索引 / Unique player-to-waiting/active-game index."""
        self._tombstones: OrderedDict[GameId, GameVersion] = OrderedDict()
        """@brief 有界终态版本墓碑 / Bounded terminal-version tombstones."""

    @property
    def state(self) -> ServiceState:
        """@brief 返回服务生命周期 / Return service lifecycle.

        @return 当前生命周期 / Current lifecycle.
        """

        return self._state

    @property
    def session_count(self) -> int:
        """@brief 返回等待加活动对局数量 / Return waiting plus active-session count.

        @return 有界注册表使用量 / Bounded-registry usage.
        """

        return len(self._games) + int(self._waiting is not None)

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行 deadline 监督器直至停止并退款排空 / Run the deadline supervisor until stopped, then refund and drain.

        @param stop_event BotRuntime 拥有的停止信号 / Stop signal owned by BotRuntime.
        @return None / None.
        @raise RuntimeError 同一实例被重复运行时抛出 / Raised when the same instance is run more than once.
        @note TaskGroup 结构化拥有唯一监督器；正常停止拒绝新请求、等待锁外扣费补偿，
        并按 SERVICE_SHUTDOWN 取消和退款全部活动对局。/
        A TaskGroup structurally owns the sole supervisor; normal shutdown rejects new
        requests, waits for out-of-lock charge compensation, and cancels/refunds every
        active game with SERVICE_SHUTDOWN.
        """

        if self._state is not ServiceState.NEW:
            raise RuntimeError(f"RPS service cannot run from {self._state}")
        try:
            await self._restore()
            self._state = ServiceState.RUNNING
            await self._expire_due()
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(
                    self._deadline_loop(),
                    name="rps-deadline-supervisor",
                )
                try:
                    await stop_event.wait()
                finally:
                    self._begin_closing()
        finally:
            self._begin_closing()
            try:
                await self._drain_for_shutdown()
            finally:
                self._state = ServiceState.CLOSED

    async def _restore(self) -> None:
        """@brief 从持久化事实重建内存索引 / Rebuild in-memory indices from durable facts.

        @return None / None.
        @raises RuntimeError 持久状态违反单玩家唯一性 / If durable state violates player uniqueness.
        """

        restored = await self._operations.load_recovery_state(
            tombstone_limit=self._max_tombstones
        )
        async with self._registry_lock:
            if self._waiting is not None or self._games or self._player_index:
                raise RuntimeError("RPS registry must be empty before recovery")
            if restored.waiting is not None:
                waiting = _ManagedWaiting(
                    restored.waiting.room,
                    restored.waiting.invitation,
                )
                self._waiting = waiting
                self._index_player_locked(
                    waiting.room.host.user_id,
                    waiting.room.game_id,
                )
            for restored_game in restored.games:
                managed = _ManagedGame(
                    restored_game.session,
                    restored_game.delivery,
                )
                self._games[managed.session.game_id] = managed
                for player in managed.session.players:
                    self._index_player_locked(
                        player.user_id,
                        managed.session.game_id,
                    )
            for game_id, version in restored.tombstones:
                self._remember_terminal_locked(game_id, version)

    def _index_player_locked(self, user_id: UserId, game_id: GameId) -> None:
        """@brief 恢复时建立唯一玩家索引 / Build the unique player index during recovery.

        @param user_id 玩家身份 / Player identity.
        @param game_id 活动游戏身份 / Active game identity.
        @return None / None.
        """

        existing = self._player_index.get(user_id)
        if existing is not None and existing != game_id:
            raise RuntimeError(
                f"RPS player {user_id.value} belongs to both {existing} and {game_id}"
            )
        self._player_index[user_id] = game_id

    def _begin_closing(self) -> None:
        """@brief 原子切换为停止准入并唤醒监督器 / Atomically stop admission and wake the supervisor.

        @return None / None.
        """

        if self._state is ServiceState.RUNNING:
            self._state = ServiceState.CLOSING
            self._wake.set()

    async def _drain_for_shutdown(self) -> None:
        """@brief 等待匹配补偿并退款所有活动对局 / Await match compensation and refund all active games.

        @return None / None.
        """

        async with self._registry_lock:
            waiting = self._remove_waiting_locked()
            games = tuple(self._games.values())
        if waiting is not None:
            async with waiting.lock:
                persisted = await self._operations.finish_waiting(
                    waiting.room,
                    WaitingTerminalStatus.CANCELLED,
                    finished_at=self._now(),
                )
                if persisted.code not in {
                    RpsMutationCode.APPLIED,
                    RpsMutationCode.NOT_FOUND,
                    RpsMutationCode.STALE,
                }:
                    raise RuntimeError(
                        f"RPS waiting shutdown lost durable row {waiting.room.game_id}"
                    )
            await self._safe_waiting_delivery(
                WaitingCancelled(waiting.room, waiting.invitation)
            )
        for managed in games:
            await self._cancel_managed_game(
                managed,
                GameCancellation.SERVICE_SHUTDOWN,
            )

    async def request_game(self, player: Player) -> RequestGameResult:
        """@brief 创建等待房间或与当前房主匹配 / Open a room or match the current host.

        @param player 发起 `/rps_game` 的玩家 / Player invoking `/rps_game`.
        @return 等待、匹配、失效或拒绝结果 / Waiting, match, invalidation, or rejection result.
        """

        rejection = await self._admit_player(player.user_id)
        if rejection is not None:
            return rejection
        await self._expire_due()
        waiting_to_match: _ManagedWaiting | None = None
        async with self._registry_lock:
            unavailable = self._unavailable_rejection()
            if unavailable is not None:
                return unavailable
            existing = self._player_index.get(player.user_id)
            if existing is not None:
                if (
                    self._waiting is not None
                    and self._waiting.room.host.user_id == player.user_id
                ):
                    return Rejected(
                        RejectionCode.ALREADY_WAITING,
                        existing,
                        self._waiting.room.version,
                    )
                game = self._games.get(existing)
                return Rejected(
                    RejectionCode.ALREADY_IN_GAME,
                    existing,
                    game.session.version if game is not None else None,
                )
            if self._waiting is None:
                if self.session_count >= self._max_sessions:
                    return Rejected(RejectionCode.CAPACITY_REACHED)
                room = WaitingRoom.open(
                    self._allocate_game_id_locked(),
                    player,
                    now=self._now(),
                    wait_for=self._waiting_timeout,
                )
                if not await self._operations.create_waiting(room):
                    return Rejected(RejectionCode.CAPACITY_REACHED)
                self._waiting = _ManagedWaiting(room)
                self._player_index[player.user_id] = room.game_id
                self._wake.set()
                return WaitingCreated(room)
            waiting_to_match = self._waiting
        if waiting_to_match is None:
            raise RuntimeError("waiting room disappeared after registry selection")
        return await self._match(
            player,
            waiting_to_match,
            expected_version=waiting_to_match.room.version,
        )

    async def join_game(
        self,
        player: Player,
        game_id: GameId,
        expected_version: GameVersion,
    ) -> JoinGameResult:
        """@brief 通过版本化邀请加入等待房间 / Join a room through a versioned invitation.

        @param player 加入玩家 / Joining player.
        @param game_id callback 中的游戏身份 / Game identity carried by the callback.
        @param expected_version callback 中的聚合版本 / Aggregate version carried by the callback.
        @return 匹配、失效或拒绝结果 / Match, invalidation, or rejection result.
        """

        rejection = await self._admit_player(player.user_id)
        if rejection is not None:
            return rejection
        await self._expire_due()
        waiting_to_match: _ManagedWaiting | None = None
        async with self._registry_lock:
            unavailable = self._unavailable_rejection()
            if unavailable is not None:
                return unavailable
            waiting = self._waiting
            if waiting is None or waiting.room.game_id != game_id:
                return self._missing_rejection(game_id, expected_version)
            if waiting.room.version != expected_version:
                return Rejected(
                    RejectionCode.STALE_VERSION, game_id, waiting.room.version
                )
            if waiting.room.host.user_id == player.user_id:
                return Rejected(RejectionCode.SELF_JOIN, game_id, waiting.room.version)
            existing = self._player_index.get(player.user_id)
            if existing is not None:
                game = self._games.get(existing)
                return Rejected(
                    RejectionCode.ALREADY_IN_GAME,
                    existing,
                    game.session.version if game is not None else None,
                )
            waiting_to_match = waiting
        if waiting_to_match is None:
            raise RuntimeError("waiting room disappeared after callback validation")
        return await self._match(
            player,
            waiting_to_match,
            expected_version=expected_version,
        )

    async def cancel_waiting(
        self,
        actor: UserId,
        game_id: GameId,
        expected_version: GameVersion,
    ) -> CancelWaitingResult:
        """@brief 房主取消版本化等待邀请 / Let the host cancel a versioned invitation.

        @param actor 操作者 / Acting user.
        @param game_id callback 中的游戏身份 / Game identity from the callback.
        @param expected_version callback 中的版本 / Version from the callback.
        @return 取消或拒绝结果 / Cancellation or rejection result.
        """

        async with self._registry_lock:
            unavailable = self._unavailable_rejection()
            if unavailable is not None:
                return unavailable
            waiting = self._waiting
            if waiting is None or waiting.room.game_id != game_id:
                return self._missing_rejection(game_id, expected_version)
            if waiting.room.version != expected_version:
                return Rejected(
                    RejectionCode.STALE_VERSION, game_id, waiting.room.version
                )
            if waiting.room.host.user_id != actor:
                return Rejected(
                    RejectionCode.NOT_PARTICIPANT, game_id, waiting.room.version
                )
            persisted = await self._operations.finish_waiting(
                waiting.room,
                WaitingTerminalStatus.CANCELLED,
                finished_at=self._now(),
            )
            if persisted.code is not RpsMutationCode.APPLIED:
                current = persisted.current_version or waiting.room.version
                return Rejected(RejectionCode.STALE_VERSION, game_id, current)
            removed = self._remove_waiting_locked()
            if removed is None:
                raise RuntimeError(
                    "waiting room disappeared while holding the registry lock"
                )
            self._remember_terminal_locked(game_id, expected_version)
            self._wake.set()
            return WaitingCancelled(removed.room, removed.invitation)

    async def choose(
        self,
        actor: UserId,
        game_id: GameId,
        expected_version: GameVersion,
        choice: Choice,
    ) -> ChooseResult:
        """@brief 在线性化的单局边界内应用选择和结算 / Apply a choice and settlement in one per-game linearization boundary.

        @param actor 出招玩家 / Acting player.
        @param game_id 游戏身份 / Game identity.
        @param expected_version callback 版本 / Callback version.
        @param choice 玩家手势 / Player choice.
        @return 记录、结算或拒绝结果 / Recorded, settled, or rejected result.
        """

        async with self._registry_lock:
            unavailable = self._unavailable_rejection()
            if unavailable is not None:
                return unavailable
            managed = self._games.get(game_id)
            if managed is None:
                return self._missing_rejection(game_id, expected_version)
        async with managed.lock:
            if managed.session.version != expected_version:
                return Rejected(
                    RejectionCode.STALE_VERSION, game_id, managed.session.version
                )
            if managed.delivery is None:
                return Rejected(
                    RejectionCode.GAME_NOT_READY, game_id, managed.session.version
                )
            if actor not in {player.user_id for player in managed.session.players}:
                return Rejected(
                    RejectionCode.NOT_PARTICIPANT, game_id, managed.session.version
                )
            if managed.session.choice_for(actor) is not None:
                return Rejected(
                    RejectionCode.ALREADY_CHOSEN, game_id, managed.session.version
                )
            try:
                previous = managed.session
                updated = managed.session.choose(
                    actor,
                    choice,
                    expected_version=expected_version,
                    now=self._now(),
                )
            except StaleGameVersion:
                return Rejected(
                    RejectionCode.STALE_VERSION, game_id, managed.session.version
                )
            except RpsDomainError:
                return Rejected(
                    RejectionCode.STALE_VERSION, game_id, managed.session.version
                )

            persisted = await self._operations.commit_choice(
                previous,
                updated,
                committed_at=self._now(),
            )
            if persisted.code is not RpsMutationCode.APPLIED:
                current = persisted.current_version or managed.session.version
                return Rejected(RejectionCode.STALE_VERSION, game_id, current)
            if updated.status is GameStatus.CHOOSING:
                managed.session = updated
                return ChoiceRecorded(updated, actor, managed.delivery)
            outcome = updated.outcome
            if outcome is None:
                raise RuntimeError("finished RPS session has no outcome")
            managed.session = updated
            await self._remove_game(managed)
            return GameSettled(updated, managed.delivery)

    async def abort_game(
        self,
        game_id: GameId,
        expected_version: GameVersion,
    ) -> AbortGameResult:
        """@brief 关键 Telegram 投递失败后取消并退款 / Cancel and refund after critical Telegram delivery failure.

        @param game_id 游戏身份 / Game identity.
        @param expected_version 调用方观察到的版本 / Version observed by the caller.
        @return 取消或拒绝结果 / Cancellation or rejection result.
        """

        async with self._registry_lock:
            managed = self._games.get(game_id)
            if managed is None:
                return self._missing_rejection(game_id, expected_version)
        async with managed.lock:
            if managed.session.version != expected_version:
                return Rejected(
                    RejectionCode.STALE_VERSION, game_id, managed.session.version
                )
            cancelled = await self._cancel_managed_game_locked(
                managed,
                GameCancellation.DELIVERY_FAILED,
            )
            return cancelled

    async def bind_waiting_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        invitation: MessageAddress,
    ) -> bool:
        """@brief 将已发送邀请地址绑定到等待房间 / Bind a sent invitation address to a waiting room.

        @param game_id 游戏身份 / Game identity.
        @param expected_version 发送内容对应版本 / Version represented by the sent content.
        @param invitation 可编辑邀请地址 / Editable invitation address.
        @return 仍是同一房间时为 True / True when the same room is still current.
        """

        async with self._registry_lock:
            waiting = self._waiting
            if (
                waiting is None
                or waiting.room.game_id != game_id
                or waiting.room.version != expected_version
            ):
                return False
            if not await self._operations.bind_waiting_delivery(
                game_id,
                expected_version,
                invitation,
            ):
                return False
            waiting.invitation = invitation
            return True

    async def bind_game_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        delivery: GameDelivery,
    ) -> bool:
        """@brief 将选择消息地址绑定到活动对局 / Bind choice-message addresses to an active game.

        @param game_id 游戏身份 / Game identity.
        @param expected_version 消息按钮对应版本 / Version encoded by the messages.
        @param delivery 全部投递地址 / All delivery addresses.
        @return 仍是相同版本活动对局时为 True / True when that active version is still current.
        """

        async with self._registry_lock:
            managed = self._games.get(game_id)
            if managed is None:
                return False
        async with managed.lock:
            if managed.session.version != expected_version:
                return False
            if not await self._operations.bind_game_delivery(
                game_id,
                expected_version,
                delivery,
            ):
                return False
            managed.delivery = delivery
            return True

    async def _admit_player(self, user_id: UserId) -> Rejected | None:
        """@brief 检查服务状态、注册与余额 / Check service state, registration, and balance.

        @param user_id 请求玩家 / Requesting player.
        @return 可选拒绝 / Optional rejection.
        """

        unavailable = self._unavailable_rejection()
        if unavailable is not None:
            return unavailable
        status = await self._operations.status(user_id)
        if not status.registered:
            return Rejected(RejectionCode.NOT_REGISTERED)
        if status.coins < ENTRY_FEE:
            return Rejected(RejectionCode.INSUFFICIENT_COINS)
        return None

    async def _match(
        self,
        guest: Player,
        waiting: _ManagedWaiting,
        *,
        expected_version: GameVersion,
    ) -> MatchStarted | WaitingInvalidated | Rejected:
        """@brief 以等待槽 reservation、锁外扣费和短 CAS 完成匹配 / Match with a waiting-slot reservation, lock-free charge, and short CAS.

        @param guest 加入玩家 / Joining player.
        @param waiting 当前等待槽位 / Current waiting slot.
        @param expected_version 调用方观察到的等待房间版本 / Waiting-room version observed by the caller.
        @return 匹配、失效或拒绝 / Match, invalidation, or rejection.
        @note 数据库事务期间不持有全局 registry lock；同一等待槽由局部锁串行化。/
            The global registry lock is not held during the DB transaction; a local lock serializes this waiting slot.
        """

        async with waiting.lock:
            rejection = await self._reserve_match(
                guest,
                waiting,
                expected_version,
            )
            if rejection is not None:
                return rejection
            try:
                session = waiting.room.join(
                    guest,
                    expected_version=expected_version,
                    now=self._now(),
                    choose_for=self._choice_timeout,
                )
            except RpsDomainError:
                return await self._expire_failed_match(waiting, guest.user_id)

            try:
                persisted = await self._operations.start_game(
                    waiting.room,
                    session,
                    started_at=session.started_at,
                )
            except BaseException:
                async with self._registry_lock:
                    self._release_match_reservation_locked(waiting, guest.user_id)
                raise

            if persisted.code is not RpsMatchCode.STARTED:
                return await self._map_failed_match(
                    persisted.code,
                    persisted.current_version,
                    waiting,
                    guest.user_id,
                    expected_version,
                )
            committed_session = persisted.session
            if committed_session is None:
                raise RuntimeError(
                    "started RPS match did not return its durable session"
                )
            return await self._publish_started_match(
                committed_session,
                waiting,
                guest.user_id,
                expected_version,
            )

    async def _reserve_match(
        self,
        guest: Player,
        waiting: _ManagedWaiting,
        expected_version: GameVersion,
    ) -> Rejected | None:
        """@brief 校验并占用一个内存匹配槽 / Validate and reserve one in-memory match slot.

        @param guest 加入玩家 / Joining player.
        @param waiting 等待槽 / Waiting slot.
        @param expected_version callback 版本 / Callback version.
        @return 拒绝结果；成功占用时为 None / Rejection, or None when reserved.
        """

        async with self._registry_lock:
            unavailable = self._unavailable_rejection()
            if unavailable is not None:
                return unavailable
            if self._waiting is not waiting:
                return self._missing_rejection(waiting.room.game_id, expected_version)
            if waiting.room.version != expected_version:
                return Rejected(
                    RejectionCode.STALE_VERSION,
                    waiting.room.game_id,
                    waiting.room.version,
                )
            if waiting.room.host.user_id == guest.user_id:
                return Rejected(
                    RejectionCode.SELF_JOIN,
                    waiting.room.game_id,
                    waiting.room.version,
                )
            if waiting.matching_guest is not None:
                return Rejected(
                    RejectionCode.GAME_NOT_READY,
                    waiting.room.game_id,
                    waiting.room.version,
                )
            existing = self._player_index.get(guest.user_id)
            if existing is not None:
                game = self._games.get(existing)
                return Rejected(
                    RejectionCode.ALREADY_IN_GAME,
                    existing,
                    game.session.version if game is not None else None,
                )
            waiting.matching_guest = guest.user_id
            self._player_index[guest.user_id] = waiting.room.game_id
            return None

    async def _expire_failed_match(
        self,
        waiting: _ManagedWaiting,
        guest_id: UserId,
    ) -> Rejected:
        """@brief 终结领域已判定过期的匹配 / Finish a match rejected as expired by the domain.

        @param waiting 等待槽 / Waiting slot.
        @param guest_id 已占用槽的加入玩家 / Guest owning the reservation.
        @return 陈旧版本拒绝 / Stale-version rejection.
        """

        await self._operations.finish_waiting(
            waiting.room,
            WaitingTerminalStatus.EXPIRED,
            finished_at=self._now(),
        )
        async with self._registry_lock:
            if self._waiting is waiting:
                removed = self._remove_waiting_locked()
                if removed is not None:
                    self._remember_terminal_locked(
                        removed.room.game_id,
                        removed.room.version,
                    )
            else:
                self._release_match_reservation_locked(waiting, guest_id)
            self._wake.set()
        return Rejected(
            RejectionCode.STALE_VERSION,
            waiting.room.game_id,
            waiting.room.version,
        )

    async def _map_failed_match(
        self,
        code: RpsMatchCode,
        current_version: GameVersion | None,
        waiting: _ManagedWaiting,
        guest_id: UserId,
        expected_version: GameVersion,
    ) -> WaitingInvalidated | Rejected:
        """@brief 将事务拒绝映射为应用结果并释放 reservation / Map a transaction rejection and release its reservation."""

        if code is RpsMatchCode.FIRST_UNAVAILABLE:
            async with self._registry_lock:
                if self._waiting is not waiting:
                    self._release_match_reservation_locked(waiting, guest_id)
                    return self._missing_rejection(
                        waiting.room.game_id,
                        expected_version,
                    )
                removed = self._remove_waiting_locked()
                if removed is None:
                    raise RuntimeError("waiting room disappeared during matching")
                self._remember_terminal_locked(
                    removed.room.game_id, removed.room.version
                )
                self._wake.set()
            return WaitingInvalidated(removed.room, removed.invitation)

        async with self._registry_lock:
            current = self._waiting is waiting
            self._release_match_reservation_locked(waiting, guest_id)
        if code is RpsMatchCode.SECOND_UNAVAILABLE and current:
            return Rejected(
                RejectionCode.INSUFFICIENT_COINS,
                waiting.room.game_id,
                current_version or waiting.room.version,
            )
        if code is RpsMatchCode.PLAYER_BUSY:
            return Rejected(
                RejectionCode.ALREADY_IN_GAME,
                waiting.room.game_id,
                waiting.room.version,
            )
        return self._missing_rejection(waiting.room.game_id, expected_version)

    async def _publish_started_match(
        self,
        session: GameSession,
        waiting: _ManagedWaiting,
        guest_id: UserId,
        expected_version: GameVersion,
    ) -> MatchStarted | Rejected:
        """@brief 发布已提交匹配，或在关停竞争中原子退款 / Publish a committed match or atomically refund a shutdown race."""

        async with self._registry_lock:
            can_commit = (
                self._state is ServiceState.RUNNING
                and self._waiting is waiting
                and waiting.matching_guest == guest_id
            )
            if can_commit:
                self._waiting = None
                waiting.matching_guest = None
                self._games[session.game_id] = _ManagedGame(session)
                self._wake.set()
            else:
                self._release_match_reservation_locked(waiting, guest_id)
        if can_commit:
            return MatchStarted(session, waiting.invitation)

        cancelled = session.cancel(
            GameCancellation.SERVICE_SHUTDOWN,
            expected_version=session.version,
        )
        compensation = await self._operations.cancel_game(
            session,
            cancelled,
            committed_at=self._now(),
        )
        if compensation.code not in {
            RpsMutationCode.APPLIED,
            RpsMutationCode.STALE,
        }:
            raise RuntimeError(
                f"RPS match compensation lost durable row {session.game_id}"
            )
        unavailable = self._unavailable_rejection()
        if unavailable is not None:
            return unavailable
        return self._missing_rejection(waiting.room.game_id, expected_version)

    async def _deadline_loop(self) -> None:
        """@brief 监督最近 deadline 并触发领域取消 / Supervise the nearest deadline and trigger domain cancellation.

        @return None / None.
        """

        while self._state is ServiceState.RUNNING:
            async with self._registry_lock:
                self._wake.clear()
                deadlines = [game.session.expires_at for game in self._games.values()]
                if self._waiting is not None:
                    deadlines.append(self._waiting.room.expires_at)
                nearest = min(deadlines, default=None)
            if nearest is None:
                await self._wake.wait()
                continue
            seconds = max(0.0, (nearest - self._now()).total_seconds())
            if seconds > 0:
                try:
                    async with asyncio.timeout(seconds):
                        await self._wake.wait()
                    continue
                except TimeoutError:
                    pass
            failed = await self._expire_due()
            if failed:
                await asyncio.sleep(_RETRY_FAILED_EXPIRY_AFTER)

    async def _expire_due(self) -> bool:
        """@brief 过期等待房间与活动对局 / Expire due waiting rooms and active games.

        @return 是否至少一次结算失败并需重试 / Whether at least one settlement failed and needs retry.
        """

        now = self._now()
        failed = False
        waiting_event: WaitingCancelled | None = None
        async with self._registry_lock:
            if self._waiting is not None and now >= self._waiting.room.expires_at:
                try:
                    persisted = await self._operations.finish_waiting(
                        self._waiting.room,
                        WaitingTerminalStatus.EXPIRED,
                        finished_at=now,
                    )
                except Exception:
                    failed = True
                    logger.exception(
                        "Failed to settle expired RPS waiting room %s",
                        self._waiting.room.game_id,
                    )
                else:
                    if persisted.code is RpsMutationCode.APPLIED:
                        removed = self._remove_waiting_locked()
                    else:
                        removed = None
                    if removed is not None:
                        self._remember_terminal_locked(
                            removed.room.game_id, removed.room.version
                        )
                        waiting_event = WaitingCancelled(
                            removed.room, removed.invitation
                        )
            due_games = tuple(
                game for game in self._games.values() if now >= game.session.expires_at
            )
        if waiting_event is not None:
            await self._safe_waiting_delivery(waiting_event)

        for managed in due_games:
            try:
                await self._cancel_managed_game(managed, GameCancellation.TIMEOUT)
            except Exception:
                failed = True
                logger.exception(
                    "Failed to settle expired RPS game %s", managed.session.game_id
                )
        return failed

    async def _cancel_managed_game(
        self,
        managed: _ManagedGame,
        reason: GameCancellation,
    ) -> GameCancelled | None:
        """@brief 获取单局锁后取消、退款并投递 / Lock one game, cancel, refund, and deliver.

        @param managed 活动对局槽位 / Active game slot.
        @param reason 取消原因 / Cancellation reason.
        @return 取消事件；已经移除时为 None / Cancellation event, or None if already removed.
        """

        async with managed.lock:
            async with self._registry_lock:
                if self._games.get(managed.session.game_id) is not managed:
                    return None
            event = await self._cancel_managed_game_locked(managed, reason)
        await self._safe_game_delivery(event)
        return event

    async def _cancel_managed_game_locked(
        self,
        managed: _ManagedGame,
        reason: GameCancellation,
    ) -> GameCancelled:
        """@brief 在已持有单局锁时取消并退款 / Cancel and refund while the per-game lock is held.

        @param managed 活动对局槽位 / Active game slot.
        @param reason 取消原因 / Cancellation reason.
        @return 退款完成的取消事件 / Refunded cancellation event.
        @note 调用方必须持有 ``managed.lock`` / Caller must hold ``managed.lock``.
        """

        session = managed.session
        cancelled = session.cancel(reason, expected_version=session.version)
        persisted = await self._operations.cancel_game(
            session,
            cancelled,
            committed_at=self._now(),
        )
        if persisted.code is not RpsMutationCode.APPLIED:
            await self._remove_game(managed)
            raise RuntimeError(
                f"RPS cancellation lost OCC for {session.game_id}: {persisted.code}"
            )
        managed.session = cancelled
        await self._remove_game(managed)
        return GameCancelled(cancelled, managed.delivery)

    async def _remove_game(self, managed: _ManagedGame) -> None:
        """@brief 从注册表与玩家索引移除终态对局 / Remove a terminal game from registry and player index.

        @param managed 终态对局槽位 / Terminal game slot.
        @return None / None.
        """

        async with self._registry_lock:
            game_id = managed.session.game_id
            if self._games.get(game_id) is not managed:
                return
            self._games.pop(game_id)
            for player in managed.session.players:
                if self._player_index.get(player.user_id) == game_id:
                    self._player_index.pop(player.user_id)
            self._remember_terminal_locked(game_id, managed.session.version)
            self._wake.set()

    def _remove_waiting_locked(self) -> _ManagedWaiting | None:
        """@brief 在注册表锁内移除等待房间及全部 reservation 索引 / Remove a waiting room and all reservation indices under lock.

        @return 被移除槽位；不存在时为 None / Removed slot, or None.
        """

        waiting = self._waiting
        if waiting is None:
            return None
        self._waiting = None
        if self._player_index.get(waiting.room.host.user_id) == waiting.room.game_id:
            self._player_index.pop(waiting.room.host.user_id)
        matching_guest = waiting.matching_guest
        if (
            matching_guest is not None
            and self._player_index.get(matching_guest) == waiting.room.game_id
        ):
            self._player_index.pop(matching_guest)
        return waiting

    def _release_match_reservation_locked(
        self,
        waiting: _ManagedWaiting,
        guest: UserId,
    ) -> None:
        """@brief 在注册表锁内释放失败匹配的 guest reservation / Release a failed guest reservation under registry lock.

        @param waiting 等待槽位 / Waiting slot.
        @param guest reservation 所有者 / Reservation owner.
        @return None / None.
        """

        if waiting.matching_guest == guest:
            waiting.matching_guest = None
        if self._player_index.get(guest) == waiting.room.game_id:
            self._player_index.pop(guest)

    def _remember_terminal_locked(self, game_id: GameId, version: GameVersion) -> None:
        """@brief 记录有界终态版本墓碑 / Record a bounded terminal-version tombstone.

        @param game_id 终态游戏身份 / Terminal game identity.
        @param version 最终版本 / Final version.
        @return None / None.
        """

        self._tombstones[game_id] = version
        self._tombstones.move_to_end(game_id)
        while len(self._tombstones) > self._max_tombstones:
            self._tombstones.popitem(last=False)

    def _missing_rejection(
        self,
        game_id: GameId,
        expected_version: GameVersion,
    ) -> Rejected:
        """@brief 区分未知游戏与已终态陈旧 callback / Distinguish unknown games from terminal stale callbacks.

        @param game_id callback 游戏身份 / Callback game identity.
        @param expected_version callback 版本 / Callback version.
        @return 类型化拒绝 / Typed rejection.
        """

        terminal_version = self._tombstones.get(game_id)
        if terminal_version is not None:
            return Rejected(RejectionCode.STALE_VERSION, game_id, terminal_version)
        return Rejected(RejectionCode.NOT_FOUND, game_id, expected_version)

    def _unavailable_rejection(self) -> Rejected | None:
        """@brief 服务不运行时生成拒绝 / Build a rejection when the service is not running.

        @return 服务可用时为 None / None when the service is available.
        """

        if self._state is ServiceState.RUNNING:
            return None
        return Rejected(RejectionCode.SERVICE_UNAVAILABLE)

    def _allocate_game_id_locked(self) -> GameId:
        """@brief 在注册表内分配无碰撞游戏身份 / Allocate a collision-free game identity in the registry.

        @return 新游戏身份 / New game identity.
        @raises RuntimeError 工厂连续产生碰撞 / If the factory repeatedly produces collisions.
        """

        for _attempt in range(16):
            game_id = self._game_id_factory()
            if (
                game_id not in self._games
                and game_id not in self._tombstones
                and (self._waiting is None or self._waiting.room.game_id != game_id)
            ):
                return game_id
        raise RuntimeError("game-id factory produced too many collisions")

    def _now(self) -> datetime:
        """@brief 读取并校验带时区当前时间 / Read and validate timezone-aware current time.

        @return 当前时间 / Current time.
        @raises ValueError 时钟返回 naive datetime / If the clock returns a naive datetime.
        """

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("RPS clock must return timezone-aware datetime values")
        return now

    async def _safe_waiting_delivery(self, event: WaitingCancelled) -> None:
        """@brief 隔离等待过期投递错误 / Isolate waiting-expiration delivery failures.

        @param event 等待过期事件 / Waiting-expiration event.
        @return None / None.
        """

        try:
            await self._sink.waiting_expired(event)
        except Exception:
            logger.exception(
                "Failed to deliver expired RPS invitation %s", event.room.game_id
            )

    async def _safe_game_delivery(self, event: GameCancelled) -> None:
        """@brief 隔离取消投递错误 / Isolate cancellation-delivery failures.

        @param event 对局取消事件 / Game-cancellation event.
        @return None / None.
        """

        try:
            await self._sink.game_cancelled(event)
        except Exception:
            logger.exception(
                "Failed to deliver cancelled RPS game %s", event.session.game_id
            )


def _utc_now() -> datetime:
    """@brief 返回当前 UTC 时间 / Return current UTC time.

    @return 带 UTC 时区的当前时间 / Current timezone-aware UTC time.
    """

    return datetime.now(UTC)


def _new_game_id() -> GameId:
    """@brief 生成紧凑 callback-safe 随机游戏身份 / Generate a compact callback-safe random game identity.

    @return 约 66 位熵的 URL-safe 游戏身份 / URL-safe game identity with roughly 66 bits of entropy.
    """

    return GameId(secrets.token_urlsafe(8))
