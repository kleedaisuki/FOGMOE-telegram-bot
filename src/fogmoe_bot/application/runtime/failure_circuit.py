"""@brief 可复用的进程内失败断路器 / Reusable in-process failure circuit."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from math import isfinite
import time


@dataclass(frozen=True, slots=True)
class FailureCircuitPolicy:
    """@brief 不可变断路策略 / Immutable failure-circuit policy.

    @param failure_threshold 滚动窗口内触发熔断的失败数 / Failures in the rolling window that open the circuit.
    @param failure_window_seconds 失败滚动窗口秒数 / Failure rolling-window duration in seconds.
    @param cooldown_seconds 快速失败的冷却秒数 / Fast-fail cooldown duration in seconds.
    """

    failure_threshold: int
    failure_window_seconds: float
    cooldown_seconds: float

    def __post_init__(self) -> None:
        """@brief 校验策略边界 / Validate policy boundaries.

        @return None / None.
        @raise ValueError 阈值或时间不是有限正值 / Threshold or duration is not finite and positive.
        """

        if (
            isinstance(self.failure_threshold, bool)
            or not isinstance(self.failure_threshold, int)
            or self.failure_threshold < 1
        ):
            raise ValueError("failure_threshold must be a positive integer")
        for name, value in (
            ("failure_window_seconds", self.failure_window_seconds),
            ("cooldown_seconds", self.cooldown_seconds),
        ):
            normalized = float(value)
            if not isfinite(normalized) or normalized <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, normalized)


@dataclass(slots=True)
class _FailureState:
    """@brief 单个 key 的私有滚动失败状态 / Private rolling failure state for one key.

    @param failure_times 未过期失败的单调时刻 / Monotonic instants of unexpired failures.
    @param open_until 熔断截止单调时刻 / Monotonic instant until which the circuit is open.
    """

    failure_times: deque[float] = field(default_factory=deque)
    open_until: float | None = None


class FailureCircuit[K: Hashable]:
    """@brief 按强类型 key 隔离的失败断路器 / Failure circuit isolated by strongly typed keys.

    @note 状态是可丢失的进程内运行时策略；业务正确性不能依赖它。冷却期内重复
        ``record_failure`` 不延长冷却，避免持续流量让依赖永远没有恢复探测机会。/
        State is discardable in-process runtime policy and cannot carry business correctness.
        Repeated failures while open do not extend cooldown, preserving a recovery probe.
    """

    def __init__(
        self,
        policy: FailureCircuitPolicy,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """@brief 注入不可变策略与单调时钟 / Inject immutable policy and monotonic clock.

        @param policy 断路策略 / Circuit policy.
        @param monotonic 可替换单调时钟 / Replaceable monotonic clock.
        """

        self._policy = policy
        self._monotonic = monotonic
        self._states: dict[K, _FailureState] = {}

    @property
    def policy(self) -> FailureCircuitPolicy:
        """@brief 返回不可变策略 / Return the immutable policy.

        @return 断路策略 / Circuit policy.
        """

        return self._policy

    def is_open(self, key: K) -> bool:
        """@brief 判断 key 是否仍处于快速失败冷却 / Check whether a key remains in fast-fail cooldown.

        @param key 外部依赖的稳定 identity / Stable external-dependency identity.
        @return 冷却期内为 True / True during cooldown.
        """

        state = self._states.get(key)
        if state is None or state.open_until is None:
            return False
        if self._now() < state.open_until:
            return True
        self._states.pop(key, None)
        return False

    def record_success(self, key: K) -> None:
        """@brief 成功后关闭并清空 key 的失败历史 / Close and clear a key after success.

        @param key 外部依赖的稳定 identity / Stable external-dependency identity.
        @return None / None.
        """

        self._states.pop(key, None)

    def record_failure(self, key: K) -> None:
        """@brief 记录窗口内失败并在达到阈值时打开 / Record a windowed failure and open at threshold.

        @param key 外部依赖的稳定 identity / Stable external-dependency identity.
        @return None / None.
        """

        now = self._now()
        state = self._states.setdefault(key, _FailureState())
        if state.open_until is not None:
            if now < state.open_until:
                return
            state.failure_times.clear()
            state.open_until = None

        cutoff = now - self._policy.failure_window_seconds
        while state.failure_times and state.failure_times[0] < cutoff:
            state.failure_times.popleft()
        state.failure_times.append(now)
        if len(state.failure_times) < self._policy.failure_threshold:
            return
        state.failure_times.clear()
        state.open_until = now + self._policy.cooldown_seconds

    def _now(self) -> float:
        """@brief 读取有限单调时刻 / Read a finite monotonic instant.

        @return 单调秒数 / Monotonic seconds.
        @raise RuntimeError 时钟返回非有限值 / Clock returns a non-finite value.
        """

        value = float(self._monotonic())
        if not isfinite(value):
            raise RuntimeError(
                "FailureCircuit monotonic clock returned a non-finite value"
            )
        return value


__all__ = ["FailureCircuit", "FailureCircuitPolicy"]
