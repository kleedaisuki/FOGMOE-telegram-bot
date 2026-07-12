"""@brief 多人奖池应用端口 / Application ports for multiplayer gamble pools."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fogmoe_bot.application.games.gamble.models import (
    GambleResult,
    GambleSettlement,
    OpenGamble,
    PlaceGambleBet,
    SettleGamble,
)
from fogmoe_bot.domain.games import GameSessionId


class GambleOperations(Protocol):
    """@brief 多人奖池原子持久化端口 / Atomic persistence port for multiplayer pools.

    @note 经济写入与业务回执必须处于同一短事务 /
    Economic writes and business receipts must share one short transaction.
    """

    async def open_gamble(self, command: OpenGamble) -> GambleResult:
        """@brief 原子开启全局奖池 / Atomically open the global pool."""

        ...

    async def active_gamble(self, now: datetime) -> GambleResult:
        """@brief 读取未到期活动奖池 / Read the unexpired active pool."""

        ...

    async def place_gamble_bet(self, command: PlaceGambleBet) -> GambleResult:
        """@brief 在 session→account 锁序下扣费并下注 / Charge and wager under session-to-account lock order."""

        ...

    async def due_gamble_ids(
        self, now: datetime, *, limit: int
    ) -> tuple[GameSessionId, ...]:
        """@brief 读取到期奖池身份 / Read due pool identities."""

        ...

    async def settle_gamble(self, command: SettleGamble) -> GambleSettlement | None:
        """@brief 原子结算一个到期奖池 / Atomically settle one due pool."""

        ...

    async def unnotified_gamble_settlements(
        self, *, limit: int
    ) -> tuple[GambleSettlement, ...]:
        """@brief 读取尚未写入 outbox 的结算 / Read settlements not yet written to the outbox."""

        ...

    async def enqueue_gamble_notification(
        self,
        settlement: GambleSettlement,
        *,
        text: str,
        enqueued_at: datetime,
    ) -> None:
        """@brief 幂等写出站编辑并标记通知 / Idempotently enqueue an edit and mark notification."""

        ...


class TicketSource(Protocol):
    """@brief 奖池随机整数源 / Pool random-integer source."""

    def ticket(self) -> int:
        """@brief 返回非负高熵整数 / Return a non-negative high-entropy integer.

        @return 随机整数 / Random integer.
        """

        ...


class GambleSettlementRenderer(Protocol):
    """@brief 到期奖池通知渲染端口 / Due-pool notification rendering port."""

    def render(self, settlement: GambleSettlement) -> str:
        """@brief 渲染结算文本 / Render settlement text.

        @param settlement 已提交结算 / Committed settlement.
        @return connector 可投递文本 / Connector-deliverable text.
        """

        ...
