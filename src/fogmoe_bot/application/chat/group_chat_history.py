"""Utilities for persisting and retrieving group chat context."""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy.exc import OperationalError

from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import conversation_repository

_bot_user_id: Optional[int] = None
_bot_display_name: str = "FogMoeBot"


def set_bot_identity(user_id: int, display_name: Optional[str] = None) -> None:
    """Register the bot's Telegram user id for downstream lookups."""
    global _bot_user_id, _bot_display_name
    _bot_user_id = user_id
    if display_name:
        _bot_display_name = display_name


async def log_group_message(message, group_id: int) -> None:
    """Persist a group chat message asynchronously."""
    if not group_id or not message:
        return

    user_id = getattr(message.from_user, "id", None)
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return

    message_type, content = _extract_message_payload(message)
    created_at = message.date or datetime.utcnow().replace(tzinfo=timezone.utc)

    record = (group_id, message_id, user_id, message_type, content, created_at)
    await _log_group_message(record)


def _encode_non_text(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode_non_text(value: str) -> str:
    try:
        return base64.b64decode(value.encode("ascii")).decode("utf-8")
    except Exception:
        return value


def _extract_message_payload(message) -> Tuple[str, str]:
    if getattr(message, "text", None):
        return "text", message.text

    if getattr(message, "caption", None):
        if message.photo:
            return "photo", _encode_non_text(message.caption)
        if message.video or message.animation:
            return "video", _encode_non_text(message.caption)
        if message.document:
            return "document", _encode_non_text(message.caption)
        return "other", _encode_non_text(message.caption)

    if getattr(message, "photo", None):
        return "photo", _encode_non_text("[photo]")
    if getattr(message, "sticker", None):
        emoji = getattr(message.sticker, "emoji", None)
        label = emoji or "[sticker]"
        return "sticker", _encode_non_text(label)
    if getattr(message, "voice", None):
        return "voice", _encode_non_text("[voice message]")
    if getattr(message, "video", None) or getattr(message, "animation", None):
        return "video", _encode_non_text("[video message]")
    if getattr(message, "document", None):
        file_name = getattr(message.document, "file_name", None)
        label = file_name or "[document]"
        return "document", _encode_non_text(label)

    return "other", _encode_non_text("[unsupported message]")


async def _log_group_message(record: Tuple[int, int, int, str, str, datetime]) -> None:
    group_id, message_id, user_id, message_type, content, created_at = record

    content = content or ""

    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)

    try:
        async with db_connection.transaction() as connection:
            await conversation_repository.insert_group_message(
                group_id,
                message_id,
                user_id,
                message_type,
                content,
                created_at,
                connection=connection,
            )
    except Exception as exc:
        logging.error("Failed to log group message: %s", exc)
        raise

    # Cleanup is best-effort; avoid failing the message insert on deadlocks.
    try:
        await _cleanup_group_history(group_id)
    except OperationalError as exc:
        if _is_lock_error(exc):
            logging.warning("Skipping chat record cleanup due to lock error: %s", exc)
            return
        logging.error("Failed to cleanup group history: %s", exc)
        raise
    except Exception as exc:
        logging.error("Failed to cleanup group history: %s", exc)
        raise


def _is_lock_error(exc: OperationalError) -> bool:
    code = getattr(getattr(exc, "orig", None), "args", [None])[0]
    return code in {1205, 1213}


async def _cleanup_group_history(group_id: int, retries: int = 3) -> None:
    for attempt in range(retries):
        try:
            async with db_connection.transaction() as connection:
                await conversation_repository.prune_group_history(
                    group_id,
                    connection=connection,
                )
            return
        except OperationalError as exc:
            if _is_lock_error(exc) and attempt < retries - 1:
                await asyncio.sleep(0.05 * (2**attempt))
                continue
            raise


def get_group_context(
    group_id: int,
    around_message_id: Optional[int] = None,
    window_size: int = 5,
) -> List[Dict[str, object]]:
    if not group_id:
        return []
    return db_connection.run_sync(
        async_get_group_context(group_id, around_message_id, window_size)
    )


async def async_get_group_context(
    group_id: int,
    around_message_id: Optional[int] = None,
    window_size: int = 5,
) -> List[Dict[str, object]]:
    if not group_id:
        return []
    return await _get_group_context(group_id, around_message_id, window_size)


async def _get_group_context(
    group_id: int,
    around_message_id: Optional[int],
    window_size: int,
) -> List[Dict[str, object]]:
    async with db_connection.connect() as connection:
        try:
            records = await conversation_repository.fetch_group_context_rows(
                group_id,
                around_message_id,
                window_size,
                connection=connection,
            )

            return [
                {
                    "message_id": row["message_id"],
                    "user_id": row["user_id"],
                    "message_type": row["message_type"],
                    "username": (
                        _bot_display_name
                        if _bot_user_id is not None and row["user_id"] == _bot_user_id
                        else row.get("username")
                    ),
                    "content": (
                        row.get("content", "")
                        if row["message_type"] == "text"
                        else _decode_non_text(row.get("content", ""))
                    ),
                    "created_at": row["created_at"].isoformat(sep=" ") if row.get("created_at") else None,
                }
                for row in records
            ]
        except Exception as exc:
            logging.error("Failed to fetch group context: %s", exc)
            return []
