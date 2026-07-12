"""@brief 猜拳原子持久化端口 / Atomic persistence port for RPS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from fogmoe_bot.application.games.rps_delivery import GameDelivery, MessageAddress
from fogmoe_bot.domain.games import (
    AccountStatus,
    GameId,
    GameSession,
    GameVersion,
    UserId,
    WaitingRoom,
)


class RpsMutationCode(StrEnum):
    """@brief 一般持久化转移结果 / General persistence-transition result."""

    APPLIED = "applied"
    NOT_FOUND = "not_found"
    STALE = "stale"


class RpsMatchCode(StrEnum):
    """@brief 等待房间到活动对局的原子匹配结果 / Atomic waiting-to-active match result."""

    STARTED = "started"
    FIRST_UNAVAILABLE = "first_unavailable"
    SECOND_UNAVAILABLE = "second_unavailable"
    PLAYER_BUSY = "player_busy"
    NOT_FOUND = "not_found"
    STALE = "stale"


class WaitingTerminalStatus(StrEnum):
    """@brief 等待房间的持久终态 / Durable terminal state of a waiting room."""

    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class RpsMutationResult:
    """@brief 一次 CAS 持久化转移的结果 / Result of one persisted CAS transition.

    @param code 转移结果 / Transition result.
    @param current_version 数据库观察到的版本 / Version observed in storage.
    """

    code: RpsMutationCode
    current_version: GameVersion | None = None


@dataclass(frozen=True, slots=True)
class RpsMatchResult:
    """@brief 原子匹配与双人扣费结果 / Atomic match-and-pair-charge result.

    @param code 匹配结果 / Match result.
    @param current_version 数据库观察到的版本 / Version observed in storage.
    @param session 已提交的活动会话 / Committed active session.
    """

    code: RpsMatchCode
    current_version: GameVersion | None = None
    session: GameSession | None = None


@dataclass(frozen=True, slots=True)
class RestoredWaiting:
    """@brief 启动时恢复的等待房间 / Waiting room restored at startup.

    @param room 领域房间 / Domain room.
    @param invitation 已绑定的 Telegram 地址 / Bound Telegram address.
    """

    room: WaitingRoom
    invitation: MessageAddress | None


@dataclass(frozen=True, slots=True)
class RestoredGame:
    """@brief 启动时恢复的活动对局 / Active game restored at startup.

    @param session 领域会话 / Domain session.
    @param delivery 已绑定的 Telegram 地址 / Bound Telegram addresses.
    """

    session: GameSession
    delivery: GameDelivery | None


@dataclass(frozen=True, slots=True)
class RpsRecoveryState:
    """@brief 一次有界启动恢复快照 / One bounded startup-recovery snapshot.

    @param waiting 唯一等待房间 / Sole waiting room.
    @param games 活动对局 / Active games.
    @param tombstones 最近终态版本 / Recent terminal versions.
    """

    waiting: RestoredWaiting | None
    games: tuple[RestoredGame, ...]
    tombstones: tuple[tuple[GameId, GameVersion], ...]


@runtime_checkable
class RpsOperations(Protocol):
    """@brief 猜拳状态与金币的原子事务端口 / Atomic transaction port for RPS state and coins."""

    async def status(self, user_id: UserId) -> AccountStatus:
        """@brief 读取玩家准入状态 / Read player admission state."""

        ...

    async def load_recovery_state(self, *, tombstone_limit: int) -> RpsRecoveryState:
        """@brief 读取活动状态与有界终态版本 / Load active state and bounded terminal versions."""

        ...

    async def create_waiting(self, room: WaitingRoom) -> bool:
        """@brief 持久化唯一等待房间 / Persist the sole waiting room."""

        ...

    async def finish_waiting(
        self,
        room: WaitingRoom,
        status: WaitingTerminalStatus,
        *,
        finished_at: datetime,
    ) -> RpsMutationResult:
        """@brief 以 CAS 结束等待房间 / Finish a waiting room with CAS."""

        ...

    async def start_game(
        self,
        room: WaitingRoom,
        session: GameSession,
        *,
        started_at: datetime,
    ) -> RpsMatchResult:
        """@brief 在同一事务中激活对局并扣除双方入场费 / Activate a game and charge both entries in one transaction."""

        ...

    async def commit_choice(
        self,
        previous: GameSession,
        updated: GameSession,
        *,
        committed_at: datetime,
    ) -> RpsMutationResult:
        """@brief 持久化选择，并在终局同事务发放奖金 / Persist a choice and atomically pay a terminal outcome."""

        ...

    async def cancel_game(
        self,
        previous: GameSession,
        cancelled: GameSession,
        *,
        committed_at: datetime,
    ) -> RpsMutationResult:
        """@brief 在同一事务中取消对局并退款 / Cancel a game and refund it in one transaction."""

        ...

    async def bind_waiting_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        invitation: MessageAddress,
    ) -> bool:
        """@brief 持久化等待邀请地址 / Persist a waiting-invitation address."""

        ...

    async def bind_game_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        delivery: GameDelivery,
    ) -> bool:
        """@brief 持久化活动对局投递地址 / Persist active-game delivery addresses."""

        ...
