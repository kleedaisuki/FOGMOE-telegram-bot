"""@brief 御神签应用端口 / Application port for Omikuji."""

from __future__ import annotations

from typing import Protocol

from fogmoe_bot.application.games.omikuji.models import DrawOmikuji, OmikujiResult


class OmikujiOperations(Protocol):
    """@brief 御神签原子持久化端口 / Atomic persistence port for Omikuji.

    @note 每日唯一写入、扣费与回执必须处于同一短事务 /
    Daily uniqueness, charging, and receipt must share one short transaction.
    """

    async def draw_omikuji(self, command: DrawOmikuji) -> OmikujiResult:
        """@brief 原子扣费并保存每日签 / Atomically charge and save the daily fortune."""

        ...
