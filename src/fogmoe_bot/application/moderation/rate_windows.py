"""@brief 单事件循环拥有的有界惰性时间窗口 / Bounded lazy time windows owned by one event loop."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

KeyT = TypeVar("KeyT", bound=Hashable)
"""@brief 时间窗口键类型 / Time-window key type."""

type MonotonicClock = Callable[[], float]
"""@brief 单调秒时钟 / Monotonic seconds clock."""


@dataclass(frozen=True, slots=True)
class _CounterWindow:
    """@brief 一个固定窗口内的计数与截止时间 / Count and deadline within one fixed window."""

    count: int
    """@brief 当前窗口计数 / Count in the current window."""

    expires_at: float
    """@brief 单调时钟截止时间 / Monotonic deadline."""


class FixedWindowCounter(Generic[KeyT]):
    """@brief 惰性过期且内存有界的固定窗口计数器 / Lazy-expiring, memory-bounded fixed-window counter.

    @param window_seconds 每个键从首次计数开始的窗口长度 / Per-key window length from its first count.
    @param max_entries 最大驻留键数 / Maximum resident keys.
    @param clock 单调时钟 / Monotonic clock.
    @note 方法无 await 且由同一事件循环线程拥有，因此整个读改写是一个协作式调度
    临界区，不需要线程互斥锁。/ Methods contain no await and are owned by one
    event-loop thread, so each read-modify-write is one cooperative-scheduling critical
    section and needs no thread mutex.
    """

    def __init__(
        self,
        *,
        window_seconds: float,
        max_entries: int,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        """@brief 配置窗口与容量 / Configure the window and capacity.

        @param window_seconds 窗口秒数 / Window length in seconds.
        @param max_entries 最大驻留键数 / Maximum resident keys.
        @param clock 单调时钟 / Monotonic clock.
        @return None / None.
        @raises ValueError 窗口或容量无效 / If the window or capacity is invalid.
        """

        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be finite and positive")
        if isinstance(max_entries, bool) or max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._window_seconds = window_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[KeyT, _CounterWindow] = {}

    @property
    def entry_count(self) -> int:
        """@brief 返回当前驻留键数 / Return the current resident-key count.

        @return 驻留键数 / Resident-key count.
        """

        return len(self._entries)

    def increment(self, key: KeyT) -> int:
        """@brief 增加键的当前窗口计数 / Increment a key's current-window count.

        @param key 计数键 / Counter key.
        @return 当前固定窗口内的新计数 / New count in the current fixed window.
        """

        now = self._clock()
        self._remove_expired(now)
        current = self._entries.get(key)
        if current is None:
            updated = _CounterWindow(1, now + self._window_seconds)
        else:
            updated = _CounterWindow(current.count + 1, current.expires_at)
        self._entries[key] = updated
        self._trim_to_bound()
        return updated.count

    def _remove_expired(self, now: float) -> None:
        """@brief 惰性删除全部已到期窗口 / Lazily remove all expired windows.

        @param now 当前单调时刻 / Current monotonic instant.
        @return None / None.
        """

        expired = [
            key for key, window in self._entries.items() if now >= window.expires_at
        ]
        for key in expired:
            self._entries.pop(key, None)

    def _trim_to_bound(self) -> None:
        """@brief 超限时删除最早到期窗口 / Evict earliest-expiring windows when over capacity.

        @return None / None.
        """

        while len(self._entries) > self._max_entries:
            oldest = min(
                self._entries,
                key=lambda key: self._entries[key].expires_at,
            )
            self._entries.pop(oldest)


class CooldownGate(Generic[KeyT]):
    """@brief 惰性过期且内存有界的冷却准入门 / Lazy-expiring, memory-bounded cooldown admission gate.

    @param cooldown_seconds 每个键的冷却长度 / Per-key cooldown length.
    @param max_entries 最大驻留键数 / Maximum resident keys.
    @param clock 单调时钟 / Monotonic clock.
    @note 与 FixedWindowCounter 相同，本对象由一个事件循环线程同步拥有。/
    Like FixedWindowCounter, this object is synchronously owned by one event-loop thread.
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float,
        max_entries: int,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        """@brief 配置冷却与容量 / Configure cooldown and capacity.

        @param cooldown_seconds 冷却秒数 / Cooldown length in seconds.
        @param max_entries 最大驻留键数 / Maximum resident keys.
        @param clock 单调时钟 / Monotonic clock.
        @return None / None.
        @raises ValueError 冷却或容量无效 / If cooldown or capacity is invalid.
        """

        if not math.isfinite(cooldown_seconds) or cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be finite and positive")
        if isinstance(max_entries, bool) or max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._cooldown_seconds = cooldown_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._accepted_at: dict[KeyT, float] = {}

    @property
    def entry_count(self) -> int:
        """@brief 返回当前驻留键数 / Return the current resident-key count.

        @return 驻留键数 / Resident-key count.
        """

        return len(self._accepted_at)

    def try_acquire(self, key: KeyT) -> bool:
        """@brief 尝试取得一次冷却准入 / Try to acquire one cooldown admission.

        @param key 冷却键 / Cooldown key.
        @return 首次或已过期为 True，仍在冷却为 False / True when new or expired; False while cooling down.
        """

        now = self._clock()
        self._remove_expired(now)
        if key in self._accepted_at:
            return False
        self._accepted_at[key] = now
        self._trim_to_bound()
        return True

    def _remove_expired(self, now: float) -> None:
        """@brief 惰性删除已结束冷却 / Lazily remove elapsed cooldowns.

        @param now 当前单调时刻 / Current monotonic instant.
        @return None / None.
        """

        expired = [
            key
            for key, accepted_at in self._accepted_at.items()
            if now - accepted_at >= self._cooldown_seconds
        ]
        for key in expired:
            self._accepted_at.pop(key, None)

    def _trim_to_bound(self) -> None:
        """@brief 超限时删除最早准入记录 / Evict oldest admissions when over capacity.

        @return None / None.
        """

        while len(self._accepted_at) > self._max_entries:
            oldest = min(self._accepted_at, key=self._accepted_at.__getitem__)
            self._accepted_at.pop(oldest)


class FixedWindowGate(Generic[KeyT]):
    """@brief 有界固定窗口准入器 / Bounded fixed-window admission gate.

    @param window_seconds 固定窗口长度 / Fixed-window length.
    @param max_admissions 每个窗口最大准入数 / Maximum admissions per window.
    @param max_entries 最大驻留键数 / Maximum resident keys.
    @param clock 单调时钟 / Monotonic clock.
    """

    def __init__(
        self,
        *,
        window_seconds: float,
        max_admissions: int,
        max_entries: int,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        """@brief 配置准入窗口 / Configure the admission window.

        @param window_seconds 窗口秒数 / Window length in seconds.
        @param max_admissions 每窗口准入上限 / Per-window admission limit.
        @param max_entries 最大驻留键数 / Maximum resident keys.
        @param clock 单调时钟 / Monotonic clock.
        @return None / None.
        @raises ValueError 任一上限无效 / For invalid limits.
        """

        if isinstance(max_admissions, bool) or max_admissions <= 0:
            raise ValueError("max_admissions must be positive")
        self._counter = FixedWindowCounter[KeyT](
            window_seconds=window_seconds,
            max_entries=max_entries,
            clock=clock,
        )
        self._max_admissions = max_admissions

    @property
    def entry_count(self) -> int:
        """@brief 返回驻留键数 / Return the resident-key count.

        @return 驻留键数 / Resident-key count.
        """

        return self._counter.entry_count

    def try_acquire(self, key: KeyT) -> bool:
        """@brief 尝试取得一个窗口名额 / Try to acquire one window slot.

        @param key 准入键 / Admission key.
        @return 未超过上限为 True / True while within the limit.
        """

        return self._counter.increment(key) <= self._max_admissions


__all__ = [
    "CooldownGate",
    "FixedWindowCounter",
    "FixedWindowGate",
    "MonotonicClock",
]
