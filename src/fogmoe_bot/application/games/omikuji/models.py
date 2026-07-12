"""@brief 御神签命令与结果 / Omikuji commands and results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from fogmoe_bot.domain.games import FortuneLevel


class OmikujiCode(StrEnum):
    """@brief 御神签用例结果代码 / Omikuji use-case result codes."""

    SUCCESS = "success"
    NOT_REGISTERED = "not_registered"
    INSUFFICIENT_COINS = "insufficient_coins"
    ALREADY_DRAWN = "already_drawn"


@dataclass(frozen=True, slots=True)
class DrawOmikuji:
    """@brief 每日御神签命令 / Daily Omikuji command."""

    user_id: int
    day: date
    drawn_fortune: FortuneLevel
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class OmikujiResult:
    """@brief 御神签结果 / Omikuji result."""

    code: OmikujiCode
    fortune: FortuneLevel | None = None
    charged: bool = False
    balance: int | None = None
    replayed: bool = False
