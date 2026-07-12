"""@brief 骰宝命令与结果 / Sic Bo commands and results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from fogmoe_bot.domain.games import (
    DiceRoll,
    GameSessionId,
    SicBoBet,
    SicBoOutcome,
    SicBoSession,
)


class SicBoCode(StrEnum):
    """@brief 骰宝用例结果代码 / Sic Bo use-case result codes."""

    SUCCESS = "success"
    NOT_REGISTERED = "not_registered"
    ALREADY_ACTIVE = "already_active"
    NO_ACTIVE_SESSION = "no_active_session"
    STALE_VERSION = "stale_version"
    EXPIRED = "expired"
    INSUFFICIENT_COINS = "insufficient_coins"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class OpenSicBo:
    """@brief 开启骰宝会话命令 / Open-Sic-Bo-session command."""

    user_id: int
    chat_id: int
    message_id: int
    now: datetime
    expires_at: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SelectSicBoBet:
    """@brief 选择骰宝下注类型 / Select-Sic-Bo-bet command."""

    session_id: GameSessionId
    user_id: int
    bet: SicBoBet
    expected_version: int | None
    now: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CancelSicBo:
    """@brief 取消骰宝会话 / Cancel-Sic-Bo command."""

    session_id: GameSessionId
    user_id: int
    expected_version: int | None
    now: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PlaySicBo:
    """@brief 使用已生成骰子原子扣费并结算 / Atomically charge and settle with a pre-generated roll."""

    session_id: GameSessionId
    user_id: int
    amount: int
    roll: DiceRoll
    expected_version: int | None
    now: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SicBoResult:
    """@brief 骰宝用例结果 / Sic Bo use-case result."""

    code: SicBoCode
    session: SicBoSession | None = None
    outcome: SicBoOutcome | None = None
    balance: int | None = None
    replayed: bool = False
