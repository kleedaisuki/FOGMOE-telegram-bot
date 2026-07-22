"""@brief Assistant 区间与定点历史读取工具 / Assistant interval and point history-read tool."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import cast

from fogmoe_bot.application.assistant.temporal_memory import (
    TemporalMemoryPassage,
    TemporalMemoryQuery,
    TemporalMemoryReader,
)
from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.timekeeping.service import TimeService
from fogmoe_bot.domain.context.token_estimator import estimate_tokens
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.memory.models import MAX_WORKING_MEMORY_MESSAGES
from fogmoe_bot.domain.retrieval import RetrievalScope
from fogmoe_bot.domain.temporal import UtcInterval, ensure_utc

from .parsing import bounded_int, optional_text


_TOOL_RESULT_MAX_TOKENS = 4_096
"""@brief 单次时间历史工具结果上限 / Per-call temporal-history tool-result limit."""


async def search_memory_by_time(
    request: ToolEffectRequest,
    *,
    memory: TemporalMemoryReader,
    time: TimeService,
) -> JsonValue:
    """@brief 在可信 scope 内读取区间或定点历史 / Read interval or point history inside the trusted scope.

    @param request 已验证工具请求 / Validated tool request.
    @param memory 独立时间历史 reader / Independent temporal-history reader.
    @param time 统一时区解析服务 / Unified time-zone parsing service.
    @return 当前 Agent Turn 可见的有界结果 / Bounded result visible in the current Agent turn.
    """

    window, anchor, temporal = _constraint(request, time=time)
    limit = bounded_int(
        request.arguments,
        "limit",
        minimum=1,
        maximum=MAX_WORKING_MEMORY_MESSAGES,
        default=64,
    )
    scope = _scope(request)
    passages = await memory.search(
        TemporalMemoryQuery(
            scope=scope,
            occurred_within=window,
            nearest_to=anchor,
            limit=limit,
        )
    )
    scope_payload: JsonObject = {"kind": scope.kind, "id": scope.scope_id}
    ranking = "nearest" if anchor is not None else "latest"
    results: list[JsonObject] = []
    for passage in passages:
        entry = _result_entry(passage)
        if _fits(scope_payload, temporal, ranking, [*results, entry]):
            results.append(entry)
            continue
        prefix = _largest_fitting_prefix(
            scope_payload,
            temporal,
            ranking,
            results,
            passage,
        )
        if prefix:
            entry["content"] = prefix
            entry["truncated"] = True
            results.append(entry)
        break
    return _payload(scope_payload, temporal, ranking, results)


def _constraint(
    request: ToolEffectRequest,
    *,
    time: TimeService,
) -> tuple[UtcInterval, datetime | None, JsonObject]:
    """@brief 将扁平工具参数解析为规范 UTC 查询 / Parse flat tool arguments into a canonical UTC query.

    @param request 已验证工具请求 / Validated tool request.
    @param time 统一时间解析服务 / Unified temporal parsing service.
    @return UTC 窗口、可选锚点及响应 metadata / UTC window, optional anchor, and response metadata.
    """

    start = optional_text(request.arguments, "start_time")
    end = optional_text(request.arguments, "end_time")
    around = optional_text(request.arguments, "around_time")
    if (start is None) != (end is None):
        raise ValueError("start_time and end_time must be provided together")
    if (start is not None) == (around is not None):
        raise ValueError(
            "provide either start_time/end_time or around_time, but not both"
        )
    zone = time.time_zone(optional_text(request.arguments, "timezone"))
    if around is not None:
        radius = bounded_int(
            request.arguments,
            "around_radius_minutes",
            minimum=1,
            maximum=10_080,
            default=60,
        )
        anchor = time.resolve(around, time_zone=zone.value)
        window = UtcInterval.around(anchor, timedelta(minutes=radius))
        return (
            window,
            anchor,
            {
                "kind": "around",
                "semantics": "[start,end)",
                "timezone": zone.value,
                "start_utc": _instant_text(window.start),
                "end_utc": _instant_text(window.end),
                "anchor_utc": _instant_text(anchor),
                "radius_minutes": radius,
            },
        )
    if start is None or end is None:
        raise ValueError("start_time and end_time are required for interval search")
    window = time.interval(start, end, time_zone=zone.value)
    return (
        window,
        None,
        {
            "kind": "interval",
            "semantics": "[start,end)",
            "timezone": zone.value,
            "start_utc": _instant_text(window.start),
            "end_utc": _instant_text(window.end),
        },
    )


def _scope(request: ToolEffectRequest) -> RetrievalScope:
    """@brief 从不可伪造上下文派生检索 scope / Derive retrieval scope from unforgeable context.

    @param request 工具请求 / Tool request.
    @return 个人或当前群聊 scope / Personal or current-group scope.
    """

    context = request.context
    if not context.is_group:
        return RetrievalScope("personal", context.user_id)
    if context.group_id is None:
        raise ValueError("Group temporal Memory search requires group_id")
    return RetrievalScope("group", context.group_id)


def _result_entry(passage: TemporalMemoryPassage) -> JsonObject:
    """@brief 映射一条有 provenance 的 passage / Map one provenance-bearing passage.

    @param passage 历史 passage / Historical passage.
    @return JSON 结果 / JSON result.
    """

    entry: JsonObject = {
        "passage_id": str(passage.passage_id),
        "source_kind": passage.source_kind,
        "source_id": str(passage.source_id),
        "occurred_at": _instant_text(passage.occurred_at),
        "content": passage.content,
    }
    if passage.temporal_distance_seconds is not None:
        entry["temporal_distance_seconds"] = passage.temporal_distance_seconds
    return entry


def _payload(
    scope: JsonObject,
    temporal: JsonObject,
    ranking: str,
    results: list[JsonObject],
) -> JsonObject:
    """@brief 构造 canonical 工具 payload / Build the canonical tool payload."""

    return {
        "scope": scope,
        "temporal": temporal,
        "ranking": ranking,
        "trust": "untrusted_historical_data",
        "results": cast(list[JsonValue], results),
    }


def _fits(
    scope: JsonObject,
    temporal: JsonObject,
    ranking: str,
    results: list[JsonObject],
) -> bool:
    """@brief 判断完整 JSON 是否在独立预算内 / Test the complete JSON against its independent budget."""

    encoded = json.dumps(
        _payload(scope, temporal, ranking, results),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return estimate_tokens(encoded) <= _TOOL_RESULT_MAX_TOKENS


def _largest_fitting_prefix(
    scope: JsonObject,
    temporal: JsonObject,
    ranking: str,
    results: list[JsonObject],
    passage: TemporalMemoryPassage,
) -> str:
    """@brief 二分寻找可容纳正文前缀 / Binary-search the largest fitting content prefix."""

    low = 0
    high = len(passage.content)
    while low < high:
        middle = (low + high + 1) // 2
        entry = _result_entry(passage)
        entry["content"] = passage.content[:middle].rstrip() + "…"
        entry["truncated"] = True
        if _fits(scope, temporal, ranking, [*results, entry]):
            low = middle
        else:
            high = middle - 1
    return "" if low == 0 else passage.content[:low].rstrip() + "…"


def _instant_text(value: datetime) -> str:
    """@brief 序列化 UTC 瞬间 / Serialize a UTC instant.

    @param value aware 时间 / Aware datetime.
    @return RFC 3339 兼容文本 / RFC-3339-compatible text.
    """

    return ensure_utc(value).isoformat().replace("+00:00", "Z")


__all__ = ["search_memory_by_time"]
