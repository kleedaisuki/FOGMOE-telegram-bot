"""@brief 审核惰性时间窗口测试 / Tests for moderation's lazy time windows."""

from __future__ import annotations

from fogmoe_bot.application.moderation.rate_windows import (
    CooldownGate,
    FixedWindowCounter,
)


class ManualMonotonicClock:
    """@brief 可推进的单调测试时钟 / Advanceable monotonic test clock."""

    def __init__(self) -> None:
        """@brief 从零初始化 / Initialize at zero.

        @return None / None.
        """

        self.current = 0.0
        """@brief 当前单调秒数 / Current monotonic seconds."""

    def __call__(self) -> float:
        """@brief 返回当前时刻 / Return the current instant.

        @return 当前单调秒数 / Current monotonic seconds.
        """

        return self.current

    def advance(self, seconds: float) -> None:
        """@brief 推进测试时钟 / Advance the test clock.

        @param seconds 推进秒数 / Seconds to advance.
        @return None / None.
        """

        self.current += seconds


def test_warning_count_resets_lazily_at_the_one_hour_window_boundary() -> None:
    """@brief 警告计数在首次警告后一小时惰性重置 / Warning count resets lazily one hour after the first warning."""

    clock = ManualMonotonicClock()
    counter = FixedWindowCounter[str](
        window_seconds=3600.0,
        max_entries=16,
        clock=clock,
    )

    assert counter.increment("chat:user") == 1
    clock.advance(3599.999)
    assert counter.increment("chat:user") == 2
    clock.advance(0.001)
    assert counter.increment("chat:user") == 1


def test_warning_windows_prune_expired_entries_and_evict_to_a_hard_bound() -> None:
    """@brief 计数窗口同时惰性清理并保持硬容量 / Counter windows prune lazily and keep a hard capacity."""

    clock = ManualMonotonicClock()
    counter = FixedWindowCounter[str](
        window_seconds=10.0,
        max_entries=2,
        clock=clock,
    )

    assert counter.increment("oldest") == 1
    clock.advance(1.0)
    assert counter.increment("middle") == 1
    clock.advance(1.0)
    assert counter.increment("newest") == 1
    assert counter.entry_count == 2
    assert counter.increment("oldest") == 1
    assert counter.entry_count == 2

    clock.advance(11.0)
    assert counter.increment("fresh") == 1
    assert counter.entry_count == 1


def test_callback_cooldown_uses_monotonic_lazy_expiry_and_a_hard_bound() -> None:
    """@brief callback 冷却按单调时间惰性过期且有界 / Callback cooldown expires lazily by monotonic time and stays bounded."""

    clock = ManualMonotonicClock()
    gate = CooldownGate[str](
        cooldown_seconds=3.0,
        max_entries=2,
        clock=clock,
    )

    assert gate.try_acquire("first") is True
    assert gate.try_acquire("first") is False
    clock.advance(2.999)
    assert gate.try_acquire("first") is False
    clock.advance(0.001)
    assert gate.try_acquire("first") is True

    clock.advance(0.1)
    assert gate.try_acquire("second") is True
    clock.advance(0.1)
    assert gate.try_acquire("third") is True
    assert gate.entry_count == 2
