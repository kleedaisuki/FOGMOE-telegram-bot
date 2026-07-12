"""@brief 骰宝应用端口 / Application ports for Sic Bo."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fogmoe_bot.application.games.sicbo.models import (
    CancelSicBo,
    OpenSicBo,
    PlaySicBo,
    SelectSicBoBet,
    SicBoResult,
)
from fogmoe_bot.domain.games import DiceRoll, SicBoSession


class SicBoOperations(Protocol):
    """@brief 骰宝原子持久化端口 / Atomic persistence port for Sic Bo.

    @note 扣费、派奖、会话结束与回执必须处于同一短事务 /
    Charging, payout, session completion, and receipt must share one short transaction.
    """

    async def open_sicbo(self, command: OpenSicBo) -> SicBoResult:
        """@brief 原子开启单人骰宝 / Atomically open single-player Sic Bo."""

        ...

    async def active_sicbo(self, user_id: int, now: datetime) -> SicBoSession | None:
        """@brief 读取玩家活动骰宝会话 / Read a player's active Sic Bo session."""

        ...

    async def select_sicbo_bet(self, command: SelectSicBoBet) -> SicBoResult:
        """@brief 以 OCC 选择下注 / Select a wager with OCC."""

        ...

    async def cancel_sicbo(self, command: CancelSicBo) -> SicBoResult:
        """@brief 以 OCC 取消会话 / Cancel a session with OCC."""

        ...

    async def play_sicbo(self, command: PlaySicBo) -> SicBoResult:
        """@brief 原子扣费、派奖并结束会话 / Atomically charge, pay, and finish a session."""

        ...

    async def expire_sicbo(self, now: datetime, *, limit: int) -> int:
        """@brief 有界过期遗留会话 / Expire stranded sessions in a bounded batch."""

        ...


class DiceSource(Protocol):
    """@brief 骰子随机源 / Dice randomness source."""

    def roll_three(self) -> DiceRoll:
        """@brief 掷三枚六面骰 / Roll three six-sided dice.

        @return 骰子结果 / Dice result.
        """

        ...
