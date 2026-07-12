"""@brief 有界 Binance BTC 报价适配器 / Bounded Binance BTC quote adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

from binance.error import ClientError  # type: ignore[import-untyped]
from binance.um_futures import UMFutures  # type: ignore[import-untyped]
from requests.exceptions import RequestException
from urllib3.exceptions import ProtocolError

from fogmoe_bot.application.crypto.workflow import MarketDataUnavailable
from fogmoe_bot.domain.crypto import PriceQuote
from fogmoe_bot.infrastructure.blocking import (
    AsyncBlockingBulkhead,
    BlockingCallQueueFull,
    BlockingCallTimedOut,
)


class BinanceMarkPriceClient(Protocol):
    """@brief 报价适配器使用的最小 Binance SDK 端口 / Minimal Binance SDK port used by the quote adapter."""

    def mark_price(self, symbol: str) -> Mapping[str, object]:
        """@brief 返回标记价格响应 / Return the mark-price response.

        @param symbol 交易对 / Symbol.
        @return SDK 响应 / SDK response.
        """

        ...


type MarkPriceClientFactory = Callable[[], BinanceMarkPriceClient]
"""@brief 可注入报价 client factory / Injectable mark-price client factory."""


def _default_client_factory() -> BinanceMarkPriceClient:
    """@brief 创建带 SDK 网络超时的 Binance client / Create a Binance client with an SDK network timeout.

    @return 窄类型 client / Narrowly typed client.
    """

    return cast(BinanceMarkPriceClient, UMFutures(timeout=10))


class BinanceBtcPriceSource:
    """@brief 通过显式隔舱提供 BTC/USDT 报价 / Provide BTC/USDT quotes through an explicit bulkhead."""

    def __init__(
        self,
        *,
        client_factory: MarkPriceClientFactory = _default_client_factory,
        bulkhead: AsyncBlockingBulkhead,
        max_attempts: int = 3,
        retry_delay: float = 0.25,
    ) -> None:
        """@brief 创建报价适配器 / Create the quote adapter.

        @param client_factory 可注入 SDK client factory / Injectable SDK-client factory.
        @param bulkhead 共享或专用同步隔舱 / Shared or dedicated synchronous bulkhead.
        @param max_attempts 最大尝试次数 / Maximum attempts.
        @param retry_delay 尝试间异步延迟秒数 / Async delay between attempts.
        @raise ValueError 重试参数非法时抛出 / Raised for invalid retry parameters.
        """

        if max_attempts < 1 or retry_delay < 0:
            raise ValueError("Invalid Binance retry policy")
        self._client_factory = client_factory
        self._bulkhead = bulkhead
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay

    async def current_price(self) -> PriceQuote:
        """@brief 获取并严格解析当前 BTC 标记价格 / Fetch and strictly parse the current BTC mark price.

        @return 正数精确报价 / Positive exact quote.
        @raise MarketDataUnavailable 所有尝试失败或响应非法时抛出 / Raised when all attempts fail or the response is invalid.
        """

        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = await self._bulkhead.call(self._mark_price_sync)
                value = response.get("markPrice")
                if isinstance(value, bool) or not isinstance(value, str | int | float):
                    raise ValueError("Binance markPrice is missing or non-numeric")
                return PriceQuote(Decimal(str(value)))
            except (
                BlockingCallQueueFull,
                BlockingCallTimedOut,
                ClientError,
                InvalidOperation,
                ProtocolError,
                RequestException,
                ValueError,
            ) as error:
                last_error = error
                if attempt + 1 < self._max_attempts and self._retry_delay:
                    await asyncio.sleep(self._retry_delay)
        raise MarketDataUnavailable(f"Binance BTC quote failed: {last_error}")

    def _mark_price_sync(self) -> Mapping[str, object]:
        """@brief 在线程内执行一次同步 SDK 调用 / Execute one synchronous SDK call in a worker thread.

        @return 原始标记价格响应 / Raw mark-price response.
        """

        return self._client_factory().mark_price("BTCUSDT")


__all__ = [
    "BinanceBtcPriceSource",
    "BinanceMarkPriceClient",
    "MarkPriceClientFactory",
]
