"""@brief 有界会话上下文缓存 / Bounded conversation-context cache."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from fogmoe_bot.domain.context import ContextState
from fogmoe_bot.infrastructure import config


@dataclass(slots=True)
class _CacheEntry:
    """@brief 单个缓存槽位 / One cached conversation slot.

    @param context 已提交的会话上下文 / Committed conversation context.
    @param expires_at 单调时钟过期时刻 / Monotonic-clock expiry time.
    """

    context: ContextState
    expires_at: float


class ConversationContextCache:
    """@brief 进程内会话工作集缓存 / Process-local conversation working-set cache.

    缓存只保存和数据库最后一次成功写入一致的 ``ContextState``。调用方必须先取得对应
    会话锁，再读取或更新返回的对象；本类的锁只保护缓存索引，不覆盖整个 Agent 回合。
    / The cache stores only ``ContextState`` values consistent with the last successful database write.
    Callers must hold the conversation lock before mutating a returned value; this lock protects the index only.
    """

    def __init__(self, *, capacity: int, ttl_seconds: float) -> None:
        """@brief 创建缓存 / Create the cache.

        @param capacity 最多缓存的会话数 / Maximum cached conversations.
        @param ttl_seconds 缓存生存时间 / Cache time-to-live.
        @return None / None.
        """

        if capacity < 0:
            raise ValueError("capacity must not be negative")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._entries: dict[int, _CacheEntry] = {}
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        """@brief 返回当前缓存项数 / Return current cache-entry count.

        @return 当前缓存项数 / Current cache-entry count.
        """

        with self._lock:
            return len(self._entries)

    def get(self, conversation_id: int) -> ContextState | None:
        """@brief 读取未过期会话 / Get a non-expired conversation.

        @param conversation_id 会话标识 / Conversation identifier.
        @return 缓存状态；未命中或过期时返回 None / Cached state, or None on miss/expiry.
        """

        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(conversation_id)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(conversation_id, None)
                return None
            return entry.context

    def commit(self, conversation_id: int, context: ContextState) -> bool:
        """@brief 提交已持久化的会话状态 / Commit a persisted conversation state.

        @param conversation_id 会话标识 / Conversation identifier.
        @param context 与数据库一致的会话状态 / Conversation state consistent with the database.
        @return 是否已缓存；满且为新会话时返回 False / Whether cached; False when full for a new conversation.
        @note 容量满时不驱逐已有项，按调用方要求放弃本次准入。/
        When full, existing entries are not evicted; the new state is simply not admitted.
        """

        if self._capacity == 0:
            return False
        with self._lock:
            if conversation_id not in self._entries and len(self._entries) >= self._capacity:
                return False
            self._entries[conversation_id] = _CacheEntry(
                context=context,
                expires_at=time.monotonic() + self._ttl_seconds,
            )
            return True

    def invalidate(self, conversation_id: int) -> None:
        """@brief 使单个会话失效 / Invalidate one conversation.

        @param conversation_id 会话标识 / Conversation identifier.
        @return None / None.
        """

        with self._lock:
            self._entries.pop(conversation_id, None)

    def purge_expired(self) -> int:
        """@brief 清理全部过期会话 / Purge all expired conversations.

        @return 被清理的缓存项数量 / Number of purged cache entries.
        """

        now = time.monotonic()
        with self._lock:
            expired_ids = [
                conversation_id
                for conversation_id, entry in self._entries.items()
                if entry.expires_at <= now
            ]
            for conversation_id in expired_ids:
                self._entries.pop(conversation_id, None)
            return len(expired_ids)


CONVERSATION_CONTEXT_CACHE = ConversationContextCache(
    capacity=config.CONVERSATION_CONTEXT_CACHE_CAPACITY,
    ttl_seconds=config.CONVERSATION_CONTEXT_CACHE_TTL_SECONDS,
)
"""@brief 进程共享会话上下文缓存 / Process-shared conversation-context cache."""
