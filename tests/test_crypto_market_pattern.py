"""@brief 行情形态领域模型测试 / Market-pattern domain model tests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from fogmoe_bot.domain.crypto.market_pattern import (
    MarketCandle,
    PatternTrigger,
    RedRedGreenPattern,
)
from fogmoe_bot.domain.temporal import TemporalValueError


def _candle(
    *,
    minute: int,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> MarketCandle:
    """@brief 构造五分钟领域 K 线 / Build a five-minute domain candle.

    @param minute 相对起点分钟数 / Minute offset from the fixture origin.
    @param open_price 开盘价 / Open price.
    @param high 最高价 / High price.
    @param low 最低价 / Low price.
    @param close 收盘价 / Close price.
    @return 经过约束的 K 线 / Constrained market candle.
    """

    opened_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minute)
    return MarketCandle(
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=5),
        open=open_price,
        high=high,
        low=low,
        close=close,
    )


def _matching_candles() -> tuple[MarketCandle, MarketCandle, MarketCandle]:
    """@brief 构造命中既有阈值的红红绿序列 / Build a red-red-green sequence matching established thresholds.

    @return 按行情顺序排列的三根 K 线 / Three candles in market order.
    """

    return (
        _candle(minute=0, open_price=100, high=102, low=88, close=90),
        _candle(minute=5, open_price=100, high=101, low=94, close=95),
        _candle(minute=10, open_price=95, high=102, low=94, close=101),
    )


def test_candle_canonicalizes_time_and_owns_price_arithmetic() -> None:
    """@brief K 线规范 UTC 并封装实体比例与涨跌幅 / Candle canonicalizes UTC and owns body/change arithmetic."""

    offset = timezone(timedelta(hours=8))
    candle = MarketCandle(
        opened_at=datetime(2026, 1, 1, 8, tzinfo=offset),
        closed_at=datetime(2026, 1, 1, 8, 5, tzinfo=offset),
        open=100,
        high=102,
        low=88,
        close=90,
    )

    assert candle.opened_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert candle.closed_at == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    assert candle.is_bearish is True
    assert candle.is_bullish is False
    assert candle.body_to_range_ratio == pytest.approx(10 / 14)
    assert candle.percentage_change == pytest.approx(-10)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("open", 0.0),
        ("high", float("inf")),
        ("low", float("nan")),
        ("close", -1.0),
    ),
)
def test_candle_rejects_non_positive_or_non_finite_prices(
    field: str,
    value: float,
) -> None:
    """@brief 非有限或非正价格不能成为领域事实 / Non-finite or non-positive prices cannot become domain facts.

    @param field 被破坏的 OHLC 字段 / OHLC field being corrupted.
    @param value 非法价格 / Invalid price.
    """

    values = {"open": 100.0, "high": 102.0, "low": 88.0, "close": 90.0}
    values[field] = value
    with pytest.raises(ValueError, match="finite and positive"):
        MarketCandle(
            opened_at=datetime(2026, 1, 1, tzinfo=UTC),
            closed_at=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
            **values,
        )


@pytest.mark.parametrize(
    ("high", "low", "message"),
    (
        (99.0, 88.0, "high must not be below"),
        (102.0, 91.0, "low must not be above"),
    ),
)
def test_candle_rejects_impossible_ohlc_relationships(
    high: float,
    low: float,
    message: str,
) -> None:
    """@brief 最高价和最低价必须包住开收盘价 / High and low must enclose open and close.

    @param high 候选最高价 / Candidate high.
    @param low 候选最低价 / Candidate low.
    @param message 预期约束错误片段 / Expected invariant-error fragment.
    """

    with pytest.raises(ValueError, match=message):
        _candle(minute=0, open_price=100, high=high, low=low, close=90)


def test_candle_rejects_naive_or_non_forward_time() -> None:
    """@brief K 线必须使用 aware 时间和正区间 / Candle requires aware times and a forward interval."""

    with pytest.raises(TemporalValueError):
        MarketCandle(
            opened_at=datetime(2026, 1, 1),
            closed_at=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
            open=100,
            high=100,
            low=100,
            close=100,
        )
    with pytest.raises(ValueError, match="later than"):
        MarketCandle(
            opened_at=datetime(2026, 1, 1, tzinfo=UTC),
            closed_at=datetime(2026, 1, 1, tzinfo=UTC),
            open=100,
            high=100,
            low=100,
            close=100,
        )


def test_flat_candle_has_zero_body_ratio_and_no_direction() -> None:
    """@brief 平盘 K 线自然成为普通零比例情况 / A flat candle is the ordinary zero-ratio case."""

    candle = _candle(minute=0, open_price=100, high=100, low=100, close=100)

    assert candle.body_to_range_ratio == 0
    assert candle.percentage_change == 0
    assert candle.is_bearish is False
    assert candle.is_bullish is False


def test_red_red_green_rule_returns_a_constrained_close_trigger() -> None:
    """@brief 形态命中时由第三根收盘事实产生触发 / A match produces a trigger from the third close fact."""

    first, second, third = _matching_candles()

    trigger = RedRedGreenPattern().detect(
        first=first,
        second=second,
        third=third,
    )

    assert trigger == PatternTrigger(price=101, occurred_at=third.closed_at)


def test_red_red_green_rule_accepts_exact_threshold_boundaries() -> None:
    """@brief 两个既有阈值都采用含等号边界 / Both established thresholds use inclusive boundaries."""

    first = _candle(minute=0, open_price=100, high=102, low=92, close=93)
    second = _candle(minute=5, open_price=100, high=101, low=94, close=95)
    third = _candle(minute=10, open_price=100, high=106, low=99, close=105)

    trigger = RedRedGreenPattern().detect(
        first=first,
        second=second,
        third=third,
    )

    assert trigger == PatternTrigger(price=105, occurred_at=third.closed_at)


def test_red_red_green_rule_rejects_wrong_direction_or_weak_body() -> None:
    """@brief 颜色或阈值不满足时没有领域触发 / Wrong direction or weak thresholds yield no trigger."""

    first, second, third = _matching_candles()
    wrong_direction = replace(third, open=101, close=95, high=102, low=94)
    weak_first = replace(first, high=110, low=80)
    weak_green = replace(third, close=96, high=102)
    rule = RedRedGreenPattern()

    assert rule.detect(first=first, second=second, third=wrong_direction) is None
    assert rule.detect(first=weak_first, second=second, third=third) is None
    assert rule.detect(first=first, second=second, third=weak_green) is None


@pytest.mark.parametrize(
    ("body_ratio", "green_ratio"),
    (
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (-0.1, 1.0),
        (1.1, 1.0),
        (0.7, 0.0),
        (0.7, float("inf")),
    ),
)
def test_red_red_green_rule_rejects_invalid_thresholds(
    body_ratio: float,
    green_ratio: float,
) -> None:
    """@brief 形态策略不接受模糊或无界阈值 / Pattern policy rejects ambiguous or unbounded thresholds.

    @param body_ratio 候选实体阈值 / Candidate body threshold.
    @param green_ratio 候选相对涨幅阈值 / Candidate relative-change threshold.
    """

    with pytest.raises(ValueError):
        RedRedGreenPattern(
            body_ratio_threshold=body_ratio,
            green_vs_red_ratio=green_ratio,
        )


def test_trigger_evaluates_strict_price_rise_inside_domain() -> None:
    """@brief 触发对象拥有严格上涨和百分比语义 / Trigger owns strict-rise and percentage semantics."""

    trigger = PatternTrigger(
        price=100,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    unchanged = trigger.evaluate(100)
    increased = trigger.evaluate(102)

    assert unchanged.succeeded is False
    assert unchanged.percentage_change == 0
    assert increased.succeeded is True
    assert increased.percentage_change == pytest.approx(2)
    with pytest.raises(ValueError, match="finite and positive"):
        trigger.evaluate(float("nan"))


def test_trigger_rejects_invalid_price_or_naive_time() -> None:
    """@brief 领域触发不能绕开价格与 UTC 不变量 / Domain triggers cannot bypass price and UTC invariants."""

    with pytest.raises(ValueError, match="finite and positive"):
        PatternTrigger(
            price=0,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(TemporalValueError):
        PatternTrigger(price=100, occurred_at=datetime(2026, 1, 1))
