"""@brief 每日抽奖聚合与奖励策略测试 / Tests for the daily-lottery aggregate and prize policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from fogmoe_bot.domain.economy.identity import EconomyAccountId
from fogmoe_bot.domain.economy.lottery import (
    PRIZE_BANDS,
    PRIZE_BAND_WEIGHTS,
    DailyLottery,
    LotteryAlreadyClaimed,
    LotteryClaimInstant,
    LotteryGranted,
    LotteryPrize,
    LotteryPrizeBand,
    draw_lottery_prize,
)

FIRST_AT = LotteryClaimInstant(datetime(2030, 1, 2, 3, 4, tzinfo=UTC))
"""@brief 固定首次领取时刻 / Fixed first claim instant."""


class ScriptedLotteryRandomness:
    """@brief 返回指定档位与整数并记录领域参数 / Return scripted values and record domain inputs."""

    def __init__(self, band: LotteryPrizeBand, value: int) -> None:
        """@brief 保存指定随机结果 / Store scripted random results.

        @param band 指定档位 / Scripted band.
        @param value 指定整数 / Scripted integer.
        """

        self._band = band
        """@brief 指定档位 / Scripted band."""
        self._value = value
        """@brief 指定整数 / Scripted integer."""
        self.band_inputs: list[
            tuple[tuple[LotteryPrizeBand, ...], tuple[float, ...]]
        ] = []
        """@brief 收到的档位与权重 / Received bands and weights."""
        self.integer_inputs: list[tuple[int, int]] = []
        """@brief 收到的整数区间 / Received integer intervals."""

    def choose_prize_band(
        self,
        bands: tuple[LotteryPrizeBand, ...],
        weights: tuple[float, ...],
    ) -> LotteryPrizeBand:
        """@brief 记录策略参数并返回指定档位 / Record policy inputs and return the scripted band.

        @param bands 有序候选档位 / Ordered candidate bands.
        @param weights 候选权重 / Candidate weights.
        @return 指定档位 / Scripted band.
        """

        self.band_inputs.append((bands, weights))
        return self._band

    def integer_between(self, lower: int, upper: int) -> int:
        """@brief 记录闭区间并返回指定整数 / Record the inclusive interval and return the scripted integer.

        @param lower 闭区间下界 / Inclusive lower bound.
        @param upper 闭区间上界 / Inclusive upper bound.
        @return 指定整数 / Scripted integer.
        """

        self.integer_inputs.append((lower, upper))
        return self._value


@pytest.mark.parametrize(
    ("band", "value", "bounds"),
    (
        (LotteryPrizeBand.SMALL, 4, (1, 4)),
        (LotteryPrizeBand.LARGE, 20, (11, 20)),
        (LotteryPrizeBand.MEDIUM, 7, (5, 10)),
    ),
)
def test_prize_policy_owns_established_weights_and_band_ranges(
    band: LotteryPrizeBand,
    value: int,
    bounds: tuple[int, int],
) -> None:
    """@brief 领域策略固定既有档位顺序、权重和范围 /
    The domain policy fixes the established band order, weights, and ranges.

    @param band 指定档位 / Scripted band.
    @param value 指定结果 / Scripted result.
    @param bounds 预期闭区间 / Expected inclusive interval.
    @return None / None.
    """

    randomness = ScriptedLotteryRandomness(band, value)

    prize = draw_lottery_prize(randomness)

    assert prize == LotteryPrize(value)
    assert randomness.band_inputs == [(PRIZE_BANDS, PRIZE_BAND_WEIGHTS)]
    assert randomness.integer_inputs == [bounds]


def test_first_claim_rejects_until_but_accepts_at_exact_boundary() -> None:
    """@brief 首次成功后边界前拒绝、恰好二十四小时成功 /
    After the first grant, just before the boundary rejects and exact equality succeeds.

    @return None / None.
    """

    lottery = DailyLottery(EconomyAccountId(42))
    first = lottery.claim(claimed_at=FIRST_AT, proposed_prize=LotteryPrize(7))
    before_boundary = LotteryClaimInstant(
        FIRST_AT.value + timedelta(hours=24) - timedelta(microseconds=1)
    )
    rejected = lottery.claim(
        claimed_at=before_boundary,
        proposed_prize=LotteryPrize(19),
    )

    assert isinstance(first, LotteryGranted)
    assert isinstance(rejected, LotteryAlreadyClaimed)
    assert rejected.next_eligible_at == FIRST_AT.after_daily_cooldown()
    assert lottery.last_claimed_at == FIRST_AT

    exact_boundary = FIRST_AT.after_daily_cooldown()
    granted = lottery.claim(
        claimed_at=exact_boundary,
        proposed_prize=LotteryPrize(13),
    )

    assert isinstance(granted, LotteryGranted)
    assert granted.prize == LotteryPrize(13)
    assert lottery.last_claimed_at == exact_boundary


def test_backdated_attempt_is_rejected_without_mutating_restored_state() -> None:
    """@brief 回溯尝试沿用既有拒绝语义且不改变快照 /
    A backdated attempt retains established rejection semantics without changing the snapshot.

    @return None / None.
    """

    lottery = DailyLottery.restore(
        account_id=EconomyAccountId(42),
        last_claimed_at=FIRST_AT,
    )
    backdated = LotteryClaimInstant(FIRST_AT.value - timedelta(days=30))

    decision = lottery.claim(
        claimed_at=backdated,
        proposed_prize=LotteryPrize(20),
    )

    assert isinstance(decision, LotteryAlreadyClaimed)
    assert decision.next_eligible_at == FIRST_AT.after_daily_cooldown()
    assert lottery.last_claimed_at == FIRST_AT


def test_claim_instants_retain_naive_as_utc_and_normalize_aware_offsets() -> None:
    """@brief naive 时刻按 UTC，aware 时刻转换到 UTC /
    Naive instants mean UTC and aware offsets are converted to UTC.

    @return None / None.
    """

    naive = LotteryClaimInstant(datetime(2030, 1, 2, 3, 4))
    offset = LotteryClaimInstant(
        datetime(2030, 1, 2, 11, 4, tzinfo=timezone(timedelta(hours=8)))
    )

    assert naive == FIRST_AT
    assert offset == FIRST_AT
    assert naive.value.tzinfo is UTC


def test_lottery_value_types_reject_bool_and_out_of_range_prizes() -> None:
    """@brief 值类型拒绝 bool、零和超出既有上界的奖励 /
    Value types reject bool, zero, and prizes above the established upper bound.

    @return None / None.
    """

    with pytest.raises(TypeError, match="integer"):
        LotteryPrize(True)
    with pytest.raises(ValueError, match="between one and twenty"):
        LotteryPrize(0)
    with pytest.raises(ValueError, match="between one and twenty"):
        LotteryPrize(21)
    with pytest.raises(TypeError, match="datetime"):
        LotteryClaimInstant("2030-01-02")  # type: ignore[arg-type]


def test_aggregate_identity_is_stable_across_state_changes() -> None:
    """@brief 聚合相等性与哈希只取决于账户身份 /
    Aggregate equality and hashing depend only on account identity.

    @return None / None.
    """

    fresh = DailyLottery(EconomyAccountId(42))
    restored = DailyLottery.restore(
        account_id=EconomyAccountId(42),
        last_claimed_at=FIRST_AT,
    )

    assert fresh == restored
    assert hash(fresh) == hash(restored)
