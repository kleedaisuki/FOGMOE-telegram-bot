"""Assistant diary read/mutation operations / Assistant 日记读写 operations."""

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.infrastructure.database import connection as db_connection

from .parsing import bounded_int, iso_instant, optional_int, required_connection


_MAX_DIARY_CHARS = 10_000
_MAX_DIARY_PAGES = 100


async def execute_diary(
    request: ToolEffectRequest,
    *,
    connection: AsyncConnection | None,
) -> JsonValue:
    """读取或在 receipt transaction 中原子更新一页 diary。"""

    action = str(request.arguments.get("action", "read"))
    page = bounded_int(
        request.arguments,
        "page",
        minimum=1,
        maximum=_MAX_DIARY_PAGES,
    )
    if connection is not None:
        await db_connection.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"assistant-diary:{request.context.user_id}",),
            connection=connection,
        )
    lock = " FOR UPDATE" if connection is not None else ""
    row = await db_connection.fetch_one(
        "SELECT content, created_at, updated_at FROM conversation.ai_user_diary_pages "
        f"WHERE user_id = %s AND page_no = %s{lock}",
        (request.context.user_id, page),
        connection=connection,
    )
    content = str(row[0]) if row is not None else ""
    if action == "read":
        return _read_diary(request, page=page, content=content, row=row)

    transaction = required_connection(connection)
    max_row = await db_connection.fetch_one(
        "SELECT COALESCE(MAX(page_no), 0) FROM conversation.ai_user_diary_pages "
        "WHERE user_id = %s",
        (request.context.user_id,),
        connection=transaction,
    )
    max_page = int(max_row[0]) if max_row is not None else 0
    if row is None and (page > max_page + 1 or max_page >= _MAX_DIARY_PAGES):
        return {"error": "Diary page must be created sequentially"}
    supplied = request.arguments.get("content")
    if not isinstance(supplied, str):
        return {"error": "Missing content for diary update"}
    merged = _mutate_diary(
        action,
        original=content,
        supplied=supplied,
        start_line=optional_int(request.arguments, "start_line"),
        end_line=optional_int(request.arguments, "end_line"),
    )
    if isinstance(merged, dict):
        return merged
    truncated = len(merged) > _MAX_DIARY_CHARS
    if truncated:
        merged = merged[-_MAX_DIARY_CHARS:]
    await db_connection.execute(
        "INSERT INTO conversation.ai_user_diary_pages (user_id, page_no, content) "
        "VALUES (%s, %s, %s) ON CONFLICT (user_id, page_no) DO UPDATE "
        "SET content = EXCLUDED.content, updated_at = CURRENT_TIMESTAMP",
        (request.context.user_id, page, merged),
        connection=transaction,
    )
    return {
        "status": "updated",
        "action": action,
        "page": page,
        "total_lines": len(merged.splitlines()),
        "length": len(merged),
        "truncated": truncated,
    }


def _read_diary(
    request: ToolEffectRequest,
    *,
    page: int,
    content: str,
    row: Sequence[object] | None,
) -> JsonValue:
    """投影 diary read 并可选附加行号 / Project a diary read with optional line numbers."""

    lines = content.splitlines()
    start = optional_int(request.arguments, "start_line") or 1
    end = optional_int(request.arguments, "end_line") or len(lines)
    selected = lines[max(0, start - 1) : max(0, end)]
    result: JsonObject = {
        "status": "ok",
        "action": "read",
        "page": page,
        "total_lines": len(lines),
        "length": len(content),
        "content": "\n".join(selected),
        "created_at": iso_instant(row[1]) if row is not None else None,
        "updated_at": iso_instant(row[2]) if row is not None else None,
    }
    if bool(request.arguments.get("line_numbers", False)):
        result["lines"] = [
            {"line": start + index, "content": line}
            for index, line in enumerate(selected)
        ]
    return result


def _mutate_diary(
    action: str,
    *,
    original: str,
    supplied: str,
    start_line: int | None,
    end_line: int | None,
) -> str | JsonObject:
    """计算纯 diary mutation，不触碰 persistence。"""

    if action == "overwrite":
        return supplied
    if action == "append":
        return (
            f"{original}\n{supplied}"
            if original and not original.endswith("\n")
            else f"{original}{supplied}"
        )
    if action != "patch" or start_line is None or end_line is None:
        return {"error": "patch requires start_line and end_line"}
    lines = original.splitlines()
    if end_line < start_line or start_line > len(lines) + 1:
        return {"error": "Diary patch range is invalid"}
    lines[start_line - 1 : min(end_line, len(lines))] = supplied.splitlines()
    return "\n".join(lines)
