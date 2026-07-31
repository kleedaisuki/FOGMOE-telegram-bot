"""@brief 每日抽奖随机与 PostgreSQL 映射器测试 / Tests for daily-lottery randomness and PostgreSQL mappers."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from fogmoe_bot.application.economy.lottery import (
    LotteryAlreadyClaimedResult,
    LotteryGrantedResult,
)
from fogmoe_bot.domain.economy.lottery import (
    PRIZE_BANDS,
    PRIZE_BAND_WEIGHTS,
    LotteryClaimInstant,
    LotteryPrize,
    LotteryPrizeBand,
    draw_lottery_prize,
)
from fogmoe_bot.infrastructure.database.economy.lottery import (
    _result_from_mapping,
    _result_mapping,
)
from fogmoe_bot.infrastructure.economy import randomness as randomness_adapter

NEXT_AT = LotteryClaimInstant(datetime(2030, 1, 3, 3, 4, tzinfo=UTC))
"""@brief 固定下次资格时刻 / Fixed next-eligibility instant."""


def test_receipt_mapping_retains_existing_json_shape_and_replay_marker() -> None:
    """@brief 新类型仍使用既有 code、prize 与 next_eligible_at JSON /
    New types retain the established code, prize, and next_eligible_at JSON shape.

    @return None / None.
    """

    granted = LotteryGrantedResult(
        prize=LotteryPrize(7),
        next_eligible_at=NEXT_AT,
    )
    granted_payload = _result_mapping(granted)
    repeated_payload = _result_mapping(
        LotteryAlreadyClaimedResult(next_eligible_at=NEXT_AT)
    )

    assert granted_payload == {
        "code": "success",
        "prize": 7,
        "next_eligible_at": "2030-01-03T03:04:00+00:00",
    }
    assert repeated_payload == {
        "code": "already_claimed",
        "prize": 0,
        "next_eligible_at": "2030-01-03T03:04:00+00:00",
    }
    replayed = _result_from_mapping(
        json.loads(json.dumps(granted_payload)),
        replayed=True,
    )
    assert isinstance(replayed, LotteryGrantedResult)
    assert replayed.replayed
    assert replayed.prize == LotteryPrize(7)
    assert replayed.next_eligible_at == NEXT_AT


def test_receipt_mapping_rejects_impossible_success_shape() -> None:
    """@brief receipt 解码拒绝缺少资格时刻或非法奖励的成功结果 /
    Receipt decoding rejects a success without eligibility or with an impossible prize.

    @return None / None.
    """

    with pytest.raises(ValueError, match="next eligibility"):
        _result_from_mapping(
            {"code": "success", "prize": 7, "next_eligible_at": None},
            replayed=True,
        )
    with pytest.raises(ValueError, match="between one and twenty"):
        _result_from_mapping(
            {
                "code": "success",
                "prize": 21,
                "next_eligible_at": NEXT_AT.value.isoformat(),
            },
            replayed=True,
        )


def test_standard_library_adapter_preserves_choices_then_randint_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 基础设施适配器保持既有 choices 后 randint 调用 /
    The infrastructure adapter retains the established choices-then-randint calls.

    @param monkeypatch 临时替换标准库随机函数 / Temporarily replace standard-library random functions.
    @return None / None.
    """

    calls: list[tuple[object, ...]] = []
    """@brief 观察到的标准库调用 / Observed standard-library calls."""

    def choices(
        population: tuple[LotteryPrizeBand, ...],
        weights: tuple[float, ...],
        *,
        k: int,
    ) -> list[LotteryPrizeBand]:
        """@brief 记录 weighted choice 并选择 large / Record weighted choice and select large.

        @param population 候选档位 / Candidate bands.
        @param weights 候选权重 / Candidate weights.
        @param k 抽取数量 / Number of draws.
        @return 单一 large 档位 / One large band.
        """

        calls.append(("choices", population, weights, k))
        return [LotteryPrizeBand.LARGE]

    def randint(lower: int, upper: int) -> int:
        """@brief 记录整数闭区间并返回十三 / Record the integer interval and return thirteen.

        @param lower 闭区间下界 / Inclusive lower bound.
        @param upper 闭区间上界 / Inclusive upper bound.
        @return 十三 / Thirteen.
        """

        calls.append(("randint", lower, upper))
        return 13

    monkeypatch.setattr(randomness_adapter.random, "choices", choices)
    monkeypatch.setattr(randomness_adapter.random, "randint", randint)

    prize = draw_lottery_prize(randomness_adapter.StandardLibraryLotteryRandomness())

    assert prize == LotteryPrize(13)
    assert calls == [
        ("choices", PRIZE_BANDS, PRIZE_BAND_WEIGHTS, 1),
        ("randint", 11, 20),
    ]
