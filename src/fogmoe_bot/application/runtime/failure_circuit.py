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
    """@brief 单个 key 的私有断路状态 / Private circuit state for one key.

    @param failure_times 未过期失败的单调时刻 / Monotonic instants of unexpired failures.
    @param open_until 熔断截止单调时刻 / Monotonic instant until which the circuit is open.
    @param generation 拒绝迟到结果的代际 / Generation rejecting stale outcomes.
    @param probe_attempt 半开状态唯一探针的序号 / Attempt number of the sole half-open probe.
    @param active_attempts 本代尚未终结的许可序号 / Unfinished permit ordinals in this generation.
    """

    failure_times: deque[float] = field(default_factory=deque)
    open_until: float | None = None
    generation: int = 0
    probe_attempt: int | None = None
    active_attempts: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class CircuitPermit[K: Hashable]:
    """@brief 一次外部依赖调用的强类型许可 / Strongly typed permit for one dependency call.

    @param key 外部依赖 identity / External-dependency identity.
    @param generation 取得许可时的断路代际 / Circuit generation at acquisition.
    @param attempt 进程内唯一调用序号 / Process-local unique call ordinal.
    @param half_open 是否为半开恢复探针 / Whether this is the half-open recovery probe.
    @note 调用方必须恰好调用 ``record_success``、``record_failure`` 或
        ``abandon`` 之一。/ The caller must finish the permit exactly once with
        ``record_success``, ``record_failure``, or ``abandon``.
    """

    key: K
    generation: int
    attempt: int
    half_open: bool


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
        self._next_attempt = 0

    @property
    def policy(self) -> FailureCircuitPolicy:
        """@brief 返回不可变策略 / Return the immutable policy.

        @return 断路策略 / Circuit policy.
        """

        return self._policy

    def try_acquire(self, key: K) -> CircuitPermit[K] | None:
        """@brief 获取调用许可或快速失败 / Acquire a call permit or fail fast.

        @param key 外部依赖的稳定 identity / Stable external-dependency identity.
        @return Closed 时的普通许可、Half-Open 时的唯一探针，或 Open 时 None /
            A normal Closed permit, the sole Half-Open probe, or None while Open.
        @note 本方法无 await，半开探针的领取在单 asyncio 进程内为原子操作。/
            This method has no await, so half-open probe acquisition is atomic within
            one asyncio process.
        """

        state = self._states.setdefault(key, _FailureState())
        self._next_attempt += 1
        attempt = self._next_attempt
        if state.open_until is None:
            state.active_attempts.add(attempt)
            return CircuitPermit(key, state.generation, attempt, False)
        if self._now() < state.open_until or state.probe_attempt is not None:
            return None
        state.probe_attempt = attempt
        state.active_attempts.add(attempt)
        return CircuitPermit(key, state.generation, attempt, True)

    def record_success(self, permit: CircuitPermit[K]) -> None:
        """@brief 成功后关闭并清空该代失败历史 / Close and clear this generation after success.

        @param permit ``try_acquire`` 返回的许可 / Permit returned by ``try_acquire``.
        @return None / None.
        @note 旧代成功不能关闭新一轮熔断 / A stale success cannot close a newer open generation.
        """

        state = self._take_state(permit)
        if state is None:
            return
        if state.open_until is not None and (
            not permit.half_open or state.probe_attempt != permit.attempt
        ):
            return
        state.failure_times.clear()
        state.open_until = None
        state.probe_attempt = None
        state.generation += 1
        state.active_attempts.clear()

    def record_failure(self, permit: CircuitPermit[K]) -> None:
        """@brief 记录窗口内失败并在达到阈值时打开 / Record a windowed failure and open at threshold.

        @param permit ``try_acquire`` 返回的许可 / Permit returned by ``try_acquire``.
        @return None / None.
        @note Half-Open 探针失败立即重新 Open；旧代失败被忽略。/
            A failed Half-Open probe immediately reopens the circuit; stale failures are ignored.
        """

        now = self._now()
        state = self._take_state(permit)
        if state is None:
            return
        if state.open_until is not None:
            if not permit.half_open or state.probe_attempt != permit.attempt:
                return
            self._open(state, now=now)
            return

        cutoff = now - self._policy.failure_window_seconds
        while state.failure_times and state.failure_times[0] < cutoff:
            state.failure_times.popleft()
        state.failure_times.append(now)
        if len(state.failure_times) < self._policy.failure_threshold:
            return
        self._open(state, now=now)

    def abandon(self, permit: CircuitPermit[K]) -> None:
        """@brief 释放未归类成功/失败的半开探针 / Release an unclassified half-open probe.

        @param permit ``try_acquire`` 返回的许可 / Permit returned by ``try_acquire``.
        @return None / None.
        @note 取消、安全拦截等不代表依赖健康的结果必须走此路径。/
            Cancellation, safety blocks, and other outcomes unrelated to dependency health
            must use this path.
        """

        state = self._take_state(permit)
        if (
            state is not None
            and permit.half_open
            and state.probe_attempt == permit.attempt
        ):
            state.probe_attempt = None

    def _take_state(self, permit: CircuitPermit[K]) -> _FailureState | None:
        """@brief 原子消费许可并返回匹配状态 / Atomically consume a permit and return matching state.

        @param permit 待验证许可 / Permit to validate.
        @return 匹配状态，迟到或重复终结则为 None /
            Matching state, or None for a stale or already-finished permit.
        """

        state = self._states.get(permit.key)
        if (
            state is None
            or state.generation != permit.generation
            or permit.attempt not in state.active_attempts
        ):
            return None
        state.active_attempts.remove(permit.attempt)
        return state

    def _open(self, state: _FailureState, *, now: float) -> None:
        """@brief 进入新一代 Open 冷却 / Enter a new Open cooldown generation.

        @param state 待更新 key 状态 / Key state to update.
        @param now 已验证单调时刻 / Validated monotonic instant.
        @return None / None.
        """

        state.failure_times.clear()
        state.open_until = now + self._policy.cooldown_seconds
        state.probe_attempt = None
        state.generation += 1
        state.active_attempts.clear()

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


__all__ = ["CircuitPermit", "FailureCircuit", "FailureCircuitPolicy"]
