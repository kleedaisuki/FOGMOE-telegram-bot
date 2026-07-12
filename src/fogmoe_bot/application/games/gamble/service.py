"""@brief 多人奖池应用服务 / Multiplayer-pool application service."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from fogmoe_bot.application.games.gamble.models import (
    GambleResult,
    OpenGamble,
    PlaceGambleBet,
)
from fogmoe_bot.application.games.ports.gamble import GambleOperations

GAMBLE_SERVICE_DATA_KEY = "games.gamble.service"
"""@brief runtime capability 中多人奖池服务的键 / Multiplayer-pool service capability key."""

GAMBLE_AMOUNTS: Final = frozenset({5, 10, 20})
"""@brief 多人奖池允许押注额 / Allowed multiplayer-pool wagers."""


class GambleService:
    """@brief 校验奖池命令并委托原子端口 / Validate pool commands and delegate atomic operations."""

    def __init__(self, operations: GambleOperations) -> None:
        self._operations = operations

    async def open(self, command: OpenGamble) -> GambleResult:
        """@brief 开启奖池 / Open a pool."""

        if command.closes_at <= command.now:
            raise ValueError("Gamble deadline must follow its opening time")
        return await self._operations.open_gamble(command)

    async def place_bet(self, command: PlaceGambleBet) -> GambleResult:
        """@brief 校验固定押注额并参加奖池 / Validate a fixed wager and join the pool."""

        if command.amount not in GAMBLE_AMOUNTS:
            raise ValueError("Unsupported gamble wager")
        return await self._operations.place_gamble_bet(command)

    async def active(self, now: datetime) -> GambleResult:
        """@brief 读取未到期活动奖池 / Read the unexpired active pool."""

        return await self._operations.active_gamble(now)
