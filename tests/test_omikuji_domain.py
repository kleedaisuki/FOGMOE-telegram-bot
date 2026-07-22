"""@brief 御神签确定性领域规则测试 / Tests for deterministic Omikuji domain rules."""

from __future__ import annotations

import random
from datetime import date

from fogmoe_bot.domain.games import FortuneLevel, daily_fortune


def test_daily_fortune_is_deterministic_without_mutating_global_random_state() -> None:
    """@brief 每日签稳定且不污染共享 PRNG / Daily fortune is stable and does not mutate the shared PRNG.

    @return None / None.
    """

    random.seed(12345)
    first = random.random()
    fortune = daily_fortune(42, date(2026, 7, 11))
    second = random.random()

    random.seed(12345)
    assert first == random.random()
    assert second == random.random()
    assert fortune is daily_fortune(42, date(2026, 7, 11))
    assert isinstance(fortune, FortuneLevel)
