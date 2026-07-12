"""@brief Binance BTC 模式数据源适配器 / Binance BTC pattern-source adapter."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, cast

from binance.error import ClientError  # type: ignore[import-untyped]
from binance.um_futures import UMFutures  # type: ignore[import-untyped]
from requests.exceptions import ConnectionError, ReadTimeout
from urllib3.exceptions import ProtocolError

from fogmoe_bot.application.crypto.market_monitor import (
    PatternScan,
    PatternTrigger,
)
from fogmoe_bot.infrastructure.blocking import (
    AsyncBlockingBulkhead,
    BlockingCallQueueFull,
    BlockingCallTimedOut,
)


class BinanceClient(Protocol):
    """@brief 本适配器使用的最小 Binance SDK 端口 / Minimal Binance SDK port used by this adapter."""

    def mark_price(self, symbol: str) -> Mapping[str, object]:
        """@brief 返回标记价格 / Return the mark price.

        @param symbol 交易对 / Symbol.
        @return SDK 响应 / SDK response.
        """

        ...

    def mark_price_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int,
    ) -> Sequence[Sequence[object]]:
        """@brief 返回标记价格 K 线 / Return mark-price candles.

        @param symbol 交易对 / Symbol.
        @param interval K 线周期 / Candle interval.
        @param limit 最大条数 / Maximum rows.
        @return SDK K 线行 / SDK candle rows.
        """

        ...


type ClientFactory = Callable[[int | None], BinanceClient]
"""@brief 可注入 Binance client factory / Injectable Binance-client factory."""


@dataclass(frozen=True, slots=True)
class _Candle:
    """@brief 已校验 K 线 / Validated candle.

    @param opened_at 开始时间 / Open time.
    @param open 开盘价 / Open price.
    @param high 最高价 / High price.
    @param low 最低价 / Low price.
    @param close 收盘价 / Close price.
    """

    opened_at: datetime
    open: float
    high: float
    low: float
    close: float


def _default_client_factory(timeout: int | None) -> BinanceClient:
    """@brief 创建 Binance SDK client / Create a Binance SDK client.

    @param timeout 可选请求超时 / Optional request timeout.
    @return 窄类型 client / Narrowly typed client.
    """

    client = UMFutures(timeout=timeout) if timeout is not None else UMFutures()
    return cast(BinanceClient, client)


class BinanceBtcPatternSource:
    """@brief 在线程边界调用同步 Binance SDK / Call the synchronous Binance SDK at a thread boundary."""

    def __init__(
        self,
        *,
        client_factory: ClientFactory = _default_client_factory,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        bulkhead: AsyncBlockingBulkhead,
        body_ratio_threshold: float = 0.7,
        green_vs_red_ratio: float = 1.0,
    ) -> None:
        """@brief 创建数据源 / Create the source.

        @param client_factory 可注入 SDK client factory / Injectable SDK-client factory.
        @param sleep 同步重试等待 / Synchronous retry sleep.
        @param now 可替换 UTC 时钟 / Replaceable UTC clock.
        @param bulkhead 同步 SDK 的有界隔舱 / Bounded bulkhead for the synchronous SDK.
        @param body_ratio_threshold 第一红柱实体比例阈值 / First-red-candle body-ratio threshold.
        @param green_vs_red_ratio 绿柱相对前红柱涨幅阈值 / Green-to-previous-red change threshold.
        @raise ValueError 阈值非法时抛出 / Raised for invalid thresholds.
        """

        if not 0 <= body_ratio_threshold <= 1:
            raise ValueError("body_ratio_threshold must be between zero and one")
        if green_vs_red_ratio <= 0:
            raise ValueError("green_vs_red_ratio must be positive")
        self._client_factory = client_factory
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._bulkhead = bulkhead
        self._body_ratio_threshold = body_ratio_threshold
        self._green_vs_red_ratio = green_vs_red_ratio

    async def scan(self) -> PatternScan:
        """@brief 在线程中扫描模式 / Scan for a pattern in a worker thread.

        @return 扫描结果 / Scan result.
        """

        try:
            return await self._bulkhead.call(self._scan_sync)
        except (BlockingCallQueueFull, BlockingCallTimedOut) as error:
            return PatternScan((f"Binance SDK 暂时繁忙: {error}",))

    async def evaluate(self, trigger: PatternTrigger) -> str:
        """@brief 在线程中复查触发 / Evaluate a trigger in a worker thread.

        @param trigger 待复查触发 / Trigger to evaluate.
        @return 结果文本 / Result text.
        """

        try:
            return await self._bulkhead.call(lambda: self._evaluate_sync(trigger))
        except (BlockingCallQueueFull, BlockingCallTimedOut) as error:
            return f"检查结果时 Binance SDK 暂时繁忙: {error}"

    def _scan_sync(self) -> PatternScan:
        """@brief 同步 SDK 扫描实现 / Synchronous SDK scan implementation.

        @return 扫描结果 / Scan result.
        """

        try:
            client = self._client_factory(30)
            raw_rows: Sequence[Sequence[object]] | None = None
            for attempt in range(3):
                try:
                    raw_rows = client.mark_price_klines(
                        "BTCUSDT",
                        "5m",
                        limit=3,
                    )
                    break
                except (ConnectionError, ProtocolError, ReadTimeout) as error:
                    if attempt == 2:
                        return PatternScan((f"连接错误 (尝试 3 次): {error}",))
                    self._sleep(5)

            if raw_rows is None or len(raw_rows) < 3:
                return PatternScan(("获取数据不足",))
            candles = tuple(self._parse_candle(row) for row in raw_rows[:3])
            if not (
                candles[0].close < candles[0].open
                and candles[1].close < candles[1].open
                and candles[2].close > candles[2].open
            ):
                return PatternScan()

            first_ratio = self._body_ratio(candles[0])
            green_change = self._price_change(candles[2])
            previous_red_change = abs(self._price_change(candles[1]))
            if (
                first_ratio < self._body_ratio_threshold
                or green_change < previous_red_change * self._green_vs_red_ratio
            ):
                return PatternScan()

            trigger = PatternTrigger(
                price=candles[2].close,
                occurred_at=candles[2].opened_at + timedelta(minutes=5),
            )
            message = self._format_trigger_message(
                trigger.price,
                self._now() + timedelta(minutes=10),
            )
            return PatternScan((message,), trigger)
        except ClientError as error:
            detail = getattr(error, "error_message", str(error))
            return PatternScan((f"API错误: {detail}",))
        except Exception as error:
            return PatternScan((f"发生未知错误: {error}",))

    def _evaluate_sync(self, trigger: PatternTrigger) -> str:
        """@brief 同步 SDK 复查实现 / Synchronous SDK evaluation implementation.

        @param trigger 待复查触发 / Trigger to evaluate.
        @return 结果文本 / Result text.
        """

        try:
            response = self._client_factory(None).mark_price("BTCUSDT")
            current_price = self._number(response.get("markPrice"))
            price_change = (current_price - trigger.price) / trigger.price * 100
            return self._format_result_message(
                trigger,
                current_price,
                price_change,
                current_price > trigger.price,
            )
        except Exception as error:
            return f"检查结果时发生错误: {error}"

    @classmethod
    def _parse_candle(cls, row: Sequence[object]) -> _Candle:
        """@brief 严格解析 SDK K 线行 / Strictly parse one SDK candle row.

        @param row SDK K 线行 / SDK candle row.
        @return 已校验 K 线 / Validated candle.
        @raise ValueError 字段不足或价格非法时抛出 / Raised for missing or invalid fields.
        """

        if len(row) < 5:
            raise ValueError("Binance candle row has fewer than five fields")
        opened_at = datetime.fromtimestamp(
            cls._number(row[0]) / 1000,
            tz=timezone.utc,
        )
        return _Candle(
            opened_at=opened_at,
            open=cls._number(row[1]),
            high=cls._number(row[2]),
            low=cls._number(row[3]),
            close=cls._number(row[4]),
        )

    @staticmethod
    def _number(value: object) -> float:
        """@brief 将 SDK 数值字段规范为有限浮点数 / Normalize an SDK numeric field to float.

        @param value SDK 字段 / SDK field.
        @return 浮点数 / Floating-point value.
        @raise ValueError 字段不是数值时抛出 / Raised when the field is not numeric.
        """

        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise ValueError(
                f"Expected numeric Binance field, got {type(value).__name__}"
            )
        return float(value)

    @staticmethod
    def _body_ratio(candle: _Candle) -> float:
        """@brief 计算 K 线实体比例 / Calculate candle body ratio.

        @param candle K 线 / Candle.
        @return 实体占总长度比例 / Body-to-total-length ratio.
        """

        total = candle.high - candle.low
        return 0.0 if total == 0 else abs(candle.close - candle.open) / total

    @staticmethod
    def _price_change(candle: _Candle) -> float:
        """@brief 计算 K 线价格变化百分比 / Calculate candle percentage change.

        @param candle K 线 / Candle.
        @return 百分比变化 / Percentage change.
        """

        if candle.open == 0:
            raise ValueError("Candle open price cannot be zero")
        return (candle.close - candle.open) / candle.open * 100

    @staticmethod
    def _format_trigger_message(price: float, next_available: datetime) -> str:
        """@brief 格式化触发消息 / Format a trigger message.

        @param price 触发价格 / Trigger price.
        @param next_available 复查时间 / Evaluation time.
        @return 用户可见消息 / User-visible message.
        """

        return (
            "\n=== 检测到BTCUSDT事件合约模式目标 ===\n"
            f"当前价格: ${price:,.2f}\n"
            "时间单位: 10分钟\n"
            "执行操作: 上涨\n"
            "数量: 5.00 USDT\n"
            f"下次可用时间: {next_available}\n" + "=" * 35
        )

    @staticmethod
    def _format_result_message(
        trigger: PatternTrigger,
        current_price: float,
        price_change: float,
        succeeded: bool,
    ) -> str:
        """@brief 格式化复查结果 / Format an evaluation result.

        @param trigger 原始触发 / Original trigger.
        @param current_price 当前价格 / Current price.
        @param price_change 价格变化百分比 / Percentage price change.
        @param succeeded 是否上涨 / Whether price increased.
        @return 用户可见消息 / User-visible message.
        """

        result = (
            "\n=== BTCUSDT事件合约模式结果检查 ===\n"
            f"触发时间: ${trigger.occurred_at.timestamp()}\n"
            f"触发时价格: ${trigger.price:,.2f}\n"
            f"当前价格: ${current_price:,.2f}\n"
            f"价格变化: {price_change:.2f}%\n"
        )
        result += (
            "结果: 胜利 ✅\n数量变化: +9.00 USDT\n"
            if succeeded
            else "结果: 失败 ❌\n数量变化: -5.00 USDT\n"
        )
        return result + "=" * 35


__all__ = ["BinanceBtcPatternSource", "BinanceClient", "ClientFactory"]
