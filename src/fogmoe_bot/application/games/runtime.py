"""@brief 到期游戏的可恢复有界 worker / Recoverable bounded worker for due games."""

from __future__ import annotations

import asyncio
import logging
import random

from fogmoe_bot.application.games.gamble.models import SettleGamble
from fogmoe_bot.application.games.ports.gamble import (
    GambleOperations,
    GambleSettlementRenderer,
    TicketSource,
)
from fogmoe_bot.application.games.ports.sicbo import SicBoOperations
from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock

logger = logging.getLogger(__name__)

GAMES_RUNTIME_DATA_KEY = "games.runtime"
"""@brief runtime capability 中游戏 worker 的稳定键 / Stable games-runtime capability key."""


class SystemTicketSource(TicketSource):
    """@brief 使用操作系统熵的奖池票源 / Pool ticket source backed by OS entropy."""

    def __init__(self) -> None:
        """@brief 初始化独立系统随机源 / Initialize an independent system RNG."""

        self._random = random.SystemRandom()
        """@brief 不共享模块级状态的 RNG / RNG independent of module-level state."""

    def ticket(self) -> int:
        """@brief 生成 127 位非负整数 / Generate a non-negative 127-bit integer.

        @return 随机票 / Random ticket.
        """

        return self._random.getrandbits(127)


class GamesRuntime:
    """@brief 结算奖池、补写通知并清理骰宝 / Settle pools, repair notifications, and expire Sic Bo."""

    def __init__(
        self,
        gamble: GambleOperations,
        sicbo: SicBoOperations,
        renderer: GambleSettlementRenderer,
        *,
        clock: UtcClock | None = None,
        tickets: TicketSource | None = None,
        batch_size: int = 32,
        poll_interval: float = 1.0,
    ) -> None:
        """@brief 注入持久化、渲染与运行边界 / Inject persistence, rendering, and runtime bounds.

        @param gamble 多人奖池恢复端口 / Multiplayer-pool recovery port.
        @param sicbo 骰宝过期端口 / Sic Bo expiration port.
        @param renderer 结算通知渲染器 / Settlement-notification renderer.
        @param clock UTC 时钟 / UTC clock.
        @param tickets 随机票源 / Random ticket source.
        @param batch_size 单轮最大条目 / Maximum items per pass.
        @param poll_interval 空闲轮询秒数 / Idle polling seconds.
        """

        if batch_size <= 0 or poll_interval <= 0:
            raise ValueError("Games runtime bounds must be positive")
        self._gamble = gamble
        self._sicbo = sicbo
        self._renderer = renderer
        self._clock = clock or SystemUtcClock()
        self._tickets = tickets or SystemTicketSource()
        """@brief 事务外票源 / Ticket source used outside transactions."""
        self._batch_size = batch_size
        self._poll_interval = poll_interval

    async def run(self, stop_event: asyncio.Event) -> None:
        """@brief 运行直到 runtime 请求停止 / Run until the runtime requests shutdown.

        @param stop_event 统一停止事件 / Unified stop event.
        @return None / None.
        @note ``CancelledError`` 不会被吞掉 / ``CancelledError`` is never swallowed.
        """

        while not stop_event.is_set():
            try:
                work = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Games runtime pass failed")
                work = 0
            if work == 0:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self._poll_interval
                    )
                except TimeoutError:
                    pass

    async def run_once(self) -> int:
        """@brief 执行一轮有界恢复工作 / Execute one bounded recovery pass.

        @return 已处理工作项数 / Number of processed work items.
        """

        now = self._clock.now()
        work = await self._sicbo.expire_sicbo(now, limit=self._batch_size)
        due = await self._gamble.due_gamble_ids(now, limit=self._batch_size)
        for session_id in due:
            try:
                settlement = await self._gamble.settle_gamble(
                    SettleGamble(session_id, self._tickets.ticket(), now)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Gamble settlement failed: session_id=%s", session_id)
                continue
            if settlement is not None:
                work += 1
        pending = await self._gamble.unnotified_gamble_settlements(
            limit=self._batch_size
        )
        for settlement in pending:
            try:
                await self._gamble.enqueue_gamble_notification(
                    settlement,
                    text=self._renderer.render(settlement),
                    enqueued_at=now,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Gamble notification enqueue failed: session_id=%s",
                    settlement.session.session_id,
                )
                continue
            work += 1
        return work


__all__ = ["GamesRuntime", "SystemTicketSource"]
