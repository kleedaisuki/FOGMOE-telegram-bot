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

    first = circuit.try_acquire("embedding")
    assert first is not None
    circuit.record_failure(first)
    clock.advance(11.0)
    second = circuit.try_acquire("embedding")
    third = circuit.try_acquire("embedding")
    assert second is not None and third is not None
    circuit.record_failure(second)
    circuit.record_failure(third)
    success = circuit.try_acquire("embedding")
    assert success is not None
    circuit.record_success(success)
    failures = tuple(circuit.try_acquire("embedding") for _ in range(3))
    assert all(permit is not None for permit in failures)
    for permit in failures:
        assert permit is not None
        circuit.record_failure(permit)
    assert circuit.try_acquire("embedding") is None
    assert circuit.try_acquire("provider") is not None

    clock.advance(30.0)
    probe = circuit.try_acquire("embedding")
    assert probe is not None and probe.half_open
    assert circuit.try_acquire("embedding") is None
    circuit.record_success(probe)
    assert circuit.try_acquire("embedding") is not None


def test_failure_circuit_limits_half_open_to_one_probe_and_rejects_stale_outcomes() -> (
    None
):
    """@brief Half-Open 仅放行一个探针且迟到结果无法覆盖新代 / Half-Open admits one probe and rejects stale outcomes."""

    clock = _Clock()
    circuit = FailureCircuit[str](
        FailureCircuitPolicy(1, 10.0, 30.0),
        monotonic=clock,
    )
    original = circuit.try_acquire("provider")
    concurrent = circuit.try_acquire("provider")
    assert original is not None and concurrent is not None
    circuit.record_failure(original)
    circuit.record_success(concurrent)
    assert circuit.try_acquire("provider") is None

    clock.advance(30.0)
    abandoned = circuit.try_acquire("provider")
    assert abandoned is not None and abandoned.half_open
    assert circuit.try_acquire("provider") is None
    circuit.abandon(abandoned)
    failed_probe = circuit.try_acquire("provider")
    assert failed_probe is not None and failed_probe.half_open
    circuit.record_failure(failed_probe)

    clock.advance(30.0)
    current_probe = circuit.try_acquire("provider")
    assert current_probe is not None and current_probe.half_open
    circuit.record_success(failed_probe)
    assert circuit.try_acquire("provider") is None
    circuit.record_success(current_probe)
    assert circuit.try_acquire("provider") is not None


def test_failure_circuit_consumes_each_permit_at_most_once() -> None:
    """@brief 重复上报同一许可不会重复计数 / Reporting one permit twice does not count it twice."""

    circuit = FailureCircuit[str](FailureCircuitPolicy(2, 10.0, 30.0))
    permit = circuit.try_acquire("provider")
    assert permit is not None
    circuit.record_failure(permit)
    circuit.record_failure(permit)

    still_closed = circuit.try_acquire("provider")
    assert still_closed is not None and not still_closed.half_open
    circuit.record_success(still_closed)


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
