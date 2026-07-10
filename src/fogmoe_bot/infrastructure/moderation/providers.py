"""@brief 数据库审核配置的类型化缓存 / Typed cache for database moderation configuration."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from fogmoe_bot.domain.moderation import (
    ChatId,
    GroupModerationPolicy,
    ModerationRule,
)
from fogmoe_bot.infrastructure.database.repositories import moderation_repository


@dataclass(frozen=True, slots=True)
class _TimedPolicy:
    """@brief 带单调时间戳的策略缓存项 / Policy cache entry with monotonic timestamp."""

    value: GroupModerationPolicy
    loaded_at: float


@dataclass(frozen=True, slots=True)
class _TimedRules:
    """@brief 带单调时间戳的规则缓存项 / Rule cache entry with monotonic timestamp."""

    value: tuple[ModerationRule, ...]
    loaded_at: float


class CachedModerationConfigurationProvider:
    """@brief 从数据库读取并缓存群组审核配置 / Load and cache group moderation configuration.

    @param ttl_seconds 缓存有效秒数 / Cache lifetime in seconds.
    """

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._policies: dict[ChatId, _TimedPolicy] = {}
        self._rules: dict[ChatId, _TimedRules] = {}
        self._locks: dict[ChatId, asyncio.Lock] = {}

    async def get_policy(self, chat_id: ChatId) -> GroupModerationPolicy:
        """@brief 读取群组策略 / Read a group policy.

        @param chat_id Telegram 群组 ID / Telegram chat ID.
        @return 群组策略；无数据库记录时返回禁用策略 / Policy, disabled when absent.
        """

        cached = self._policies.get(chat_id)
        if cached and self._is_fresh(cached.loaded_at):
            return cached.value
        async with self._lock_for(chat_id):
            cached = self._policies.get(chat_id)
            if cached and self._is_fresh(cached.loaded_at):
                return cached.value
            policy = await moderation_repository.fetch_spam_control(int(chat_id))
            value = policy or GroupModerationPolicy(chat_id=chat_id)
            self.put_policy(value)
            return value

    async def get_group_rules(self, chat_id: ChatId) -> tuple[ModerationRule, ...]:
        """@brief 读取群组自定义规则 / Read group-specific moderation rules.

        @param chat_id Telegram 群组 ID / Telegram chat ID.
        @return 群组规则 / Group-specific rules.
        """

        cached = self._rules.get(chat_id)
        if cached and self._is_fresh(cached.loaded_at):
            return cached.value
        return await self.refresh_group_rules(chat_id)

    def put_policy(self, policy: GroupModerationPolicy) -> None:
        """@brief 将已确认策略写入缓存 / Put a confirmed policy into cache.

        @param policy 群组策略 / Group policy.
        @return None / None.
        """

        self._policies[policy.chat_id] = _TimedPolicy(policy, time.monotonic())

    async def refresh_group_rules(self, chat_id: ChatId) -> tuple[ModerationRule, ...]:
        """@brief 强制从数据库刷新群组规则 / Refresh group rules from the database.

        @param chat_id Telegram 群组 ID / Telegram chat ID.
        @return 刷新后的群组规则 / Refreshed group rules.
        """

        async with self._lock_for(chat_id):
            rules = await moderation_repository.fetch_group_spam_keywords(int(chat_id))
            self._rules[chat_id] = _TimedRules(rules, time.monotonic())
            return rules

    def invalidate_policy(self, chat_id: ChatId) -> None:
        """@brief 使策略缓存失效 / Invalidate a policy cache entry.

        @param chat_id Telegram 群组 ID / Telegram chat ID.
        @return None / None.
        """

        self._policies.pop(chat_id, None)

    def invalidate_rules(self, chat_id: ChatId) -> None:
        """@brief 使规则缓存失效 / Invalidate a rule cache entry.

        @param chat_id Telegram 群组 ID / Telegram chat ID.
        @return None / None.
        """

        self._rules.pop(chat_id, None)

    def _is_fresh(self, loaded_at: float) -> bool:
        """@brief 判断缓存项是否有效 / Check whether a cache entry is fresh.

        @param loaded_at 单调时钟加载时刻 / Monotonic load timestamp.
        @return 有效返回 True / True when fresh.
        """

        return time.monotonic() - loaded_at < self._ttl_seconds

    def _lock_for(self, chat_id: ChatId) -> asyncio.Lock:
        """@brief 获取群组级加载锁 / Get a per-chat loading lock.

        @param chat_id Telegram 群组 ID / Telegram chat ID.
        @return 异步锁 / Async lock.
        """

        return self._locks.setdefault(chat_id, asyncio.Lock())
