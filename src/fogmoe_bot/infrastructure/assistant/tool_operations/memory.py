"""@brief WorkingMemory-backed Assistant tool operation / 基于 WorkingMemory 的 Assistant 工具操作."""

import json
from typing import cast

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.memory.ports import WorkingMemoryQuery, WorkingMemoryReader
from fogmoe_bot.domain.context.token_estimator import estimate_tokens
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.memory.models import (
    GroupMemoryScope,
    PersonalMemoryScope,
    WorkingMemoryMessage,
)

from .parsing import bounded_int, iso_instant, required_text


_TOOL_RESULT_MAX_TOKENS = 4_096
"""@brief 单次 Memory tool 回填的保守 token 上限 / Conservative token cap for one Memory-tool result."""


async def search_memory(
    request: ToolEffectRequest,
    *,
    memory: WorkingMemoryReader,
) -> JsonValue:
    """@brief 在可信个人/群聊域中执行标准 Memory tool call / Execute a standard Memory tool call in the trusted personal/group scope.

    @param request 已验证工具请求 / Validated tool request.
    @param memory 每次调用 fresh retrieve 的 Memory 端口 / Memory port retrieving afresh per call.
    @return 当前 Agent Turn 可见的有界结果 / Bounded result visible in the current Agent turn.
    """

    query = required_text(request.arguments, "query")
    limit = bounded_int(request.arguments, "limit", minimum=1, maximum=6)
    scope = _scope(request)
    working_memory = await memory.retrieve(
        WorkingMemoryQuery(scope=scope, text=query, limit=limit)
    )
    scope_payload: JsonObject = (
        {"kind": "personal", "id": scope.user_id}
        if isinstance(scope, PersonalMemoryScope)
        else {"kind": "group", "id": scope.group_id}
    )
    results: list[JsonObject] = []
    for message in working_memory.messages:
        entry = _result_entry(message)
        if _fits(scope_payload, query, [*results, entry]):
            results.append(entry)
            continue
        prefix = _largest_fitting_prefix(
            scope_payload,
            query,
            results,
            message,
        )
        if prefix:
            entry["content"] = prefix
            entry["truncated"] = True
            results.append(entry)
        break
    return _payload(scope_payload, query, results)


def _payload(scope: JsonObject, query: str, results: list[JsonObject]) -> JsonObject:
    """@brief 构造标准 Memory tool payload / Build the canonical Memory-tool payload.

    @param scope 已授权域 / Authorized scope.
    @param query 原始 Query / Raw query.
    @param results 已预算结果 / Budgeted results.
    @return JSON payload / JSON payload.
    """

    return {
        "scope": scope,
        "query": query,
        "trust": "untrusted_historical_data",
        "results": cast(list[JsonValue], results),
    }


def _result_entry(message: WorkingMemoryMessage) -> JsonObject:
    """@brief 映射一条带 provenance 的结果 / Map one provenance-bearing result.

    @param message 工作记忆消息 / WorkingMemory message.
    @return JSON entry / JSON entry.
    """

    return {
        "passage_id": str(message.passage_id),
        "source_kind": message.source_kind,
        "source_id": str(message.source_id),
        "occurred_at": iso_instant(message.occurred_at),
        "content": message.content,
        "cosine_distance": message.cosine_distance,
    }


def _fits(scope: JsonObject, query: str, results: list[JsonObject]) -> bool:
    """@brief 判断 JSON 回填是否在预算内 / Test whether the JSON tool result fits its budget.

    @param scope 已授权域 / Authorized scope.
    @param query 原始 Query / Raw query.
    @param results 候选结果 / Candidate results.
    @return 未超预算为 True / True when within budget.
    """

    encoded = json.dumps(
        _payload(scope, query, results),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return estimate_tokens(encoded) <= _TOOL_RESULT_MAX_TOKENS


def _largest_fitting_prefix(
    scope: JsonObject,
    query: str,
    results: list[JsonObject],
    message: WorkingMemoryMessage,
) -> str:
    """@brief 二分寻找工具结果可容纳的正文前缀 / Binary-search the content prefix fitting the tool-result budget.

    @param scope 已授权域 / Authorized scope.
    @param query 原始 Query / Raw query.
    @param results 已接受结果 / Accepted results.
    @param message 当前消息 / Current message.
    @return 带省略号的前缀；无法容纳则为空 / Ellipsized prefix, or empty if none fits.
    """

    low = 0
    high = len(message.content)
    while low < high:
        middle = (low + high + 1) // 2
        entry = _result_entry(message)
        entry["content"] = message.content[:middle].rstrip() + "…"
        entry["truncated"] = True
        if _fits(scope, query, [*results, entry]):
            low = middle
        else:
            high = middle - 1
    return "" if low == 0 else message.content[:low].rstrip() + "…"


def _scope(request: ToolEffectRequest) -> PersonalMemoryScope | GroupMemoryScope:
    """@brief 从不可伪造的工具授权上下文派生 Memory 域 / Derive Memory scope from unforgeable tool authorization context.

    @param request 工具请求 / Tool request.
    @return 个人或当前群聊域 / Personal or current-group scope.
    @raise ValueError 群聊请求缺少 group_id / Group request lacks a group identifier.
    """

    context = request.context
    if not context.is_group:
        return PersonalMemoryScope(context.user_id)
    if context.group_id is None:
        raise ValueError("Group Memory tool requires group_id")
    return GroupMemoryScope(context.group_id)


__all__ = ["search_memory"]
