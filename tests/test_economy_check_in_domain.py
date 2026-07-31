"""@brief Economy 每日签到领域行为测试 / Domain-behavior tests for Economy daily check-in."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from fogmoe_bot.domain.economy.check_in import (
    CheckInAlreadyClaimed,
    CheckInGranted,
    CheckInReward,
    CheckInStreak,
    CheckInStreakLength,
)
from fogmoe_bot.domain.economy.identity import EconomyAccountId

START_DAY = date(2030, 1, 1)
"""@brief 固定签到起始业务日 / Fixed starting business day for check-in tests."""


@pytest.mark.parametrize("invalid", (True, 1.5, "42", None))
def test_economy_account_identity_rejects_non_integer_values(invalid: object) -> None:
    """@brief 账户身份拒绝 Python 隐式数值兼容 / Account identity rejects Python's implicit numeric compatibility.

    @param invalid 非严格整数值 / Non-strict-integer value.
    @return None / None.
    """

    with pytest.raises(TypeError, match="integer"):
        EconomyAccountId(invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", (0, -1))
def test_economy_account_identity_rejects_non_positive_values(invalid: int) -> None:
    """@brief 账户身份只能为正 / Account identity must be positive.

    @param invalid 非正整数 / Non-positive integer.
    @return None / None.
    """

    with pytest.raises(ValueError, match="positive"):
        EconomyAccountId(invalid)


@pytest.mark.parametrize(
    ("days", "coins"),
    ((1, 1), (5, 1), (6, 2), (10, 2), (11, 3), (30, 6), (31, 7), (100, 7)),
)
def test_check_in_reward_preserves_the_established_five_day_tiers(
    days: int,
    coins: int,
) -> None:
    """@brief 阶梯奖励每五天增长并封顶七枚 / Reward grows every five days and caps at seven.

    @param days 连续签到天数 / Consecutive check-in days.
    @param coins 期望金币奖励 / Expected token reward.
    @return None / None.
    """

    assert CheckInReward.for_streak(CheckInStreakLength(days)).coins == coins


def test_check_in_lifecycle_extends_once_and_rejects_duplicate_claims() -> None:
    """@brief 聚合递增连续天数且同日重复不改变状态 / Aggregate extends streaks and leaves duplicates unchanged.

    @return None / None.
    """

    streak = CheckInStreak(EconomyAccountId(42))
    first = streak.claim(START_DAY)
    assert isinstance(first, CheckInGranted)
    assert first.streak.days == 1
    assert first.reward.coins == 1

    sixth: CheckInGranted | None = None
    for offset in range(1, 6):
        decision = streak.claim(START_DAY + timedelta(days=offset))
        assert isinstance(decision, CheckInGranted)
        sixth = decision
    assert sixth is not None
    assert sixth.streak.days == 6
    assert sixth.reward.coins == 2

    duplicate = streak.claim(START_DAY + timedelta(days=5))
    assert isinstance(duplicate, CheckInAlreadyClaimed)
    assert duplicate.streak.days == 6
    assert streak.length == CheckInStreakLength(6)


def test_check_in_lifecycle_restores_and_resets_after_a_gap() -> None:
    """@brief 持久化状态经领域校验恢复，断签后从一天重启 / Persisted state is validated and restarts after a gap.

    @return None / None.
    """

    streak = CheckInStreak.restore(
        account_id=EconomyAccountId(42),
        last_claimed_on=START_DAY,
        consecutive_days=30,
    )
    decision = streak.claim(START_DAY + timedelta(days=2))
    assert isinstance(decision, CheckInGranted)
    assert decision.streak.days == 1
    assert decision.reward.coins == 1
    assert streak.last_claimed_on == START_DAY + timedelta(days=2)

    with pytest.raises(ValueError, match="positive"):
        CheckInStreak.restore(
            account_id=EconomyAccountId(42),
            last_claimed_on=START_DAY,
            consecutive_days=0,
        )


def test_check_in_entity_equality_uses_stable_account_identity() -> None:
    """@brief 生命周期变化不改变实体身份与哈希 / Lifecycle changes do not alter entity identity or hash.

    @return None / None.
    """

    original = CheckInStreak(EconomyAccountId(42))
    restored = CheckInStreak.restore(
        account_id=EconomyAccountId(42),
        last_claimed_on=START_DAY,
        consecutive_days=7,
    )
    different = CheckInStreak(EconomyAccountId(43))
    identity_hash = hash(original)

    original.claim(START_DAY)

    assert original == restored
    assert original != different
    assert hash(original) == identity_hash
