"""@brief Assistant 时间历史读取端口 / Assistant temporal-history read port.

该模块只定义显式工具所需的查询读模型，不参与 WorkingMemory、embedding、
projection 或 compaction。/ This module defines only the query read model required by the
explicit tool; it does not participate in WorkingMemory, embeddings, projection, or compaction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from fogmoe_bot.domain.memory.models import MAX_WORKING_MEMORY_MESSAGES
from fogmoe_bot.domain.retrieval import RetrievalScope
from fogmoe_bot.domain.temporal import UtcInterval, ensure_utc


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalMemoryQuery:
    """@brief 受信任域约束的区间或定点历史查询 / Interval or point history query constrained by a trusted scope.

    @param scope 由运行时身份派生的检索域 / Retrieval scope derived from runtime identity.
    @param occurred_within 左闭右开 UTC 区间 / Half-open UTC interval.
    @param limit 最大 passage 数 / Maximum passage count.
    @param nearest_to 可选最近时间排序锚点 / Optional nearest-time ranking anchor.
    """

    scope: RetrievalScope
    occurred_within: UtcInterval
    limit: int
    nearest_to: datetime | None = None

    def __post_init__(self) -> None:
        """@brief 校验定点与数量边界 / Validate the point and count bounds.

        @return None / None.
        @raise ValueError 锚点越界或 limit 非法时抛出 / Raised for an out-of-window point or invalid limit.
        """

        nearest = None if self.nearest_to is None else ensure_utc(self.nearest_to)
        if nearest is not None and not self.occurred_within.contains(nearest):
            raise ValueError("Temporal Memory point must lie inside its interval")
        if not 1 <= self.limit <= MAX_WORKING_MEMORY_MESSAGES:
            raise ValueError(
                "Temporal Memory limit must be between 1 and "
                f"{MAX_WORKING_MEMORY_MESSAGES}"
            )
        object.__setattr__(self, "nearest_to", nearest)


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalMemoryPassage:
    """@brief 工具读取的一条历史 passage / One historical passage read by the tool.

    @param passage_id 稳定 passage ID / Stable passage identifier.
    @param source_kind 来源类别 / Source kind.
    @param source_id 来源实体 ID / Source entity identifier.
    @param occurred_at 来源事件时刻 / Source-event instant.
    @param content 历史正文 / Historical content.
    @param temporal_distance_seconds 可选锚点绝对秒差 / Optional absolute seconds from the point anchor.
    """

    passage_id: UUID
    source_kind: str
    source_id: UUID
    occurred_at: datetime
    content: str
    temporal_distance_seconds: float | None = None

    def __post_init__(self) -> None:
        """@brief 规范 tool read model / Normalize the tool read model.

        @return None / None.
        @raise ValueError 来源、正文或距离非法时抛出 / Raised for invalid provenance, content, or distance.
        """

        source_kind = self.source_kind.strip()
        content = self.content.strip()
        if not source_kind or len(source_kind) > 100:
            raise ValueError(
                "Temporal Memory source_kind must contain 1-100 characters"
            )
        if not content or len(content) > 20_000:
            raise ValueError("Temporal Memory content must contain 1-20000 characters")
        distance = self.temporal_distance_seconds
        if distance is not None:
            distance = float(distance)
            if not math.isfinite(distance) or distance < 0.0:
                raise ValueError(
                    "Temporal Memory distance must be finite and non-negative"
                )
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "temporal_distance_seconds", distance)


class TemporalMemoryReader(Protocol):
    """@brief 独立于记忆生成系统的时间历史读取端口 / Temporal-history reader independent of the memory-generation system."""

    async def search(
        self, query: TemporalMemoryQuery
    ) -> tuple[TemporalMemoryPassage, ...]:
        """@brief 按区间或定点读取 passage / Read passages by interval or point.

        @param query 已验证查询 / Validated query.
        @return 稳定排序的历史 passages / Stably ordered historical passages.
        """

        ...


__all__ = [
    "TemporalMemoryPassage",
    "TemporalMemoryQuery",
    "TemporalMemoryReader",
]
