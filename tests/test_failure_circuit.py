"""@brief 通用失败断路器状态测试 / Generic failure-circuit state tests."""

from __future__ import annotations

import pytest

from fogmoe_bot.application.runtime import FailureCircuit, FailureCircuitPolicy


class _Clock:
    """@brief 可控单调时钟 / Controllable monotonic clock."""

    def __init__(self, now: float = 100.0) -> None:
        """@brief 设置初始时刻 / Set the initial instant.

        @param now 初始单调秒数 / Initial monotonic seconds.
        """

        self.now = now

    def __call__(self) -> float:
        """@brief 返回当前时刻 / Return the current instant.

        @return 单调秒数 / Monotonic seconds.
        """

        return self.now

    def advance(self, seconds: float) -> None:
        """@brief 推进时钟 / Advance the clock.

        @param seconds 推进秒数 / Seconds to advance.
        @return None / None.
        """

        self.now += seconds


def test_failure_circuit_uses_rolling_window_cooldown_and_success_reset() -> None:
    """@brief 断路器按 key 滚动计数、冷却并由成功复位 / Circuit rolls per key, cools down, and resets on success."""

    clock = _Clock()
    circuit = FailureCircuit[str](
        FailureCircuitPolicy(
            failure_threshold=3,
            failure_window_seconds=10.0,
            cooldown_seconds=30.0,
        ),
        monotonic=clock,
    )

    circuit.record_failure("embedding")
    clock.advance(11.0)
    circuit.record_failure("embedding")
    circuit.record_failure("embedding")
    assert circuit.is_open("embedding") is False

    circuit.record_success("embedding")
    circuit.record_failure("embedding")
    circuit.record_failure("embedding")
    circuit.record_failure("embedding")
    assert circuit.is_open("embedding") is True
    assert circuit.is_open("provider") is False

    clock.advance(30.0)
    assert circuit.is_open("embedding") is False
    circuit.record_failure("embedding")
    circuit.record_success("embedding")
    assert circuit.is_open("embedding") is False


def test_failure_circuit_policy_rejects_invalid_numbers() -> None:
    """@brief 策略拒绝非正与非有限数值 / Policy rejects non-positive and non-finite values."""

    with pytest.raises(ValueError, match="failure_threshold"):
        FailureCircuitPolicy(0, 1.0, 1.0)
    with pytest.raises(ValueError, match="failure_threshold"):
        FailureCircuitPolicy(1.5, 1.0, 1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="failure_window_seconds"):
        FailureCircuitPolicy(1, float("nan"), 1.0)
    with pytest.raises(ValueError, match="cooldown_seconds"):
        FailureCircuitPolicy(1, 1.0, float("inf"))
