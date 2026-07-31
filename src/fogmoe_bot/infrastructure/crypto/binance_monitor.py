"""@brief Binance BTC 模式数据源适配器 / Binance BTC pattern-source adapter."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol, cast

from binance.error import ClientError  # type: ignore[import-untyped]
from binance.um_futures import UMFutures  # type: ignore[import-untyped]
from requests.exceptions import ConnectionError, ReadTimeout
from urllib3.exceptions import ProtocolError

from fogmoe_bot.application.crypto.market_monitor import PatternScan
from fogmoe_bot.domain.crypto.market_pattern import (
    MarketCandle,
    PatternEvaluation,
    PatternTrigger,
    RedRedGreenPattern,
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
        pattern: RedRedGreenPattern = RedRedGreenPattern(),
    ) -> None:
        """@brief 创建数据源 / Create the source.

        @param client_factory 可注入 SDK client factory / Injectable SDK-client factory.
        @param sleep 同步重试等待 / Synchronous retry sleep.
        @param now 可替换 UTC 时钟 / Replaceable UTC clock.
        @param bulkhead 同步 SDK 的有界隔舱 / Bounded bulkhead for the synchronous SDK.
        @param pattern 已建立阈值不变量的行情领域规则 / Market-domain rule with established threshold invariants.
        @raise TypeError pattern 不是 RedRedGreenPattern 时抛出 / Raised when pattern is not RedRedGreenPattern.
        """

        if not isinstance(pattern, RedRedGreenPattern):
            raise TypeError("pattern must be a RedRedGreenPattern")
        self._client_factory = client_factory
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._bulkhead = bulkhead
        self._pattern = pattern

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
            trigger = self._pattern.detect(
                first=candles[0],
                second=candles[1],
                third=candles[2],
            )
            if trigger is None:
                return PatternScan()

            message = self._format_trigger_message(
                trigger,
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
            return self._format_result_message(trigger.evaluate(current_price))
        except Exception as error:
            return f"检查结果时发生错误: {error}"

    @classmethod
    def _parse_candle(cls, row: Sequence[object]) -> MarketCandle:
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
        return MarketCandle(
            opened_at=opened_at,
            closed_at=opened_at + timedelta(minutes=5),
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
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Expected finite Binance numeric field")
        return number

    @staticmethod
    def _format_trigger_message(
        trigger: PatternTrigger,
        next_available: datetime,
    ) -> str:
        """@brief 格式化触发消息 / Format a trigger message.

        @param trigger 领域形态触发 / Domain pattern trigger.
        @param next_available 复查时间 / Evaluation time.
        @return 用户可见消息 / User-visible message.
        """

        return (
            "\n=== 检测到BTCUSDT事件合约模式目标 ===\n"
            f"当前价格: ${trigger.price:,.2f}\n"
            "时间单位: 10分钟\n"
            "执行操作: 上涨\n"
            "数量: 5.00 USDT\n"
            f"下次可用时间: {next_available}\n" + "=" * 35
        )

    @staticmethod
    def _format_result_message(
        evaluation: PatternEvaluation,
    ) -> str:
        """@brief 格式化复查结果 / Format an evaluation result.

        @param evaluation 领域复查结果 / Domain evaluation result.
        @return 用户可见消息 / User-visible message.
        """

        result = (
            "\n=== BTCUSDT事件合约模式结果检查 ===\n"
            f"触发时间: ${evaluation.trigger.occurred_at.timestamp()}\n"
            f"触发时价格: ${evaluation.trigger.price:,.2f}\n"
            f"当前价格: ${evaluation.current_price:,.2f}\n"
            f"价格变化: {evaluation.percentage_change:.2f}%\n"
        )
        result += (
            "结果: 胜利 ✅\n数量变化: +9.00 USDT\n"
            if evaluation.succeeded
            else "结果: 失败 ❌\n数量变化: -5.00 USDT\n"
        )
        return result + "=" * 35


__all__ = ["BinanceBtcPatternSource", "BinanceClient", "ClientFactory"]
