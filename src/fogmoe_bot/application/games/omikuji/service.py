"""@brief 御神签应用服务 / Omikuji application service."""

from __future__ import annotations

from datetime import date

from fogmoe_bot.application.games.omikuji.models import DrawOmikuji, OmikujiResult
from fogmoe_bot.application.games.ports.omikuji import OmikujiOperations
from fogmoe_bot.domain.games import daily_fortune

OMIKUJI_SERVICE_DATA_KEY = "games.omikuji.service"
"""@brief runtime capability 中御神签服务的键 / Omikuji service capability key."""


class OmikujiService:
    """@brief 事务外确定每日签并委托原子端口 / Determine a daily fortune outside the transaction and delegate persistence."""

    def __init__(self, operations: OmikujiOperations) -> None:
        self._operations = operations

    async def draw(
        self,
        *,
        user_id: int,
        day: date,
        idempotency_key: str,
    ) -> OmikujiResult:
        """@brief 确定每日签并原子扣费保存 / Determine and atomically persist a daily fortune."""

        return await self._operations.draw_omikuji(
            DrawOmikuji(user_id, day, daily_fortune(user_id, day), idempotency_key)
        )
