"""@brief 瞬时工作记忆领域值 / Ephemeral WorkingMemory domain values."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fogmoe_bot.domain.temporal import ensure_utc


_SOURCE_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
"""@brief Memory 来源类别语法 / Memory-source-kind grammar."""


@dataclass(frozen=True, slots=True)
class PersonalMemoryScope:
    """@brief 与所有群聊隔离的个人 Memory 域 / Personal Memory scope isolated from every group.

    @param user_id 已认证用户 ID / Authenticated user identifier.
    """

    user_id: int

    def __post_init__(self) -> None:
        """@brief 校验个人域 / Validate the personal scope.

        @return None / None.
        @raise ValueError user_id 非正 / Non-positive user identifier.
        """

        if isinstance(self.user_id, bool) or self.user_id <= 0:
            raise ValueError("Personal Memory user_id must be positive")


@dataclass(frozen=True, slots=True)
class GroupMemoryScope:
    """@brief 与个人和其他群隔离的群聊 Memory 域 / Group Memory scope isolated from personal and other groups.

    @param group_id Telegram 群 ID / Telegram group identifier.
    """

    group_id: int

    def __post_init__(self) -> None:
        """@brief 校验群域 / Validate the group scope.

        @return None / None.
        @raise ValueError group_id 为零 / Zero group identifier.
        """

        if isinstance(self.group_id, bool) or self.group_id == 0:
            raise ValueError("Group Memory group_id must be non-zero")


type MemoryScope = PersonalMemoryScope | GroupMemoryScope
"""@brief 穷尽的个人/群聊 Memory 域 / Exhaustive personal/group Memory scope."""


@dataclass(frozen=True, slots=True)
class WorkingMemoryMessage:
    """@brief 一条按当前 Query 换入的历史消息 / One historical message paged in for the current query.

    @param passage_id 检索 passage ID / Retrieval-passage identifier.
    @param source_kind 不透明来源类别 / Opaque source kind.
    @param source_id 不透明来源 ID / Opaque source identifier.
    @param occurred_at 来源事件时间 / Source-event time.
    @param content 规范历史文本 / Canonical historical text.
    @param cosine_distance 余弦距离，越小越相关 / Cosine distance; lower is more relevant.
    """

    passage_id: UUID
    source_kind: str
    source_id: UUID
    occurred_at: datetime
    content: str
    cosine_distance: float

    def __post_init__(self) -> None:
        """@brief 校验来源、正文与距离 / Validate source, content, and distance.

        @return None / None.
        @raise ValueError 任一字段违反 WorkingMemory 边界 / Any field violates WorkingMemory bounds.
        """

        source_kind = self.source_kind.strip()
        content = self.content.strip()
        distance = float(self.cosine_distance)
        if _SOURCE_KIND_PATTERN.fullmatch(source_kind) is None:
            raise ValueError("WorkingMemory source_kind has invalid syntax")
        if not content or len(content) > 20_000:
            raise ValueError("WorkingMemory content must contain 1-20000 characters")
        if not math.isfinite(distance) or not 0.0 <= distance <= 2.000001:
            raise ValueError("WorkingMemory cosine distance must be between 0 and 2")
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "cosine_distance", distance)
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))


@dataclass(frozen=True, slots=True)
class WorkingMemory:
    """@brief 一次模型 Query 的非缓存工作记忆 / Non-cached WorkingMemory for one model query.

    @param scope 强隔离 Memory 域 / Strongly isolated Memory scope.
    @param query 未改写的当前 Query / Unrewritten current query.
    @param messages 按相关性排序的历史消息 / Historical messages ordered by relevance.
    @note WorkingMemory 既不属于 ContextState，也不能持久化进 Conversation 或参与 compaction。/
        WorkingMemory belongs to neither ContextState nor Conversation and must never participate
        in compaction.
    """

    scope: MemoryScope
    query: str
    messages: tuple[WorkingMemoryMessage, ...]

    def __post_init__(self) -> None:
        """@brief 校验 Query、容量与唯一性 / Validate query, capacity, and uniqueness.

        @return None / None.
        @raise ValueError Query 非法、页面过多或 passage 重复 / Invalid query, excess pages, or duplicate passages.
        """

        query = self.query.strip()
        messages = tuple(self.messages)
        if not query or len(query) > 20_000:
            raise ValueError("WorkingMemory query must contain 1-20000 characters")
        if len(messages) > 20:
            raise ValueError("WorkingMemory cannot contain more than 20 messages")
        passage_ids = tuple(message.passage_id for message in messages)
        if len(set(passage_ids)) != len(passage_ids):
            raise ValueError("WorkingMemory passage IDs must be unique")
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "messages", messages)


__all__ = [
    "GroupMemoryScope",
    "MemoryScope",
    "PersonalMemoryScope",
    "WorkingMemory",
    "WorkingMemoryMessage",
]
