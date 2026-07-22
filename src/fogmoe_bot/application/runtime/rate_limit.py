"""@brief Runtime-owned 有界重放感知冷却门 / Runtime-owned bounded replay-aware cooldown gate."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _CooldownDecision:
    """@brief 一个 durable 请求的稳定冷却决定 / Stable cooldown decision for one durable request.

    @param admitted 首次求值是否获准 / Whether the first evaluation was admitted.
    @param decided_at monotonic 决定时刻 / Monotonic decision instant.
    """

    admitted: bool
    decided_at: float


class ReplayAwareCooldownGate[KeyT: Hashable]:
    """@brief 有界 P1 冷却状态，稳定重放每个近期请求 / Bounded P1 cooldown state replaying each recent decision.

    @note 该门记录 ``(key, request_id)`` 的首次决定，而不只记录每个 key 的最后请求。
        因此旧 Update 在较新 Update 之后重试时仍得到原决定；被拒绝 Update 的反馈写入失败后
        重试也不会悄悄变成获准。/ The gate records the first decision for each
        ``(key, request_id)``, not merely the last request per key. An older Update therefore
        retains its decision after a newer Update, and a rejected Update cannot silently become
        admitted when retrying after feedback persistence fails.
    @note 这是可丢失的 P1 policy 状态；容量淘汰只会放宽短期限流，业务事实仍必须由数据库
        幂等约束保护。/ This is discardable P1 policy state; capacity eviction can only relax
        short-term limiting, while database idempotency must still protect business facts.
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float,
        max_entries: int,
        retention_seconds: float = 3600.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """@brief 创建有界冷却门 / Create a bounded cooldown gate.

        @param cooldown_seconds 不同请求间最短间隔 / Minimum interval between distinct requests.
        @param max_entries 最大近期请求决定数 / Maximum number of recent request decisions.
        @param retention_seconds 请求决定保留时间 / Request-decision retention.
        @param monotonic 可替换 monotonic clock / Replaceable monotonic clock.
        @raise ValueError 配置非正或 retention 小于 cooldown / Non-positive configuration or retention below cooldown.
        """

        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least one")
        if retention_seconds < cooldown_seconds:
            raise ValueError("retention_seconds cannot be shorter than cooldown")
        self._cooldown_seconds = float(cooldown_seconds)
        self._max_entries = max_entries
        self._retention_seconds = float(retention_seconds)
        self._monotonic = monotonic
        self._decisions: OrderedDict[tuple[KeyT, int], _CooldownDecision] = (
            OrderedDict()
        )
        """@brief 按首次决定时刻排序的重放记录 / Replay records ordered by first-decision time."""
        self._latest_admission: dict[KeyT, float] = {}
        """@brief 每个 key 最近获准时刻 / Most recent admission instant for each key."""

    def try_acquire(self, key: KeyT, request_id: int) -> bool:
        """@brief 首次决定冷却结果，之后稳定重放 / Decide once, then replay the cooldown result.

        @param key 限流 identity / Rate-limit identity.
        @param request_id durable 请求 ID / Durable request identifier.
        @return 首次或重放决定为获准时返回 True / True when the first or replayed decision is admission.
        @raise ValueError request ID 不是非负整数 / The request identifier is not a non-negative integer.
        """

        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, int)
            or request_id < 0
        ):
            raise ValueError("request_id must be a non-negative integer")
        now = self._monotonic()
        self._purge(now)
        decision_key = (key, request_id)
        existing = self._decisions.get(decision_key)
        if existing is not None:
            return existing.admitted

        latest = self._latest_admission.get(key)
        admitted = latest is None or now - latest >= self._cooldown_seconds
        self._decisions[decision_key] = _CooldownDecision(admitted, now)
        if admitted:
            self._latest_admission[key] = now
        while len(self._decisions) > self._max_entries:
            removed_key, removed = self._decisions.popitem(last=False)
            self._forget_admission_if_owned(removed_key[0], removed)
        return admitted

    def _purge(self, now: float) -> None:
        """@brief 从时间序列前端删除过期决定 / Remove expired decisions from the time-ordered front.

        @param now 当前 monotonic 时刻 / Current monotonic instant.
        @return None / None.
        """

        threshold = now - self._retention_seconds
        while self._decisions:
            first_key = next(iter(self._decisions))
            first = self._decisions[first_key]
            if first.decided_at > threshold:
                break
            del self._decisions[first_key]
            self._forget_admission_if_owned(first_key[0], first)

    def _forget_admission_if_owned(
        self,
        key: KeyT,
        removed: _CooldownDecision,
    ) -> None:
        """@brief 淘汰其拥有的 latest admission，并从剩余记录重建 / Forget an evicted latest admission and rebuild it from retained records.

        @param key 被淘汰决定的限流 key / Rate-limit key of the evicted decision.
        @param removed 已淘汰决定 / Evicted decision.
        @return None / None.
        """

        if not removed.admitted:
            return
        if self._latest_admission.get(key) != removed.decided_at:
            return
        replacement = max(
            (
                decision.decided_at
                for (candidate, _), decision in self._decisions.items()
                if candidate == key and decision.admitted
            ),
            default=None,
        )
        if replacement is None:
            self._latest_admission.pop(key, None)
        else:
            self._latest_admission[key] = replacement


__all__ = ["ReplayAwareCooldownGate"]
