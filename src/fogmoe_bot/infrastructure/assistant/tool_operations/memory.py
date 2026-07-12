"""Retention-backed Assistant memory operations / 基于 retention 的 Assistant 记忆 operations."""

import json
import re
from collections.abc import Sequence
from typing import Protocol

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.conversation.retention import RetentionSegment

from .parsing import bounded_int, iso_instant


class PermanentMemoryReader(Protocol):
    """受付费 quota 约束的 retention view。"""

    async def count_visible_summaries(self, owner_user_id: int) -> int:
        """统计可见摘要 / Count visible summaries."""

        ...

    async def fetch_visible_summaries(
        self,
        owner_user_id: int,
        *,
        limit: int,
        offset: int,
    ) -> Sequence[RetentionSegment]:
        """读取可见摘要 / Read visible summaries."""

        ...

    async def fetch_visible_segments(
        self,
        owner_user_id: int,
        *,
        newest_first: bool,
        limit: int,
        offset: int,
    ) -> Sequence[RetentionSegment]:
        """读取可搜索 snapshots / Read searchable snapshots."""

        ...


async def fetch_permanent_summaries(
    request: ToolEffectRequest,
    *,
    memory: PermanentMemoryReader,
) -> JsonValue:
    """读取永久摘要的有界窗口 / Read a bounded permanent-summary window."""

    start = bounded_int(request.arguments, "start", minimum=1)
    end = bounded_int(request.arguments, "end", minimum=start)
    limit = min(5, end - start + 1)
    total = await memory.count_visible_summaries(request.context.user_id)
    segments = await memory.fetch_visible_summaries(
        request.context.user_id,
        limit=limit,
        offset=start - 1,
    )
    records: list[JsonValue] = [
        {
            "record_id": _record_id(segment),
            "summary": segment.summary.text,
            "created_at": iso_instant(segment.completed_at or segment.draft.created_at),
        }
        for segment in segments
        if segment.summary is not None
    ]
    return {"user_id": request.context.user_id, "total": total, "records": records}


async def search_permanent_records(
    request: ToolEffectRequest,
    *,
    memory: PermanentMemoryReader,
) -> JsonValue:
    """在 quota-aware retention view 中有界扫描永久记录。"""

    pattern = str(request.arguments["pattern"])
    warning: str | None = None
    try:
        matcher = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error:
        matcher = re.compile(re.escape(pattern), re.IGNORECASE | re.DOTALL)
        warning = "Invalid regex; matched as literal text"
    limit = bounded_int(request.arguments, "limit", minimum=1, maximum=50)
    oldest_first = bool(request.arguments.get("oldest_first", False))
    segments = await memory.fetch_visible_segments(
        request.context.user_id,
        newest_first=not oldest_first,
        limit=min(500, max(50, limit * 20)),
        offset=0,
    )
    results: list[JsonValue] = []
    for segment in segments:
        text = json.dumps(
            segment.draft.source_snapshot,
            ensure_ascii=False,
            default=str,
        )
        match = matcher.search(text)
        if match is None:
            continue
        results.append(
            {
                "record_id": _record_id(segment),
                "created_at": iso_instant(
                    segment.completed_at or segment.draft.created_at
                ),
                "excerpt": text[max(0, match.start() - 300) : match.end() + 300],
            }
        )
        if len(results) >= limit:
            break
    response: JsonObject = {
        "user_id": request.context.user_id,
        "pattern": pattern,
        "oldest_first": oldest_first,
        "results": results,
    }
    if warning is not None:
        response["warning"] = warning
    return response


def _record_id(segment: RetentionSegment) -> int | str:
    """优先保留迁移数据的 legacy ID，否则返回稳定 Segment UUID。"""

    legacy_id = segment.draft.legacy_record_id
    return legacy_id if legacy_id is not None else str(segment.segment_id)
