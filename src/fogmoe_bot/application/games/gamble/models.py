"""@brief 多人奖池命令与结果 / Multiplayer-pool commands and results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from fogmoe_bot.domain.games import GambleSession, GameSessionId


class GambleCode(StrEnum):
    """@brief 多人奖池用例结果代码 / Multiplayer-pool use-case result codes."""

    SUCCESS = "success"
    NOT_REGISTERED = "not_registered"
    PERMISSION_DENIED = "permission_denied"
    ALREADY_ACTIVE = "already_active"
    NO_ACTIVE_SESSION = "no_active_session"
    EXPIRED = "expired"
    ALREADY_JOINED = "already_joined"
    INSUFFICIENT_COINS = "insufficient_coins"


@dataclass(frozen=True, slots=True)
class OpenGamble:
    """@brief 开启多人奖池命令 / Open-multiplayer-pool command."""

    user_id: int
    chat_id: int
    message_id: int
    now: datetime
    closes_at: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class GambleResult:
    """@brief 多人奖池用例结果 / Multiplayer-pool use-case result."""

    code: GambleCode
    session: GambleSession | None = None
    balance: int | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class PlaceGambleBet:
    """@brief 参加多人奖池命令 / Join-multiplayer-pool command."""

    session_id: GameSessionId
    user_id: int
    display_name: str
    amount: int
    expected_version: int | None
    now: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SettleGamble:
    """@brief 到期奖池结算命令 / Due-pool settlement command."""

    session_id: GameSessionId
    random_ticket: int
    settled_at: datetime


@dataclass(frozen=True, slots=True)
class GambleSettlement:
    """@brief 已提交的多人奖池结算 / Committed multiplayer-pool settlement."""

    session: GambleSession
    winner_id: int | None
    winner_name: str | None
    prize: int
    notification_enqueued: bool
