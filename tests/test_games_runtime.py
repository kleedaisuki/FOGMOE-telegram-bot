"""@brief Games 到期 worker 恢复契约 / Recovery contracts for the Games due-work worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fogmoe_bot.application.games.gamble.models import (
    GambleSettlement,
    SettleGamble,
)
from fogmoe_bot.application.games.ports.gamble import GambleOperations
from fogmoe_bot.application.games.ports.sicbo import SicBoOperations
from fogmoe_bot.application.games.runtime import GamesRuntime
from fogmoe_bot.domain.games import (
    GambleBet,
    GambleSession,
    GameSessionId,
    GameSessionStatus,
)


class _Clock:
    """@brief 固定 UTC 时钟 / Fixed UTC clock."""

    def now(self) -> datetime:
        """@brief 返回固定时间 / Return the fixed instant."""

        return datetime(2030, 1, 1, tzinfo=UTC)


class _Tickets:
    """@brief 确定性票源 / Deterministic ticket source."""

    def ticket(self) -> int:
        """@brief 返回固定票 / Return a fixed ticket."""

        return 7


class _Renderer:
    """@brief 测试结算渲染器 / Test settlement renderer."""

    def render(self, settlement: GambleSettlement) -> str:
        """@brief 渲染稳定文本 / Render stable text."""

        return f"winner={settlement.winner_id}"


class _Operations:
    """@brief 仅实现 worker 所需端口的 fake / Fake implementing only worker-required operations."""

    def __init__(self) -> None:
        """@brief 初始化观察状态 / Initialize observed state."""

        self.session_id = GameSessionId(UUID("00000000-0000-0000-0000-000000000010"))
        self.settle_commands: list[SettleGamble] = []
        self.notifications: list[tuple[str, datetime]] = []

    async def expire_sicbo(self, now: datetime, *, limit: int) -> int:
        """@brief 模拟过期一个会话 / Simulate expiring one session."""

        del now, limit
        return 1

    async def due_gamble_ids(
        self, now: datetime, *, limit: int
    ) -> tuple[GameSessionId, ...]:
        """@brief 返回一个到期奖池 / Return one due pool."""

        del now, limit
        return (self.session_id,)

    async def settle_gamble(self, command: SettleGamble) -> GambleSettlement:
        """@brief 记录结算并返回结果 / Record settlement and return its result."""

        self.settle_commands.append(command)
        return self._settlement(False)

    async def unnotified_gamble_settlements(
        self, *, limit: int
    ) -> tuple[GambleSettlement, ...]:
        """@brief 返回一个通知修复项 / Return one notification repair item."""

        del limit
        return (self._settlement(False),)

    async def enqueue_gamble_notification(
        self,
        settlement: GambleSettlement,
        *,
        text: str,
        enqueued_at: datetime,
    ) -> None:
        """@brief 记录通知入队 / Record notification enqueue."""

        del settlement
        self.notifications.append((text, enqueued_at))

    def _settlement(self, notified: bool) -> GambleSettlement:
        """@brief 创建稳定结算 / Build a stable settlement."""

        session = GambleSession(
            self.session_id,
            -100,
            9,
            datetime(2030, 1, 1, tzinfo=UTC),
            (GambleBet(1, "alice", 5),),
            GameSessionStatus.SETTLED,
            2,
        )
        return GambleSettlement(session, 1, "alice", 5, notified)


class _PoisonOperations(_Operations):
    """@brief 首个奖池失败但第二个可结算的 fake / Fake with one poison pool followed by a healthy pool."""

    def __init__(self) -> None:
        """@brief 创建第二会话 / Create the second session."""

        super().__init__()
        self.second_id = GameSessionId(UUID("00000000-0000-0000-0000-000000000011"))

    async def due_gamble_ids(
        self, now: datetime, *, limit: int
    ) -> tuple[GameSessionId, ...]:
        """@brief 返回 poison 与健康会话 / Return poison and healthy sessions."""

        del now, limit
        return self.session_id, self.second_id

    async def settle_gamble(self, command: SettleGamble) -> GambleSettlement:
        """@brief 首会话抛错，第二会话成功 / Fail the first session and settle the second."""

        if command.session_id == self.session_id:
            raise RuntimeError("poison session")
        self.settle_commands.append(command)
        settlement = self._settlement(False)
        return GambleSettlement(
            GambleSession(
                self.second_id,
                settlement.session.chat_id,
                settlement.session.message_id,
                settlement.session.closes_at,
                settlement.session.bets,
                settlement.session.status,
                settlement.session.version,
            ),
            settlement.winner_id,
            settlement.winner_name,
            settlement.prize,
            settlement.notification_enqueued,
        )

    async def unnotified_gamble_settlements(
        self, *, limit: int
    ) -> tuple[GambleSettlement, ...]:
        """@brief 本场景无通知修复 / Return no notification repairs."""

        del limit
        return ()


def test_runtime_settles_repairs_notifications_and_expires_sessions_in_one_pass() -> (
    None
):
    """@brief 单轮完成三类 durable 恢复工作 / One pass performs all three durable recovery categories."""

    async def scenario() -> None:
        """@brief 驱动单轮 worker / Drive one worker pass."""

        operations = _Operations()
        runtime = GamesRuntime(
            cast(GambleOperations, operations),
            cast(SicBoOperations, operations),
            _Renderer(),
            clock=_Clock(),
            tickets=_Tickets(),
        )

        assert await runtime.run_once() == 3
        assert operations.settle_commands[0].random_ticket == 7
        assert operations.notifications == [
            ("winner=1", datetime(2030, 1, 1, tzinfo=UTC))
        ]

    asyncio.run(scenario())


def test_poison_settlement_does_not_starve_later_due_sessions() -> None:
    """@brief 单个 poison 会话不会阻塞同批后续会话 / One poison session does not block later sessions in the batch."""

    async def scenario() -> None:
        """@brief 驱动隔离失败场景 / Drive the isolated-failure scenario."""

        operations = _PoisonOperations()
        runtime = GamesRuntime(
            cast(GambleOperations, operations),
            cast(SicBoOperations, operations),
            _Renderer(),
            clock=_Clock(),
            tickets=_Tickets(),
        )

        assert await runtime.run_once() == 2
        assert [command.session_id for command in operations.settle_commands] == [
            operations.second_id
        ]

    asyncio.run(scenario())
