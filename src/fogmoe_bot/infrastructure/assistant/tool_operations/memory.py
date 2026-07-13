"""Memory-port-backed Assistant operations / 基于 Memory 端口的 Assistant operations."""

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.memory.queries import (
    MemoryPageQuery,
    MemoryReader,
    MemorySearchQuery,
)
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)

from .parsing import bounded_int, iso_instant


async def fetch_permanent_summaries(
    request: ToolEffectRequest,
    *,
    memory: MemoryReader,
) -> JsonValue:
    """读取永久摘要的有界窗口 / Read a bounded permanent-summary window."""

    start = bounded_int(request.arguments, "start", minimum=1)
    end = bounded_int(request.arguments, "end", minimum=start)
    limit = min(5, end - start + 1)
    total = await memory.count_summaries(request.context.user_id)
    records_page = await memory.read_page(
        MemoryPageQuery(
            owner_user_id=request.context.user_id,
            limit=limit,
            offset=start - 1,
            summaries_only=True,
        )
    )
    records: list[JsonValue] = [
        {
            "record_id": record.memory_id.external_value,
            "summary": record.summary,
            "created_at": iso_instant(record.created_at),
        }
        for record in records_page
        if record.summary is not None
    ]
    return {"user_id": request.context.user_id, "total": total, "records": records}


async def search_permanent_records(
    request: ToolEffectRequest,
    *,
    memory: MemoryReader,
) -> JsonValue:
    """在 entitlement-aware memory view 中执行有界检索。"""

    pattern = str(request.arguments["pattern"])
    limit = bounded_int(request.arguments, "limit", minimum=1, maximum=50)
    oldest_first = bool(request.arguments.get("oldest_first", False))
    search_result = await memory.search(
        MemorySearchQuery(
            owner_user_id=request.context.user_id,
            pattern=pattern,
            limit=limit,
            oldest_first=oldest_first,
        )
    )
    results: list[JsonValue] = [
        {
            "record_id": hit.memory_id.external_value,
            "created_at": iso_instant(hit.created_at),
            "excerpt": hit.excerpt,
        }
        for hit in search_result.hits
    ]
    response: JsonObject = {
        "user_id": request.context.user_id,
        "pattern": pattern,
        "oldest_first": oldest_first,
        "results": results,
    }
    if search_result.warning is not None:
        response["warning"] = search_result.warning
    return response
