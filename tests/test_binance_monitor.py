"""@brief Binance BTC 模式适配器测试 / Binance BTC pattern-adapter tests."""

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from requests.exceptions import ConnectionError

from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead
from fogmoe_bot.infrastructure.crypto.binance_monitor import (
    BinanceBtcPatternSource,
    BinanceClient,
)


class _Client:
    """@brief 可脚本化 Binance client / Scriptable Binance client."""

    def __init__(self, rows: Sequence[Sequence[object]]) -> None:
        """@brief 保存 K 线 / Store candle rows.

        @param rows SDK 形状 K 线 / SDK-shaped candle rows.
        """

        self.rows = rows
        self.failures = 0
        self.calls = 0

    def mark_price(self, symbol: str) -> Mapping[str, object]:
        """@brief 返回固定价格 / Return a fixed price.

        @param symbol 交易对 / Symbol.
        @return 价格响应 / Price response.
        """

        assert symbol == "BTCUSDT"
        return {"markPrice": "102"}

    def mark_price_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
    ) -> Sequence[Sequence[object]]:
        """@brief 返回或暂时拒绝 K 线 / Return or temporarily reject candle rows.

        @param symbol 交易对 / Symbol.
        @param interval 周期 / Interval.
        @param limit 条数 / Row limit.
        @return K 线 / Candle rows.
        """

        assert (symbol, interval, limit) == ("BTCUSDT", "5m", 3)
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("temporary")
        return self.rows


def _rows() -> tuple[tuple[object, ...], ...]:
    """@brief 构造满足模式的三根 K 线 / Build three candles matching the pattern.

    @return SDK 形状 K 线 / SDK-shaped rows.
    """

    return (
        (1_700_000_000_000, "100", "102", "88", "90"),
        (1_700_000_300_000, "100", "101", "94", "95"),
        (1_700_000_600_000, "95", "102", "94", "101"),
    )


def test_source_parses_pattern_and_evaluates_price() -> None:
    """@brief adapter 严格解析 K 线并复查价格 / Adapter parses candles and evaluates price."""

    async def scenario() -> None:
        """@brief 执行扫描与复查 / Run scan and evaluation.

        @return None / None.
        """

        client = _Client(_rows())

        def factory(timeout: int | None) -> BinanceClient:
            """@brief 返回 fake client / Return the fake client.

            @param timeout 请求超时 / Request timeout.
            @return fake client / Fake client.
            """

            assert timeout in {None, 30}
            return client

        source = BinanceBtcPatternSource(
            client_factory=factory,
            now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            bulkhead=AsyncBlockingBulkhead(),
        )
        scan = await source.scan()

        assert scan.trigger is not None
        assert scan.trigger.price == 101
        assert "当前价格: $101.00" in scan.messages[0]
        result = await source.evaluate(scan.trigger)
        assert "当前价格: $102.00" in result
        assert "胜利" in result

    asyncio.run(scenario())


def test_source_retries_transient_candle_errors_inside_thread_boundary() -> None:
    """@brief 同步 SDK 瞬态错误按预算重试 / Transient synchronous SDK errors retry within budget."""

    async def scenario() -> None:
        """@brief 驱动两次失败后成功 / Drive two failures followed by success.

        @return None / None.
        """

        client = _Client(_rows())
        client.failures = 2
        sleeps: list[float] = []
        source = BinanceBtcPatternSource(
            client_factory=lambda timeout: client,
            sleep=sleeps.append,
            bulkhead=AsyncBlockingBulkhead(),
        )

        scan = await source.scan()

        assert scan.trigger is not None
        assert client.calls == 3
        assert sleeps == [5, 5]

    asyncio.run(scenario())
