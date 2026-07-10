import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import kindness_repository, user_repository

from .context import get_tool_request_context

AFFECTION_TOOL_ENABLED = False


def _get_last_kindness_for_recipient(recipient_id: int) -> Optional[dict[str, object]]:
    row = db_connection.run_sync(
        kindness_repository.fetch_latest_gift_for_recipient(recipient_id)
    )
    if not row:
        return None
    return {"amount": row[0], "created_at": row[1]}


def kindness_gift_tool(
    amount: Optional[int] = None,
    **kwargs,
) -> dict:
    context = get_tool_request_context()
    try:
        recipient_id = int(context.get("user_id"))
    except (TypeError, ValueError):
        return {"error": "Missing recipient information, cannot execute gift"}

    recipient = db_connection.run_sync(user_repository.fetch_user_account(recipient_id))
    if not recipient:
        return {"error": "Recipient user not found"}

    last_record = _get_last_kindness_for_recipient(recipient.user_id)
    if last_record and last_record.get("created_at"):
        last_time = last_record["created_at"]
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last_time < timedelta(hours=24):
            return {
                "status": "cooldown",
                "last_amount": last_record["amount"],
                "last_time": last_time.isoformat(sep=" "),
                "message": "Failed: 24-hour cooldown period has not elapsed. Cannot gift coins again yet",
            }

    try:
        amt = int(amount) if amount is not None else random.randint(1, 10)
    except (TypeError, ValueError):
        amt = random.randint(1, 10)
    amt = max(1, min(amt, 10))

    try:
        async def _record_gift():
            async with db_connection.transaction() as connection:
                await user_repository.add_free_coins(
                    recipient.user_id,
                    amt,
                    connection=connection,
                )
                await kindness_repository.insert_gift(
                    recipient.user_id,
                    amt,
                    connection=connection,
                )

        db_connection.run_sync(_record_gift())
    except Exception as exc:
        logging.error("Failed to record kindness gift: %s", exc)
        return {"error": "Error recording gift, please try again later"}

    latest = _get_last_kindness_for_recipient(recipient.user_id)
    last_time_str = None
    last_amount = None
    if latest and latest.get("created_at"):
        last_time_value = latest["created_at"]
        if last_time_value.tzinfo is None:
            last_time_value = last_time_value.replace(tzinfo=timezone.utc)
        last_time_str = last_time_value.isoformat(sep=" ")
        last_amount = latest.get("amount")

    return {
        "status": "granted",
        "recipient_id": recipient.user_id,
        "recipient_username": f"@{recipient.name}" if recipient.name else None,
        "amount": amt,
        "last_time": last_time_str,
        "last_amount": last_amount,
        "recipient_coins_before": recipient.total_coins,
        "recipient_coins_after": recipient.total_coins + amt,
        "message": f"Successfully gifted {amt} coins to user",
    }


def update_affection_tool(delta: int, **kwargs) -> dict:
    """Adjust the AI's affection towards the current user."""
    if not AFFECTION_TOOL_ENABLED:
        return {"error": "Affection tool is temporarily disabled"}

    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {"error": "Missing user information, cannot update affection level"}

    try:
        change = int(delta)
    except (TypeError, ValueError):
        return {"error": "Affection change value must be an integer"}

    if change > 10:
        change = 10
    elif change < -10:
        change = -10

    try:
        affection = db_connection.run_sync(
            user_repository.fetch_affection(user_id)
        )
    except Exception as exc:
        logging.exception("Failed to fetch affection: %s", exc)
        return {"error": "Error querying affection level, please try again later"}

    affection = int(affection or 0)

    if (affection >= 100 and change > 0) or (affection <= -100 and change < 0):
        return {"error": "Affection level has reached the limit, cannot adjust further"}

    try:
        async def _update_affection() -> int:
            async with db_connection.transaction() as connection:
                current = await user_repository.fetch_affection(
                    user_id,
                    connection=connection,
                    for_update=True,
                )
                new_value = max(-100, min(100, int(current or 0) + change))
                await user_repository.upsert_affection(
                    user_id,
                    new_value,
                    connection=connection,
                )
                return new_value

        new_affection = db_connection.run_sync(_update_affection())
    except Exception as exc:
        logging.exception("Failed to update affection: %s", exc)
        return {"error": "Error updating affection level, please try again later"}

    return {
        "user_id": user_id,
        "change": change,
        "affection": new_affection,
        "message": f"Affection level adjusted by {change:+d}, current value: {new_affection}",
    }


def update_impression_tool(impression: str, **kwargs) -> dict:
    """Write or overwrite the AI's impression of the current user."""
    context = get_tool_request_context()
    user_id = context.get("user_id")
    if not user_id:
        return {
            "user_id": None,
            "error": "Missing user information, cannot update impression",
        }

    text = (impression or "").strip()
    if not text:
        return {"user_id": user_id, "error": "Impression text must not be empty"}
    if len(text) > 500:
        text = text[:500]

    try:
        async def _update_impression() -> str:
            async with db_connection.transaction() as connection:
                await user_repository.upsert_impression(
                    user_id,
                    text,
                    connection=connection,
                )
            return text

        saved = db_connection.run_sync(_update_impression())
    except Exception as exc:
        logging.exception("Failed to update impression: %s", exc)
        return {"user_id": user_id, "error": "Error updating impression"}

    return {
        "user_id": user_id,
        "impression": saved,
        "message": "Impression record updated successfully",
    }


__all__ = [
    "kindness_gift_tool",
    "update_impression_tool",
]
