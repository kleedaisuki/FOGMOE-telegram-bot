"""@brief 可停止的自适应空闲轮询 / Stoppable adaptive idle polling."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from math import isfinite

from fogmoe_bot.application.runtime.clock import Jitter

type MonotonicClock = Callable[[], float]
"""@brief 返回单调秒数的时钟端口 / Clock port returning monotonic seconds."""


@dataclass(frozen=True, slots=True)
class AdaptivePollingPolicy:
    """@brief 有界指数空闲轮询策略 / Bounded exponential idle-polling policy.

    @param base_interval_seconds 发现工作后及首次空轮的间隔 / Interval after work and for the first idle poll.
    @param max_interval_seconds 连续空轮或轮询异常的间隔上限 / Interval cap for consecutive idle or failed polls.
    @note 该策略只控制可丢失的进程内轮询节奏，不参与 durable retry、lease 或业务语义。/
        This policy controls only discardable in-process polling cadence and does not carry
        durable retry, lease, or business semantics.
    """

    base_interval_seconds: float
    max_interval_seconds: float
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        """@brief 规范并校验有限正区间 / Normalize and validate finite positive intervals.

        @return None / None.
        @raise ValueError 间隔无效或上限小于基准时抛出 / Raised for invalid intervals or a cap below the base.
        """

        for name in ("base_interval_seconds", "max_interval_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool):
                raise ValueError(f"{name} must be finite and positive")
            normalized = float(value)
            if not isfinite(normalized) or normalized <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, normalized)
        if self.max_interval_seconds < self.base_interval_seconds:
            raise ValueError("max_interval_seconds must be >= base_interval_seconds")
        if (
            isinstance(self.jitter_ratio, bool)
            or not isfinite(float(self.jitter_ratio))
            or not 0.0 <= self.jitter_ratio < 1.0
        ):
            raise ValueError("jitter_ratio must be finite and in [0, 1)")
        object.__setattr__(self, "jitter_ratio", float(self.jitter_ratio))

    def start(self, *, jitter: Jitter = random.uniform) -> AdaptivePolling:
        """@brief 为一个独立轮询循环创建状态 / Create state for one independent polling loop.

        @param jitter 后续空轮的轻量随机源 / Lightweight random source for subsequent idle polls.
        @return 从 base 开始的可变轮询状态 / Mutable polling state starting at the base interval.
        """

        return AdaptivePolling(self, jitter=jitter)


@dataclass(slots=True)
class AdaptivePolling:
    """@brief 单个轮询循环的可丢失退避状态 / Discardable backoff state for one polling loop.

    @param policy 不可变轮询策略 / Immutable polling policy.
    """

    policy: AdaptivePollingPolicy
    jitter: Jitter = field(repr=False)
    _next_interval_seconds: float = field(init=False, repr=False)
    _idle_wait_count: int = field(init=False, default=0, repr=False)

    def __post_init__(self) -> None:
        """@brief 从基准间隔初始化 / Initialize from the base interval.

        @return None / None.
        """

        self._next_interval_seconds = self.policy.base_interval_seconds
        self._idle_wait_count = 0

    @property
    def next_interval_seconds(self) -> float:
        """@brief 返回下一次空闲等待长度 / Return the next idle-wait duration.

        @return 有界等待秒数 / Bounded wait in seconds.
        """

        return self._next_interval_seconds

    def reset(self) -> None:
        """@brief 发现工作后立即恢复基准节奏 / Immediately restore the base cadence after finding work.

        @return None / None.
        """

        self._next_interval_seconds = self.policy.base_interval_seconds
        self._idle_wait_count = 0

    async def wait(self, stop_event: asyncio.Event) -> None:
        """@brief 空轮或轮询异常后等待，并为下一轮指数退避 / Wait after idle or failure and back off the next poll.

        @param stop_event 顶层结构化停止信号 / Top-level structured stop signal.
        @return None；stop 已置位或等待中置位时立即返回 / None; returns immediately when stop is or becomes set.
        @note 取消原样传播；间隔按 ``base, 2*base, ... max`` 截断。/
            Cancellation propagates unchanged; intervals follow ``base, 2*base, ... max``.
        """

        nominal_delay = self._next_interval_seconds
        delay = nominal_delay
        if self._idle_wait_count > 0 and self.policy.jitter_ratio > 0.0:
            lower = nominal_delay * (1.0 - self.policy.jitter_ratio)
            delay = float(self.jitter(lower, nominal_delay))
            if not isfinite(delay) or not lower <= delay <= nominal_delay:
                raise ValueError(
                    "jitter returned a value outside its requested interval"
                )
        self._next_interval_seconds = min(
            self.policy.max_interval_seconds,
            nominal_delay * 2.0,
        )
        self._idle_wait_count += 1
        try:
            async with asyncio.timeout(delay):
                await stop_event.wait()
        except TimeoutError:
            return


@dataclass(slots=True)
class LeaseRecoveryCadence:
    """@brief 与 lease 生命周期对齐的低频恢复节奏 / Low-frequency recovery cadence aligned with lease lifetime.

    @param interval_seconds 两次恢复尝试的最短间隔 / Minimum interval between recovery attempts.
    @param monotonic 可替换单调时钟 / Replaceable monotonic clock.
    @note ``take_due`` 在返回 True 时先推进 deadline，因此恢复查询自身失败不会形成紧密重试。/
        ``take_due`` advances its deadline before returning True, so a failed recovery query
        cannot create a tight retry loop.
    """

    interval_seconds: float
    monotonic: MonotonicClock = field(default=time.monotonic, repr=False)
    _next_due: float | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        """@brief 校验有限正节奏 / Validate a finite positive cadence.

        @return None / None.
        @raise ValueError 间隔非法时抛出 / Raised when the interval is invalid.
        """

        if isinstance(self.interval_seconds, bool):
            raise ValueError("interval_seconds must be finite and positive")
        normalized = float(self.interval_seconds)
        if not isfinite(normalized) or normalized <= 0.0:
            raise ValueError("interval_seconds must be finite and positive")
        self.interval_seconds = normalized

    @classmethod
    def for_lease(
        cls,
        lease_for: timedelta,
        *,
        max_interval_seconds: float = 5.0,
        monotonic: MonotonicClock = time.monotonic,
    ) -> LeaseRecoveryCadence:
        """@brief 从 lease 推导不晚于半程的恢复周期 / Derive a recovery period no later than half the lease.

        @param lease_for durable claim lease / Durable claim lease.
        @param max_interval_seconds 运维查询频率上限 / Operational query-frequency cap.
        @param monotonic 可替换单调时钟 / Replaceable monotonic clock.
        @return 首次调用立即到期的恢复节奏 / Recovery cadence due immediately on first use.
        @raise ValueError lease 或上限不是有限正值时抛出 / Raised when the lease or cap is not finite and positive.
        """

        lease_seconds = lease_for.total_seconds()
        cap = float(max_interval_seconds)
        if (
            not isfinite(lease_seconds)
            or lease_seconds <= 0.0
            or isinstance(max_interval_seconds, bool)
            or not isfinite(cap)
            or cap <= 0.0
        ):
            raise ValueError("lease_for and max_interval_seconds must be positive")
        return cls(min(lease_seconds / 2.0, cap), monotonic=monotonic)

    def take_due(self) -> bool:
        """@brief 原子消费当前到期机会 / Consume the current due opportunity atomically.

        @return 首次调用或 deadline 已到时为 True / True on first use or once the deadline is reached.
        @raise RuntimeError 单调时钟返回非有限值时抛出 / Raised when the monotonic clock returns a non-finite value.
        """

        now = float(self.monotonic())
        if not isfinite(now):
            raise RuntimeError(
                "Lease recovery monotonic clock returned a non-finite value"
            )
        if self._next_due is not None and now < self._next_due:
            return False
        self._next_due = now + self.interval_seconds
        return True


__all__ = [
    "AdaptivePolling",
    "AdaptivePollingPolicy",
    "LeaseRecoveryCadence",
]
