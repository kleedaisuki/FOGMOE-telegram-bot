import json
import re
import base64
from typing import Dict, Optional

from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import (
    conversation_repository,
    user_repository,
)

from .context import get_tool_request_context

MAX_USER_DIARY_PAGE_CHARS = 10000
MAX_USER_DIARY_PAGES = 100
_GROUP_CONTEXT_BOT_ID: int | None = None
_GROUP_CONTEXT_BOT_NAME = "FogMoeBot"


def set_group_context_bot_identity(user_id: int, display_name: str | None = None) -> None:
    """@brief 注册群聊上下文中的 Bot 身份 / Register the bot identity for group context.

    @param user_id Telegram Bot 用户 ID / Telegram bot user identifier.
    @param display_name 可选显示名 / Optional display name.
    @return None / None.
    """

    global _GROUP_CONTEXT_BOT_ID, _GROUP_CONTEXT_BOT_NAME
    _GROUP_CONTEXT_BOT_ID = user_id
    if display_name:
        _GROUP_CONTEXT_BOT_NAME = display_name


def get_help_text_tool() -> dict:
    """Return the configured help command list for the bot."""
    return {"help_text": config.HELP_TEXT}


def fetch_group_context_tool(
    window_size: int = 10,
    **kwargs,
) -> dict:
    """Retrieve recent messages before the current group chat message."""
    context = get_tool_request_context()
    if not context.get("is_group"):
        return {"error": "This is not a group chat, cannot fetch context"}

    target_group_id = context.get("group_id")
    if not target_group_id:
        return {"error": "Missing group chat identifier, cannot fetch context"}

    current_message_id = context.get("message_id")

    try:
        window_size = max(1, min(int(window_size), 100))
    except (TypeError, ValueError):
        window_size = 10

    around_message_id = current_message_id

    rows = db_connection.run_sync(
        conversation_repository.fetch_group_context_rows(
            target_group_id,
            around_message_id,
            window_size,
        )
    )
    context_messages = [
        {
            "message_id": row["message_id"],
            "user_id": row["user_id"],
            "message_type": row["message_type"],
            "username": (
                _GROUP_CONTEXT_BOT_NAME
                if _GROUP_CONTEXT_BOT_ID is not None
                and row["user_id"] == _GROUP_CONTEXT_BOT_ID
                else row.get("username")
            ),
            "content": _decode_group_content(
                row.get("content", ""),
                row["message_type"],
            ),
            "created_at": (
                row["created_at"].isoformat(sep=" ")
                if row.get("created_at")
                else None
            ),
        }
        for row in rows
    ]
    return {
        "group_id": target_group_id,
        "around_message_id": around_message_id,
        "window_size": window_size,
        "messages": context_messages,
    }


def _decode_group_content(content: object, message_type: str) -> str:
    """@brief 解码群聊上下文内容 / Decode group-context content.

    @param content 数据库存储内容 / Stored content.
    @param message_type 消息类型 / Message type.
    @return 面向 Agent 的可读文本 / Agent-readable text.
    """

    text = str(content or "")
    if message_type == "text":
        return text
    try:
        return base64.b64decode(text.encode("ascii")).decode("utf-8")
    except Exception:
        return text


def fetch_permanent_summaries_tool(
    start: Optional[int] = None,
    end: Optional[int] = None,
    **kwargs,
) -> dict:
    """Retrieve current user's permanent conversation summaries."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"user_id": None, "error": "Missing user information, cannot retrieve summaries"}

    try:
        start_idx = int(start) if start is not None else 1
    except (TypeError, ValueError):
        start_idx = 1

    try:
        end_idx = int(end) if end is not None else start_idx
    except (TypeError, ValueError):
        end_idx = start_idx

    if start_idx < 1:
        start_idx = 1
    if end_idx < start_idx:
        end_idx = start_idx

    window_size = end_idx - start_idx + 1
    window_size = max(1, min(window_size, 5))
    offset = start_idx - 1

    total_rows = db_connection.run_sync(
        conversation_repository.count_summarised_permanent_records(user_id)
    )

    rows = db_connection.run_sync(
        conversation_repository.fetch_summarised_permanent_records(
            user_id,
            limit=window_size,
            offset=offset,
        )
    )

    records = []
    for row in rows:
        record_id, summary_text, created_at = row
        records.append(
            {
                "record_id": record_id,
                "created_at": created_at.isoformat(sep=" ") if created_at else None,
                "summary": summary_text,
            }
        )

    return {
        "user_id": user_id,
        "total": total_rows,
        "range_start": start_idx,
        "range_end": start_idx + len(records) - 1 if records else start_idx - 1,
        "records": records,
    }


def search_permanent_records_tool(
    pattern: str,
    limit: Optional[int] = None,
    oldest_first: Optional[bool] = None,
    **kwargs,
) -> dict:
    """Search user's permanent conversation snapshots with a regex pattern."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"user_id": None, "error": "Missing user information, cannot search records"}

    if not isinstance(pattern, str) or not pattern.strip():
        return {"user_id": user_id, "error": "Missing search pattern"}

    try:
        limit_value = int(limit) if limit is not None else 5
    except (TypeError, ValueError):
        limit_value = 5
    limit_value = max(1, min(limit_value, 50))

    oldest_first_value = False
    if isinstance(oldest_first, bool):
        oldest_first_value = oldest_first
    elif isinstance(oldest_first, str):
        oldest_first_value = oldest_first.strip().lower() in {"1", "true", "yes", "y"}

    warning = None
    try:
        matcher = re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error:
        warning = "Invalid regex pattern, treated as literal string"
        matcher = re.compile(re.escape(pattern), re.IGNORECASE | re.DOTALL)

    total_rows = db_connection.run_sync(
        conversation_repository.count_permanent_records(user_id)
    )
    if total_rows <= 0:
        response = {
            "user_id": user_id,
            "pattern": pattern,
            "limit": limit_value,
            "oldest_first": oldest_first_value,
            "results": [],
        }
        if warning:
            response["warning"] = warning
        return response

    max_records = db_connection.PERMANENT_RECORDS_KEEP
    try:
        account = db_connection.run_sync(user_repository.fetch_user_account(user_id))
    except Exception:
        account = None
    if account and account.permanent_records_limit is not None:
        max_records = account.permanent_records_limit
    max_records = max(1, max_records)

    scan_limit = min(max_records, total_rows)

    batch_size = 50

    def _fetch_rows(offset: int, size: int) -> list[tuple]:
        return db_connection.run_sync(
            conversation_repository.fetch_permanent_records_batch(
                user_id,
                newest_first=not oldest_first_value,
                limit=size,
                offset=offset,
            )
        )

    def _record_position(offset: int, row_index: int) -> int:
        if oldest_first_value:
            return total_rows - (offset + row_index)
        return offset + row_index + 1

    def _scan_rows(rows: list[tuple], results: list[dict], offset: int) -> list[dict]:
        for row_index, row in enumerate(rows):
            _record_id, snapshot_text, created_at = row
            if isinstance(snapshot_text, bytes):
                snapshot_text = snapshot_text.decode("utf-8")

            try:
                messages = json.loads(snapshot_text) if isinstance(snapshot_text, str) else snapshot_text
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

            if not isinstance(messages, list):
                continue

            filtered_messages = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = message.get("content")
                if content is None:
                    continue
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                if role == "user" and 'origin="history_state"' in content:
                    continue
                filtered_messages.append(
                    {
                        "role": role,
                        "content": content,
                    }
                )

            if not filtered_messages:
                continue

            for idx in range(len(filtered_messages) - 1, -1, -1):
                content = filtered_messages[idx]["content"]
                if not matcher.search(content):
                    continue

                before_start = max(0, idx - 5)
                after_end = min(len(filtered_messages), idx + 6)
                before = [
                    {"index": before_start + offset, **msg}
                    for offset, msg in enumerate(filtered_messages[before_start:idx])
                ]
                after = [
                    {"index": idx + 1 + offset, **msg}
                    for offset, msg in enumerate(filtered_messages[idx + 1 : after_end])
                ]
                results.append(
                    {
                        "record_position": _record_position(offset, row_index),
                        "created_at": created_at.isoformat(sep=" ") if created_at else None,
                        "match": {"index": idx, **filtered_messages[idx]},
                        "before": before,
                        "after": after,
                    }
                )
                if len(results) >= limit_value:
                    return results
        return results

    results: list[dict] = []
    offset = 0
    remaining = scan_limit
    while remaining > 0 and len(results) < limit_value:
        fetch_size = min(batch_size, remaining)
        rows = _fetch_rows(offset, fetch_size)
        if not rows:
            break
        results = _scan_rows(rows, results, offset)
        if len(rows) < fetch_size:
            break
        offset += fetch_size
        remaining -= fetch_size

    response = {
        "user_id": user_id,
        "pattern": pattern,
        "limit": limit_value,
        "oldest_first": oldest_first_value,
        "results": results,
    }
    if warning:
        response["warning"] = warning

    return response


def user_diary_tool(
    action: Optional[str] = None,
    content: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    line_numbers: Optional[bool] = None,
    page: Optional[int] = None,
    **kwargs,
) -> dict:
    """Read or update the internal diary for the current user."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"user_id": None, "error": "Missing user information, cannot access diary"}

    action_value = (action or "read").strip().lower()
    if action_value in {"read", "view", "get"}:
        action_value = "read"
    elif action_value in {"append", "add", "increment"}:
        action_value = "append"
    elif action_value in {"patch", "edit", "update", "modify"}:
        action_value = "patch"
    elif action_value in {"overwrite", "replace", "set"}:
        action_value = "overwrite"
    else:
        return {"user_id": user_id, "error": f"Unknown action: {action}"}

    try:
        page_value = int(page) if page is not None else 1
    except (TypeError, ValueError):
        return {"user_id": user_id, "error": "Invalid page number"}
    if page_value < 1 or page_value > MAX_USER_DIARY_PAGES:
        return {"user_id": user_id, "error": f"Page number out of range (max={MAX_USER_DIARY_PAGES})"}

    max_page = db_connection.run_sync(conversation_repository.fetch_max_diary_page(user_id))

    row = db_connection.run_sync(
        conversation_repository.fetch_diary_page(user_id, page_value)
    )

    diary_content = ""
    created_at = None
    updated_at = None
    page_exists = False
    if row:
        page_exists = True
        diary_content, created_at, updated_at = row
        if isinstance(diary_content, bytes):
            diary_content = diary_content.decode("utf-8")

    warnings: list[str] = []
    if action_value == "read" and content is not None:
        warnings.append("content ignored for read action")
    if action_value in {"append", "overwrite"} and (start_line is not None or end_line is not None):
        warnings.append("line range ignored for append/overwrite action")

    if action_value == "read":
        line_numbers_value = False
        if isinstance(line_numbers, bool):
            line_numbers_value = line_numbers
        elif isinstance(line_numbers, str):
            line_numbers_value = line_numbers.strip().lower() in {"1", "true", "yes", "y"}

        lines = diary_content.splitlines()
        total_lines = len(lines)
        content_length = len(diary_content)

        if start_line is None and end_line is None:
            response = {
                "user_id": user_id,
                "action": "read",
                "page": page_value,
                "total_pages": max_page,
                "total_lines": total_lines,
                "length": content_length,
                "content": diary_content,
                "created_at": created_at.isoformat(sep=" ") if created_at else None,
                "updated_at": updated_at.isoformat(sep=" ") if updated_at else None,
            }
            if line_numbers_value:
                response["lines"] = [
                    {"line": idx + 1, "content": line}
                    for idx, line in enumerate(lines)
                ]
            if warnings:
                response["warning"] = "; ".join(warnings)
            return response

        try:
            start_value = int(start_line) if start_line is not None else 1
            end_value = int(end_line) if end_line is not None else total_lines
        except (TypeError, ValueError):
            return {"user_id": user_id, "error": "Invalid line range"}

        if total_lines == 0:
            response = {
                "user_id": user_id,
                "action": "read",
                "page": page_value,
                "total_pages": max_page,
                "total_lines": 0,
                "length": 0,
                "range": {"start_line": 0, "end_line": 0},
                "content": "",
                "created_at": created_at.isoformat(sep=" ") if created_at else None,
                "updated_at": updated_at.isoformat(sep=" ") if updated_at else None,
            }
            if line_numbers_value:
                response["lines"] = []
            if warnings:
                response["warning"] = "; ".join(warnings)
            return response

        if start_value < 1:
            start_value = 1
        if end_value < start_value:
            return {"user_id": user_id, "error": "Invalid line range"}
        if total_lines and end_value > total_lines:
            end_value = total_lines

        selected_lines = lines[start_value - 1 : end_value] if total_lines else []
        response = {
            "user_id": user_id,
            "action": "read",
            "page": page_value,
            "total_pages": max_page,
            "total_lines": total_lines,
            "length": content_length,
            "range": {"start_line": start_value, "end_line": end_value},
            "content": "\n".join(selected_lines),
            "created_at": created_at.isoformat(sep=" ") if created_at else None,
            "updated_at": updated_at.isoformat(sep=" ") if updated_at else None,
        }
        if line_numbers_value:
            response["lines"] = [
                {"line": start_value + idx, "content": line}
                for idx, line in enumerate(selected_lines)
            ]
        if warnings:
            response["warning"] = "; ".join(warnings)
        return response

    if not page_exists:
        if max_page >= MAX_USER_DIARY_PAGES:
            return {"user_id": user_id, "error": f"Diary page limit reached (max={MAX_USER_DIARY_PAGES})"}
        if page_value > max_page + 1:
            return {
                "user_id": user_id,
                "error": f"Page out of range; create next page first (max={MAX_USER_DIARY_PAGES})",
            }

    if content is None:
        return {"user_id": user_id, "error": "Missing content for diary update"}

    content_value = content if isinstance(content, str) else str(content)
    if action_value == "patch":
        lines = diary_content.splitlines()
        total_lines = len(lines)
        if start_line is None or end_line is None:
            return {"user_id": user_id, "error": "Missing line range for patch"}
        try:
            start_value = int(start_line)
            end_value = int(end_line)
        except (TypeError, ValueError):
            return {"user_id": user_id, "error": "Invalid line range"}

        if start_value < 1 or end_value < start_value:
            return {"user_id": user_id, "error": "Invalid line range"}
        if start_value > total_lines + 1:
            return {"user_id": user_id, "error": "Line range out of bounds"}

        start_idx = start_value - 1
        end_idx = min(end_value, total_lines)
        replacement_lines = content_value.splitlines()
        lines[start_idx:end_idx] = replacement_lines
        merged_content = "\n".join(lines)
    elif action_value == "append":
        if diary_content and not diary_content.endswith("\n"):
            merged_content = f"{diary_content}\n{content_value}"
        else:
            merged_content = f"{diary_content}{content_value}"
    else:
        merged_content = content_value

    truncated = False
    if len(merged_content) > MAX_USER_DIARY_PAGE_CHARS:
        merged_content = merged_content[-MAX_USER_DIARY_PAGE_CHARS:]
        truncated = True

    db_connection.run_sync(
        conversation_repository.upsert_diary_page(user_id, page_value, merged_content)
    )

    total_lines = len(merged_content.splitlines())
    updated_total_pages = max(max_page, page_value)
    response = {
        "user_id": user_id,
        "action": action_value,
        "page": page_value,
        "total_pages": updated_total_pages,
        "total_lines": total_lines,
        "length": len(merged_content),
        "truncated": truncated,
    }
    if truncated:
        warnings.append(
            f"Diary exceeded {MAX_USER_DIARY_PAGE_CHARS} chars, truncated oldest content"
        )
    if warnings:
        response["warning"] = "; ".join(warnings)
    return response


__all__ = [
    "get_help_text_tool",
    "fetch_group_context_tool",
    "fetch_permanent_summaries_tool",
    "search_permanent_records_tool",
    "user_diary_tool",
]
