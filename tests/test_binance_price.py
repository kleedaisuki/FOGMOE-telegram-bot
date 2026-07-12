"""@brief Binance 报价适配器和同步隔舱测试 / Tests for the Binance quote adapter and synchronous bulkhead."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal

import pytest

from fogmoe_bot.application.crypto.workflow import MarketDataUnavailable
from fogmoe_bot.infrastructure.crypto.binance_price import BinanceBtcPriceSource
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead


class _Client:
    """@brief 固定 Binance client / Fixed Binance client."""

    def __init__(self, value: object) -> None:
        """@brief 保存响应值 / Store the response value."""

        self.value = value

    def mark_price(self, symbol: str) -> Mapping[str, object]:
        """@brief 返回测试响应 / Return the test response."""

        assert symbol == "BTCUSDT"
        return {"markPrice": self.value}


def test_price_source_preserves_decimal_precision_and_rejects_invalid_payload() -> None:
    """@brief 报价保持 Decimal 精度且拒绝 SDK 畸形值 / Quote preserves Decimal precision and rejects malformed SDK values."""

    async def scenario() -> None:
        source = BinanceBtcPriceSource(
            client_factory=lambda: _Client("12345.67890123"),
            bulkhead=AsyncBlockingBulkhead(),
            max_attempts=1,
        )
        assert (await source.current_price()).value == Decimal("12345.67890123")
        invalid = BinanceBtcPriceSource(
            client_factory=lambda: _Client(True),
            bulkhead=AsyncBlockingBulkhead(),
            max_attempts=1,
        )
        with pytest.raises(MarketDataUnavailable):
            await invalid.current_price()

    asyncio.run(scenario())
