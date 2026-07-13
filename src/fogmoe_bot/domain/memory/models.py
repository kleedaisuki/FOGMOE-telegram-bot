"""@brief 长期记忆只读领域模型 / Long-term-memory read-domain models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from fogmoe_bot.domain.conversation.identity import ConversationId
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.conversation.temporal import ensure_utc


class MemorySourceKind(StrEnum):
    """@brief 记忆来源类别 / Memory-source kind."""

    COMPACTION_CHECKPOINT = "compaction_checkpoint"
    """@brief 从 Context Window checkpoint 形成 / Formed from a context-window checkpoint."""

    LEGACY_ARCHIVE = "legacy_archive"
    """@brief 从旧永久记录无损迁移 / Losslessly migrated from a legacy permanent record."""


@dataclass(frozen=True, slots=True)
class MemoryId:
    """@brief 稳定记忆身份 / Stable memory identity.

    @param value 内部 UUID / Internal UUID.
    @param legacy_value 旧用户可见数字 ID / Legacy user-visible numeric identifier.
    """

    value: UUID
    legacy_value: int | None = None

    def __post_init__(self) -> None:
        """@brief 校验可选旧身份 / Validate the optional legacy identity.

        @return None / None.
        @raise ValueError 旧 ID 非正或为 bool / Legacy ID is non-positive or boolean.
        """

        if self.legacy_value is not None and (
            isinstance(self.legacy_value, bool) or self.legacy_value <= 0
        ):
            raise ValueError("Memory legacy identity must be positive")

    @property
    def external_value(self) -> int | str:
        """@brief 返回稳定用户可见身份 / Return the stable user-visible identity.

        @return 旧数字 ID 或规范 UUID / Legacy numeric ID or canonical UUID.
        """

        return self.legacy_value if self.legacy_value is not None else str(self.value)


@dataclass(frozen=True, slots=True)
class MemoryProvenance:
    """@brief 记忆的不可变来源引用 / Immutable provenance of a memory.

    @param conversation_id 来源会话 / Source conversation.
    @param source_kind 来源类别 / Source kind.
    @param source_id 来源 artifact UUID / Source artifact UUID.
    @param source_digest 来源内容 SHA-256 / Source-content SHA-256.
    """

    conversation_id: ConversationId
    source_kind: MemorySourceKind
    source_id: UUID
    source_digest: str

    def __post_init__(self) -> None:
        """@brief 校验来源引用 / Validate the provenance reference.

        @return None / None.
        @raise ValueError 来源 ID 或 digest 非法 / Source ID or digest is invalid.
        """

        digest = self.source_digest.strip()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("Memory source digest must be lowercase SHA-256")
        object.__setattr__(self, "source_digest", digest)


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """@brief 用户可见永久记忆记录 / User-visible permanent-memory record.

    @param memory_id 记忆身份 / Memory identity.
    @param owner_user_id 所有者 / Owning user.
    @param provenance 不可变来源 / Immutable provenance.
    @param snapshot 冻结来源数据 / Frozen source data.
    @param summary 可选摘要 / Optional summary.
    @param created_at 形成时间 / Formation time.
    """

    memory_id: MemoryId
    owner_user_id: int
    provenance: MemoryProvenance
    snapshot: tuple[JsonObject, ...]
    summary: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        """@brief 隔离可变 JSON 并校验记录 / Isolate mutable JSON and validate the record.

        @return None / None.
        @raise ValueError 所有者或摘要非法 / Owner or summary is invalid.
        @raise TypeError snapshot 不是对象数组 / Snapshot is not an object array.
        """

        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Memory owner_user_id must be positive")
        summary = self.summary.strip() if self.summary is not None else None
        if summary == "":
            summary = None
        encoded = json.dumps(
            self.snapshot,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        decoded = cast(JsonValue, json.loads(encoded))
        if not isinstance(decoded, list) or not all(
            isinstance(item, dict) for item in decoded
        ):
            raise TypeError("Memory snapshot must contain only JSON objects")
        object.__setattr__(
            self,
            "snapshot",
            tuple(cast(JsonObject, item) for item in decoded),
        )
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


@dataclass(frozen=True, slots=True)
class MemorySearchHit:
    """@brief 有界记忆检索命中 / Bounded memory-search hit.

    @param memory_id 记忆身份 / Memory identity.
    @param created_at 形成时间 / Formation time.
    @param excerpt 有界命中片段 / Bounded matching excerpt.
    """

    memory_id: MemoryId
    created_at: datetime
    excerpt: str

    def __post_init__(self) -> None:
        """@brief 校验并规范化命中 / Validate and normalize the hit.

        @return None / None.
        @raise ValueError excerpt 为空或超界 / Excerpt is blank or oversized.
        """

        excerpt = self.excerpt.strip()
        if not excerpt or len(excerpt) > 4096:
            raise ValueError("Memory search excerpt must contain 1-4096 characters")
        object.__setattr__(self, "excerpt", excerpt)
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


__all__ = [
    "MemoryId",
    "MemoryProvenance",
    "MemoryRecord",
    "MemorySearchHit",
    "MemorySourceKind",
]
