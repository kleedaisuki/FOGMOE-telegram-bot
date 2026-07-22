"""@brief 运行时拥有的有界治理配置缓存 / Runtime-owned bounded moderation-configuration cache."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

from fogmoe_bot.domain.moderation.aggregate import GroupModeration
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    GroupModerationPolicy,
    ModerationRule,
)

from .ports import GroupModerationRepository

KeyT = TypeVar("KeyT", bound=Hashable)
"""@brief 缓存键类型 / Cache-key type."""

ValueT = TypeVar("ValueT")
"""@brief 缓存值类型 / Cache-value type."""

type MonotonicClock = Callable[[], float]
"""@brief 单调秒时钟 / Monotonic-seconds clock."""


@dataclass(frozen=True, slots=True)
class _TimedValue(Generic[ValueT]):
    """@brief 带截止时间的缓存值 / Cached value with a deadline.

    @param value 缓存值 / Cached value.
    @param expires_at 单调时钟截止时间 / Monotonic deadline.
    @param touched_at 最近写入时间 / Most recent write instant.
    """

    value: ValueT
    expires_at: float
    touched_at: float


class BoundedTtlCache(Generic[KeyT, ValueT]):
    """@brief 单事件循环拥有的有界 TTL 缓存 / Bounded TTL cache owned by one event loop.

    @param ttl_seconds 缓存寿命 / Cache lifetime.
    @param max_entries 最大驻留键数 / Maximum resident keys.
    @param clock 单调时钟 / Monotonic clock.
    @note 本对象不是 module singleton，也不创建 timer 或 thread lock。调用方运行时拥有它，
    过期与容量回收都在访问时同步发生。/ This is not a module singleton and creates no
    timer or thread lock. Its caller runtime owns it; expiry and capacity eviction happen
    synchronously on access.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        clock: MonotonicClock = time.monotonic,
    ) -> None:
        """@brief 配置缓存 / Configure the cache.

        @param ttl_seconds 缓存寿命 / Cache lifetime.
        @param max_entries 最大驻留键数 / Maximum resident keys.
        @param clock 单调时钟 / Monotonic clock.
        @return None / None.
        @raises ValueError 参数无效 / For invalid configuration.
        """

        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive")
        if isinstance(max_entries, bool) or max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[KeyT, _TimedValue[ValueT]] = {}

    @property
    def entry_count(self) -> int:
        """@brief 返回驻留条目数 / Return the resident-entry count.

        @return 条目数 / Entry count.
        """

        self._remove_expired(self._clock())
        return len(self._entries)

    def get(self, key: KeyT) -> ValueT | None:
        """@brief 读取新鲜值 / Read a fresh value.

        @param key 缓存键 / Cache key.
        @return 新鲜值或 None / Fresh value or None.
        """

        now = self._clock()
        self._remove_expired(now)
        entry = self._entries.get(key)
        return entry.value if entry is not None else None

    def put(self, key: KeyT, value: ValueT) -> None:
        """@brief 写入值并执行容量回收 / Put a value and enforce capacity.

        @param key 缓存键 / Cache key.
        @param value 缓存值 / Cache value.
        @return None / None.
        """

        now = self._clock()
        self._remove_expired(now)
        self._entries[key] = _TimedValue(
            value=value,
            expires_at=now + self._ttl_seconds,
            touched_at=now,
        )
        while len(self._entries) > self._max_entries:
            oldest = min(
                self._entries,
                key=lambda candidate: self._entries[candidate].touched_at,
            )
            self._entries.pop(oldest)

    def invalidate(self, key: KeyT) -> None:
        """@brief 删除一个键 / Invalidate one key.

        @param key 缓存键 / Cache key.
        @return None / None.
        """

        self._entries.pop(key, None)

    def _remove_expired(self, now: float) -> None:
        """@brief 惰性清理过期条目 / Lazily remove expired entries.

        @param now 当前单调时刻 / Current monotonic instant.
        @return None / None.
        """

        expired = tuple(
            key for key, entry in self._entries.items() if now >= entry.expires_at
        )
        for key in expired:
            self._entries.pop(key, None)


class GroupModerationConfiguration:
    """@brief 聚合读取与缓存 capability / Aggregate-reading and caching capability.

    @param repository 群组治理仓储 / Group-moderation repository.
    @param cache 运行时拥有的缓存 / Runtime-owned cache.
    """

    def __init__(
        self,
        repository: GroupModerationRepository,
        cache: BoundedTtlCache[ChatId, GroupModeration],
    ) -> None:
        """@brief 注入仓储和缓存 / Inject the repository and cache.

        @param repository 群组治理仓储 / Group-moderation repository.
        @param cache 运行时拥有的缓存 / Runtime-owned cache.
        @return None / None.
        """

        self._repository = repository
        self._cache = cache

    async def get_group(self, chat_id: ChatId) -> GroupModeration:
        """@brief 读取群组聚合 / Read a group aggregate.

        @param chat_id 群组 ID / Group identifier.
        @return 当前聚合 / Current aggregate.
        """

        cached = self._cache.get(chat_id)
        if cached is not None:
            return cached
        aggregate = await self._repository.load_group(chat_id)
        self._cache.put(chat_id, aggregate)
        return aggregate

    async def get_policy(self, chat_id: ChatId) -> GroupModerationPolicy:
        """@brief 读取审核策略 / Read a moderation policy.

        @param chat_id 群组 ID / Group identifier.
        @return 策略快照 / Policy snapshot.
        """

        return (await self.get_group(chat_id)).policy

    async def get_group_rules(self, chat_id: ChatId) -> tuple[ModerationRule, ...]:
        """@brief 读取群组垃圾规则 / Read group spam rules.

        @param chat_id 群组 ID / Group identifier.
        @return 规则元组 / Rule tuple.
        """

        return (await self.get_group(chat_id)).spam_rules

    def put(self, aggregate: GroupModeration) -> None:
        """@brief 缓存已提交聚合 / Cache a committed aggregate.

        @param aggregate 已提交聚合 / Committed aggregate.
        @return None / None.
        """

        self._cache.put(aggregate.chat_id, aggregate)

    def invalidate(self, chat_id: ChatId) -> None:
        """@brief 使一个群组缓存失效 / Invalidate one group cache entry.

        @param chat_id 群组 ID / Group identifier.
        @return None / None.
        """

        self._cache.invalidate(chat_id)


__all__ = [
    "BoundedTtlCache",
    "GroupModerationConfiguration",
    "MonotonicClock",
]
