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
    INSUFFICIENT_FREE_TOKENS = "insufficient_coins"
    """@brief Free 钱包余额不足 / Insufficient Free-wallet tokens.

    @note 持久化值保留 ``insufficient_coins``，以兼容迁移前的御神签回执；
        The persisted value remains ``insufficient_coins`` for compatibility with pre-migration
        Omikuji receipts.
    """
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
    """@brief 御神签结果 / Omikuji result.

    @param code 稳定结果代码 / Stable result code.
    @param fortune 当日签文 / Daily fortune.
    @param charged 是否首次从 Free 钱包扣除供奉 / Whether the first offering was charged from the Free wallet.
    @param balance 事务观察到的 Free 钱包余额 / Free-wallet balance observed by the transaction.
    @param replayed 是否来自幂等回放 / Whether the result came from an idempotent replay.
    """

    code: OmikujiCode
    fortune: FortuneLevel | None = None
    charged: bool = False
    balance: int | None = None
    replayed: bool = False
