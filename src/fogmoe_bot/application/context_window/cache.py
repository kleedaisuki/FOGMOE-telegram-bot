"""@brief 已提交 Context Window 历史的进程内缓存 / Process-local cache for committed context-window history."""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from fogmoe_bot.domain.conversation.identity import ConversationId, TurnId
from fogmoe_bot.domain.conversation.message import ConversationMessage


@dataclass(frozen=True, slots=True)
class CachedContextWindow:
    """@brief 一个 epoch/checkpoint 内连续历史的不可变缓存 / Immutable contiguous history cache within one epoch/checkpoint.

    @param conversation_id 所属会话 / Owning conversation.
    @param through_turn_id 缓存末端所属 Turn / Turn owning the cached tail.
    @param epoch_floor_sequence reset epoch 起点 / Reset-epoch floor.
    @param start_sequence 缓存窗口的排他起点 / Exclusive start of the cached window.
    @param through_sequence 缓存窗口的包含终点 / Inclusive end of the cached window.
    @param checkpoint_id 生成窗口的 compaction checkpoint / Compaction checkpoint used for the window.
    @param messages 已提交、严格递增的数据库消息 / Committed database messages in strict sequence order.
    @param include_history 是否为普通历史投影 / Whether this is a normal history projection.
    """

    conversation_id: ConversationId
    through_turn_id: TurnId
    epoch_floor_sequence: int
    start_sequence: int
    through_sequence: int
    checkpoint_id: str | None
    messages: tuple[ConversationMessage, ...]
    include_history: bool

    def __post_init__(self) -> None:
        """@brief 校验水位线与连续消息 / Validate watermarks and contiguous messages.

        @return None / None.
        """

        if self.epoch_floor_sequence < 0:
            raise ValueError("Cache epoch floor cannot be negative")
        if self.start_sequence < self.epoch_floor_sequence:
            raise ValueError("Cache start cannot precede its epoch floor")
        if self.through_sequence < self.start_sequence:
            raise ValueError("Cache end cannot precede its start")
        previous = self.start_sequence
        for message in self.messages:
            sequence = int(message.sequence)
            if message.draft.conversation_id != self.conversation_id:
                raise ValueError("Cached history crossed a conversation boundary")
            if sequence <= previous or sequence > self.through_sequence:
                raise ValueError("Cached history messages are not a valid window")
            previous = sequence
        if self.messages and previous != self.through_sequence:
            raise ValueError("Cached history does not reach its watermark")


@dataclass(frozen=True, slots=True)
class _CacheSlot:
    """@brief 一个带单调时钟失效时间的缓存槽 / Cache slot with a monotonic expiry time.

    @param value 历史快照 / History snapshot.
    @param expires_at 单调时钟过期点 / Monotonic-clock expiry point.
    """

    value: CachedContextWindow
    expires_at: float


class ContextWindowCache:
    """@brief 有时空边界的会话历史缓存 / Conversation-history cache with temporal and spatial bounds.

    仅缓存已经写入数据库的 append-only history。缓存命中只能复用相同 reset epoch 与
    compaction checkpoint 的连续前缀；其他情况安全降级为数据库全量投影。/
    Caches only database-committed append-only history. A hit can reuse a contiguous prefix only
    within the same reset epoch and compaction checkpoint; all other cases safely fall back to a
    full database projection.
    """

    def __init__(self, *, capacity: int, ttl_seconds: float) -> None:
        """@brief 创建 LRU+TTL 缓存 / Create an LRU-plus-TTL cache.

        @param capacity 最多会话数 / Maximum number of conversations.
        @param ttl_seconds 条目生存时间 / Entry time-to-live.
        @return None / None.
        """

        if capacity < 0:
            raise ValueError("History cache capacity cannot be negative")
        if ttl_seconds <= 0:
            raise ValueError("History cache TTL must be positive")
        self._capacity = capacity
        self._ttl_seconds = ttl_seconds
        self._slots: OrderedDict[ConversationId, _CacheSlot] = OrderedDict()

    def get(
        self,
        *,
        conversation_id: ConversationId,
        epoch_floor_sequence: int,
        start_sequence: int,
        checkpoint_id: str | None,
        include_history: bool,
        through_sequence: int,
    ) -> CachedContextWindow | None:
        """@brief 读取能作为当前投影连续前缀的历史 / Get a valid contiguous prefix for the current projection.

        @param conversation_id 会话 ID / Conversation identifier.
        @param epoch_floor_sequence 当前 reset epoch / Current reset epoch.
        @param start_sequence 当前 checkpoint 后的排他起点 / Exclusive start after the current checkpoint.
        @param checkpoint_id 当前 checkpoint ID / Current checkpoint identifier.
        @param include_history 是否包含普通历史 / Whether normal history is included.
        @param through_sequence 本次投影锚点末端 / Projection anchor end.
        @return 可复用前缀；未命中、过期或世代不一致时为 None / Reusable prefix, or None on miss, expiry, or generation mismatch.
        """

        slot = self._slots.get(conversation_id)
        if slot is None:
            return None
        if slot.expires_at <= time.monotonic():
            self._slots.pop(conversation_id, None)
            return None
        value = slot.value
        if (
            value.epoch_floor_sequence != epoch_floor_sequence
            or value.start_sequence != start_sequence
            or value.checkpoint_id != checkpoint_id
            or value.include_history is not include_history
            or value.through_sequence > through_sequence
        ):
            return None
        self._slots.move_to_end(conversation_id)
        return value

    def put(self, value: CachedContextWindow) -> bool:
        """@brief 提交已完成投影的历史窗口 / Commit a completed projection's history window.

        @param value 与数据库已提交记录一致的窗口 / Window consistent with committed database records.
        @return 是否被缓存；容量为零时为 False / Whether cached; False when capacity is zero.
        """

        if self._capacity == 0:
            return False
        self._slots[value.conversation_id] = _CacheSlot(
            value=value,
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        self._slots.move_to_end(value.conversation_id)
        while len(self._slots) > self._capacity:
            self._slots.popitem(last=False)
        return True

    def invalidate(self, conversation_id: ConversationId) -> None:
        """@brief 使一个会话的本地投影失效 / Invalidate one conversation's local projection.

        @param conversation_id 会话 ID / Conversation identifier.
        @return None / None.
        """

        self._slots.pop(conversation_id, None)


__all__ = ["CachedContextWindow", "ContextWindowCache"]
