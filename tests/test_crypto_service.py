"""@brief Crypto 应用服务与受监督结算循环测试 / Tests for the Crypto application service and supervised settlement loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fogmoe_bot.application.crypto.workflow import (
    AccountSnapshot,
    ActivePrediction,
    CryptoService,
)
from fogmoe_bot.domain.crypto import CoinStake, PredictionDirection, PriceQuote


class _Clock:
    """@brief 固定测试时钟 / Fixed test clock."""

    def __init__(self, now: datetime) -> None:
        """@brief 保存当前时间 / Store the current time."""

        self.value = now

    def now(self) -> datetime:
        """@brief 返回固定时间 / Return the fixed time."""

        return self.value


class _Prices:
    """@brief 计数报价端口 / Counting quote port."""

    def __init__(self) -> None:
        """@brief 初始化调用数 / Initialize the call count."""

        self.calls = 0

    async def current_price(self) -> PriceQuote:
        """@brief 返回固定报价 / Return a fixed quote."""

        self.calls += 1
        return PriceQuote(Decimal("100"))


class _Operations:
    """@brief 可配置内存持久化端口 / Configurable in-memory persistence port."""

    def __init__(self, now: datetime) -> None:
        """@brief 初始化账户与结算状态 / Initialize account and settlement state."""

        self.account = AccountSnapshot(True, 100)
        self.active: ActivePrediction | None = None
        self.due = False
        self.settle_calls = 0
        self.settle_attempts = 0
        self.due_checks = 0
        self.due_failures = 0
        self.settle_failures = 0
        self.block_due: asyncio.Event | None = None
        self.now = now

    async def account_snapshot(self, user_id: int) -> AccountSnapshot:
        """@brief 返回账户 / Return the account."""

        return self.account

    async def active_prediction(
        self, user_id: int, *, now: datetime
    ) -> ActivePrediction | None:
        """@brief 返回可选活跃预测 / Return the optional active prediction."""

        return self.active

    async def chart_token(self, group_id: int):
        """@brief 返回空绑定 / Return no binding."""

        return None

    async def bind_chart(self, command):
        raise AssertionError

    async def clear_chart(self, command):
        raise AssertionError

    async def pending_swap(self, user_id: int):
        return None

    async def submit_swap(self, command):
        raise AssertionError

    async def create_prediction(self, command, *, quote):
        raise AssertionError

    async def has_due_prediction(self, *, now: datetime) -> bool:
        """@brief 返回到期标志 / Return the due flag."""

        self.due_checks += 1
        if self.block_due is not None:
            await self.block_due.wait()
        if self.due_failures:
            self.due_failures -= 1
            raise RuntimeError("temporary due-query failure")
        return self.due

    async def settle_due_predictions(
        self, *, quote: PriceQuote, settled_at: datetime, limit: int
    ) -> int:
        """@brief 记录一次结算并停止连续领取 / Record one settlement and stop continuous claiming."""

        self.settle_attempts += 1
        if self.settle_failures:
            self.settle_failures -= 1
            raise RuntimeError("temporary settlement failure")
        self.settle_calls += 1
        self.due = False
        return 1


def test_overview_avoids_market_call_for_rejection_or_active_state() -> None:
    """@brief 未注册与已有预测不会浪费交易所容量 / Unregistered and active states do not waste exchange capacity."""

    async def scenario() -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        operations = _Operations(now)
        prices = _Prices()
        service = CryptoService(
            accounts=operations,
            charts=operations,
            predictions=operations,
            swaps=operations,
            prices=prices,
            clock=_Clock(now),
        )
        operations.account = AccountSnapshot(False)
        assert not (await service.prediction_overview(1)).account.registered
        assert prices.calls == 0
        operations.account = AccountSnapshot(True, 100)
        operations.active = ActivePrediction(
            PredictionDirection.UP,
            CoinStake(20),
            PriceQuote(Decimal("99")),
            now,
            now + timedelta(minutes=10),
        )
        assert (await service.prediction_overview(1)).active is not None
        assert prices.calls == 0
        operations.active = None
        assert (await service.prediction_overview(1)).quote is not None
        assert prices.calls == 1

    asyncio.run(scenario())


def test_runtime_recovers_from_transient_repository_failures() -> None:
    """@brief 查询与提交瞬态失败仅退避当前轮次 / Transient query and commit failures back off only the current pass."""

    async def scenario() -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        operations = _Operations(now)
        operations.due = True
        operations.due_failures = 1
        operations.settle_failures = 1
        prices = _Prices()
        service = CryptoService(
            accounts=operations,
            charts=operations,
            predictions=operations,
            swaps=operations,
            prices=prices,
            poll_interval=0.001,
            failure_interval=0.001,
            clock=_Clock(now),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(service.run(stop))
        for _ in range(200):
            if operations.settle_calls:
                break
            await asyncio.sleep(0.001)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

        assert operations.due_checks >= 3
        assert operations.settle_attempts == 2
        assert operations.settle_calls == 1
        assert prices.calls == 2

    asyncio.run(scenario())


def test_runtime_propagates_external_cancellation() -> None:
    """@brief 外部取消穿透持久化等待 / External cancellation propagates through a persistence wait."""

    async def scenario() -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        operations = _Operations(now)
        operations.block_due = asyncio.Event()
        service = CryptoService(
            accounts=operations,
            charts=operations,
            predictions=operations,
            swaps=operations,
            prices=_Prices(),
            clock=_Clock(now),
        )
        task = asyncio.create_task(service.run(asyncio.Event()))
        while operations.due_checks == 0:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_runtime_fetches_price_only_for_due_work_and_obeys_stop() -> None:
    """@brief worker 仅为到期工作取价且由 stop_event 收束 / Worker fetches a quote only for due work and is bounded by stop_event."""

    async def scenario() -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        operations = _Operations(now)
        operations.due = True
        prices = _Prices()
        service = CryptoService(
            accounts=operations,
            charts=operations,
            predictions=operations,
            swaps=operations,
            prices=prices,
            poll_interval=0.01,
            clock=_Clock(now),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(service.run(stop))
        for _ in range(20):
            if operations.settle_calls:
                break
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        assert operations.settle_calls == 1
        assert prices.calls == 1

    asyncio.run(scenario())
