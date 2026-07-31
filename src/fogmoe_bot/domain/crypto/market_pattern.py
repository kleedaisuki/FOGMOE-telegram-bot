"""@brief 行情 K 线与价格形态领域模型 / Market-candle and price-pattern domain model.

本模块只表达与交易所、交易对和消息文案无关的市场事实与规则。同步 SDK 响应、重试和
用户通知属于外层 adapter；这里负责让非法价格、时间和 OHLC 关系无法进入形态判定。
/ This module expresses market facts and rules independently of an exchange, symbol, or message
copy. Synchronous SDK responses, retries, and user notifications belong to outer adapters; this
module prevents invalid prices, timestamps, and OHLC relationships from entering pattern logic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from fogmoe_bot.domain.temporal import ensure_utc


def _positive_finite_number(value: int | float, *, name: str) -> float:
    """@brief 规范一个有限正数 / Normalize one finite positive number.

    @param value 待验证数值 / Value to validate.
    @param name 错误信息中的领域字段名 / Domain field name used in errors.
    @return 规范为 float 的有限正数 / Finite positive value normalized to float.
    @raise TypeError 值不是实数或是 bool 时抛出 / Raised when the value is not real or is bool.
    @raise ValueError 值不是有限正数时抛出 / Raised when the value is not finite and positive.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a real number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return normalized


def _finite_number(value: int | float, *, name: str) -> float:
    """@brief 规范一个有限实数 / Normalize one finite real number.

    @param value 待验证数值 / Value to validate.
    @param name 错误信息中的领域字段名 / Domain field name used in errors.
    @return 规范为 float 的有限值 / Finite value normalized to float.
    @raise TypeError 值不是实数或是 bool 时抛出 / Raised when the value is not real or is bool.
    @raise ValueError 值不是有限数时抛出 / Raised when the value is not finite.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a real number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be finite")
    return normalized


def _utc_instant(value: datetime, *, name: str) -> datetime:
    """@brief 规范一个 aware UTC 时刻 / Normalize one aware UTC instant.

    @param value 待验证时间 / Timestamp to validate.
    @param name 错误信息中的领域字段名 / Domain field name used in errors.
    @return 规范为 UTC 的 aware datetime / Aware datetime normalized to UTC.
    @raise TypeError 值不是 datetime 时抛出 / Raised when the value is not a datetime.
    @raise TemporalValueError 值是 naive datetime 时抛出 / Raised for a naive datetime.
    """

    if type(value) is not datetime:
        raise TypeError(f"{name} must be a datetime")
    return ensure_utc(value)


@dataclass(frozen=True, slots=True)
class MarketCandle:
    """@brief 一根拥有完整 OHLC 不变量的市场 K 线 / Market candle with complete OHLC invariants.

    @param opened_at K 线开始的 aware 时刻 / Aware candle-open instant.
    @param closed_at K 线结束的 aware 时刻 / Aware candle-close instant.
    @param open 开盘价 / Open price.
    @param high 最高价 / High price.
    @param low 最低价 / Low price.
    @param close 收盘价 / Close price.
    """

    opened_at: datetime
    """@brief 规范为 UTC 的开始时刻 / Open instant canonicalized to UTC."""

    closed_at: datetime
    """@brief 规范为 UTC 的结束时刻 / Close instant canonicalized to UTC."""

    open: float
    """@brief 有限正开盘价 / Finite positive open price."""

    high: float
    """@brief 不低于开盘价和收盘价的有限正最高价 / Finite positive high at least as large as open and close."""

    low: float
    """@brief 不高于开盘价和收盘价的有限正最低价 / Finite positive low no greater than open and close."""

    close: float
    """@brief 有限正收盘价 / Finite positive close price."""

    def __post_init__(self) -> None:
        """@brief 建立时间、价格与 OHLC 不变量 / Establish time, price, and OHLC invariants.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised when a field has the wrong type.
        @raise ValueError 时间顺序、价格或 OHLC 关系非法时抛出 / Raised for invalid ordering, prices, or OHLC relationships.
        """

        opened_at = _utc_instant(self.opened_at, name="Candle opened_at")
        closed_at = _utc_instant(self.closed_at, name="Candle closed_at")
        if closed_at <= opened_at:
            raise ValueError("Candle closed_at must be later than opened_at")

        open_price = _positive_finite_number(self.open, name="Candle open")
        high_price = _positive_finite_number(self.high, name="Candle high")
        low_price = _positive_finite_number(self.low, name="Candle low")
        close_price = _positive_finite_number(self.close, name="Candle close")
        if high_price < max(open_price, close_price):
            raise ValueError("Candle high must not be below open or close")
        if low_price > min(open_price, close_price):
            raise ValueError("Candle low must not be above open or close")

        object.__setattr__(self, "opened_at", opened_at)
        object.__setattr__(self, "closed_at", closed_at)
        object.__setattr__(self, "open", open_price)
        object.__setattr__(self, "high", high_price)
        object.__setattr__(self, "low", low_price)
        object.__setattr__(self, "close", close_price)

    @property
    def is_bearish(self) -> bool:
        """@brief 是否为严格下跌 K 线 / Whether this is a strictly bearish candle.

        @return 收盘价低于开盘价时为 True / True when close is below open.
        """

        return self.close < self.open

    @property
    def is_bullish(self) -> bool:
        """@brief 是否为严格上涨 K 线 / Whether this is a strictly bullish candle.

        @return 收盘价高于开盘价时为 True / True when close is above open.
        """

        return self.close > self.open

    @property
    def body_to_range_ratio(self) -> float:
        """@brief 返回实体占完整振幅的比例 / Return body size as a fraction of the full range.

        @return 闭区间 0 到 1 内的比例 / Ratio in the inclusive interval from zero to one.
        @note 无振幅的平盘 K 线返回 0 / A flat zero-range candle returns zero.
        """

        total_range = self.high - self.low
        if total_range == 0:
            return 0.0
        return abs(self.close - self.open) / total_range

    @property
    def percentage_change(self) -> float:
        """@brief 返回从开盘到收盘的百分比变化 / Return percentage change from open to close.

        @return 可正、可负或为零的百分比 / Percentage that may be positive, negative, or zero.
        """

        return (self.close - self.open) / self.open * 100


@dataclass(frozen=True, slots=True)
class PatternTrigger:
    """@brief 一次等待复查的市场形态触发 / Market-pattern trigger awaiting evaluation.

    @param price 形态完成时的有限正价格 / Finite positive price when the pattern completed.
    @param occurred_at 形态完成的 aware 时刻 / Aware instant when the pattern completed.
    """

    price: float
    """@brief 形态完成价格 / Pattern-completion price."""

    occurred_at: datetime
    """@brief 规范为 UTC 的形态完成时刻 / Pattern-completion instant canonicalized to UTC."""

    def __post_init__(self) -> None:
        """@brief 建立触发价格和时间不变量 / Establish trigger price and time invariants.

        @return None / None.
        @raise TypeError 字段类型非法时抛出 / Raised when a field has the wrong type.
        @raise ValueError 价格或时间非法时抛出 / Raised when the price or timestamp is invalid.
        """

        object.__setattr__(
            self,
            "price",
            _positive_finite_number(self.price, name="Pattern trigger price"),
        )
        object.__setattr__(
            self,
            "occurred_at",
            _utc_instant(self.occurred_at, name="Pattern trigger occurred_at"),
        )

    def evaluate(self, current_price: float) -> PatternEvaluation:
        """@brief 以当前价格复查形态结果 / Evaluate the pattern against a current price.

        @param current_price 复查时的有限正价格 / Finite positive price at evaluation.
        @return 从本触发产生的不可变复查结果 / Immutable evaluation derived from this trigger.
        """

        return PatternEvaluation(trigger=self, current_price=current_price)


@dataclass(frozen=True, slots=True)
class PatternEvaluation:
    """@brief 形态触发后的价格复查结果 / Price evaluation after a pattern trigger.

    @param trigger 被复查的原始触发 / Original trigger under evaluation.
    @param current_price 复查时的有限正价格 / Finite positive price at evaluation.
    """

    trigger: PatternTrigger
    """@brief 被复查触发 / Evaluated trigger."""

    current_price: float
    """@brief 有限正当前价格 / Finite positive current price."""

    def __post_init__(self) -> None:
        """@brief 建立复查结果的不变量 / Establish evaluation invariants.

        @return None / None.
        @raise TypeError 触发或价格类型非法时抛出 / Raised for an invalid trigger or price type.
        @raise ValueError 当前价格不是有限正数时抛出 / Raised when current price is not finite and positive.
        """

        if not isinstance(self.trigger, PatternTrigger):
            raise TypeError("Pattern evaluation requires a PatternTrigger")
        object.__setattr__(
            self,
            "current_price",
            _positive_finite_number(
                self.current_price,
                name="Pattern evaluation current price",
            ),
        )

    @property
    def percentage_change(self) -> float:
        """@brief 返回相对触发价格的百分比变化 / Return percentage change from the trigger price.

        @return 当前价相对触发价的百分比 / Current price as a percentage change from trigger price.
        """

        return (self.current_price - self.trigger.price) / self.trigger.price * 100

    @property
    def succeeded(self) -> bool:
        """@brief 是否满足既有严格上涨结果 / Whether the established strict-rise outcome succeeded.

        @return 当前价严格高于触发价时为 True / True only when current price exceeds trigger price.
        """

        return self.current_price > self.trigger.price


@dataclass(frozen=True, slots=True)
class RedRedGreenPattern:
    """@brief 红、红、绿三 K 线反转形态规则 / Red-red-green three-candle reversal rule.

    @param body_ratio_threshold 第一根红柱的最小实体比例 / Minimum body ratio for the first red candle.
    @param green_vs_red_ratio 第三根绿柱涨幅相对第二根红柱跌幅的最小倍数 / Minimum third-green change relative to the second-red decline.
    """

    body_ratio_threshold: float = 0.7
    """@brief 第一根红柱实体比例阈值 / First-red body-ratio threshold."""

    green_vs_red_ratio: float = 1.0
    """@brief 第三绿柱与第二红柱变化倍数阈值 / Third-green to second-red change-ratio threshold."""

    def __post_init__(self) -> None:
        """@brief 校验形态阈值 / Validate pattern thresholds.

        @return None / None.
        @raise TypeError 阈值不是实数时抛出 / Raised when a threshold is not real.
        @raise ValueError 阈值越界或非有限时抛出 / Raised when a threshold is out of range or non-finite.
        """

        body_ratio = _finite_number(
            self.body_ratio_threshold,
            name="body_ratio_threshold",
        )
        green_ratio = _finite_number(
            self.green_vs_red_ratio,
            name="green_vs_red_ratio",
        )
        if not 0 <= body_ratio <= 1:
            raise ValueError("body_ratio_threshold must be between zero and one")
        if green_ratio <= 0:
            raise ValueError("green_vs_red_ratio must be positive")
        object.__setattr__(self, "body_ratio_threshold", body_ratio)
        object.__setattr__(self, "green_vs_red_ratio", green_ratio)

    def detect(
        self,
        *,
        first: MarketCandle,
        second: MarketCandle,
        third: MarketCandle,
    ) -> PatternTrigger | None:
        """@brief 判定按时序给出的三根 K 线 / Detect the pattern in three chronologically supplied candles.

        @param first 第一根候选 K 线 / First candidate candle.
        @param second 第二根候选 K 线 / Second candidate candle.
        @param third 第三根候选 K 线 / Third candidate candle.
        @return 命中时返回第三根收盘触发，否则返回 None / Trigger at the third close when matched, otherwise None.
        @raise TypeError 任一输入不是 MarketCandle 时抛出 / Raised when any input is not a MarketCandle.
        @note 输入顺序由行情端口定义；规则不臆测交易所是否提供连续 K 线。/
            Input ordering is defined by the market-data port; the rule does not guess whether an
            exchange supplied contiguous candles.
        """

        candles = (first, second, third)
        if not all(isinstance(candle, MarketCandle) for candle in candles):
            raise TypeError(
                "Red-red-green detection requires three MarketCandle values"
            )
        if not (first.is_bearish and second.is_bearish and third.is_bullish):
            return None
        if first.body_to_range_ratio < self.body_ratio_threshold:
            return None
        if (
            third.percentage_change
            < abs(second.percentage_change) * self.green_vs_red_ratio
        ):
            return None
        return PatternTrigger(price=third.close, occurred_at=third.closed_at)


__all__ = [
    "MarketCandle",
    "PatternEvaluation",
    "PatternTrigger",
    "RedRedGreenPattern",
]
