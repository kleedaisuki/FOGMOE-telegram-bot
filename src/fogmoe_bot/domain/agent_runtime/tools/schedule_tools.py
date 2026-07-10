from datetime import datetime, timedelta, timezone
from typing import Optional

from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import ai_schedule_repository

from .context import get_tool_request_context

MAX_PENDING_SCHEDULES = 3
MAX_TOTAL_SCHEDULES = 12
RECURRENCE_UNITS = {"none", "minute", "hour", "day"}


def _normalise_recurrence_unit(value: Optional[str]) -> str:
    raw = (value or "none").strip().lower()
    aliases = {
        "": "none",
        "no": "none",
        "once": "none",
        "one_time": "none",
        "one-time": "none",
        "minutes": "minute",
        "mins": "minute",
        "min": "minute",
        "hours": "hour",
        "hourly": "hour",
        "days": "day",
        "daily": "day",
    }
    return aliases.get(raw, raw)


def _recurrence_delta(unit: str, interval: int) -> Optional[timedelta]:
    if unit == "minute":
        return timedelta(minutes=interval)
    if unit == "hour":
        return timedelta(hours=interval)
    if unit == "day":
        return timedelta(days=interval)
    return None


def _default_first_run_at(unit: str, interval: int) -> Optional[datetime]:
    delta = _recurrence_delta(unit, interval)
    if delta is None:
        return None
    return datetime.utcnow() + delta


def _parse_timestamp_utc(value: str | None) -> Optional[datetime]:
    if not value:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _format_timestamp_utc(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _create_or_replace_schedule(
    user_id: int,
    run_at: datetime,
    trigger_reason: str,
    context_text: Optional[str],
    instruction_text: str,
    recurrence_unit: str,
    recurrence_interval: int,
) -> tuple[Optional[int], Optional[datetime], bool, Optional[str]]:
    result = await ai_schedule_repository.create_or_replace_for_user(
        user_id=user_id,
        run_at=run_at,
        trigger_reason=trigger_reason,
        context_text=context_text,
        instruction_text=instruction_text,
        recurrence_unit=recurrence_unit,
        recurrence_interval=recurrence_interval,
        max_pending=MAX_PENDING_SCHEDULES,
        max_total=MAX_TOTAL_SCHEDULES,
    )
    return result.schedule_id, result.created_at, result.replaced, result.blocked_reason


def schedule_ai_message_tool(
    action: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
    recurrence_unit: Optional[str] = None,
    recurrence_interval: Optional[int] = None,
    trigger_reason: Optional[str] = None,
    context: Optional[str] = None,
    instruction: Optional[str] = None,
    schedule_id: Optional[int] = None,
    **kwargs,
) -> dict:
    request_context = get_tool_request_context()
    user_id = request_context.get("user_id")
    if not user_id:
        return {"user_id": None, "error": "Missing user information, cannot schedule message"}

    action_value = (action or "create").strip().lower()
    if action_value in {"create", "new", "add"}:
        action_value = "create"
    elif action_value in {"list", "ls", "show"}:
        action_value = "list"
    elif action_value in {"cancel", "delete", "remove"}:
        action_value = "cancel"
    else:
        return {"user_id": user_id, "error": f"Unknown action: {action}"}

    warnings: list[str] = []

    if action_value == "list":
        if any(
            [
                timestamp_utc,
                recurrence_unit,
                recurrence_interval,
                trigger_reason,
                context,
                instruction,
                schedule_id,
            ]
        ):
            warnings.append("extra fields ignored for list action")

        rows = db_connection.run_sync(
            ai_schedule_repository.list_for_user(user_id, limit=MAX_TOTAL_SCHEDULES)
        )
        tasks = []
        pending_count = 0
        for row in rows:
            (
                task_id,
                run_at,
                task_recurrence_unit,
                task_recurrence_interval,
                created_at,
                executed_at,
                last_run_at,
                status,
                reason,
                context_text,
                instruction_text,
                error_text,
            ) = row
            if status == "pending":
                pending_count += 1
            task = {
                "schedule_id": task_id,
                "timestamp_utc": _format_timestamp_utc(run_at),
                "recurrence_unit": task_recurrence_unit or "none",
                "recurrence_interval": task_recurrence_interval or 1,
                "created_at": _format_timestamp_utc(created_at),
                "status": status,
                "trigger_reason": reason,
                "context": context_text,
                "instruction": instruction_text,
            }
            if executed_at:
                task["executed_at"] = _format_timestamp_utc(executed_at)
            if last_run_at:
                task["last_run_at"] = _format_timestamp_utc(last_run_at)
            if error_text:
                task["error"] = error_text
            tasks.append(task)

        response = {
            "status": "ok",
            "total": len(tasks),
            "pending_count": pending_count,
            "tasks": tasks,
        }
        if warnings:
            response["warning"] = "; ".join(warnings)
        return response

    if action_value == "cancel":
        if schedule_id is None:
            return {"user_id": user_id, "error": "Missing schedule_id for cancel action"}
        try:
            schedule_id_value = int(schedule_id)
        except (TypeError, ValueError):
            return {"user_id": user_id, "error": "Invalid schedule_id"}

        if any([timestamp_utc, recurrence_unit, recurrence_interval, trigger_reason, context, instruction]):
            warnings.append("extra fields ignored for cancel action")

        cancelled = db_connection.run_sync(
            ai_schedule_repository.cancel_pending_for_user(schedule_id_value, user_id)
        )
        if not cancelled:
            return {
                "user_id": user_id,
                "error": "Schedule not found or not pending",
            }

        response = {
            "status": "cancelled",
            "schedule_id": schedule_id_value,
        }
        if warnings:
            response["warning"] = "; ".join(warnings)
        return response

    recurrence_unit_value = _normalise_recurrence_unit(recurrence_unit)
    if recurrence_unit_value not in RECURRENCE_UNITS:
        return {
            "user_id": user_id,
            "error": "Invalid recurrence_unit; expected none, minute, hour, or day",
        }

    try:
        recurrence_interval_value = (
            int(recurrence_interval) if recurrence_interval is not None else 1
        )
    except (TypeError, ValueError):
        return {"user_id": user_id, "error": "Invalid recurrence_interval"}
    if recurrence_interval_value < 1:
        return {"user_id": user_id, "error": "recurrence_interval must be at least 1"}
    if recurrence_unit_value == "none":
        recurrence_interval_value = 1

    if not timestamp_utc and recurrence_unit_value == "none":
        return {"user_id": user_id, "error": "Missing timestamp_utc for create action"}
    if not trigger_reason:
        return {"user_id": user_id, "error": "Missing trigger_reason for create action"}
    instruction_value = instruction
    if not instruction_value:
        return {"user_id": user_id, "error": "Missing instruction for create action"}

    if timestamp_utc:
        run_at = _parse_timestamp_utc(timestamp_utc)
        if run_at is None:
            return {"user_id": user_id, "error": "Invalid timestamp_utc format"}
    elif recurrence_unit_value != "none":
        run_at = _default_first_run_at(recurrence_unit_value, recurrence_interval_value)
    else:
        return {"user_id": user_id, "error": "Missing timestamp_utc for create action"}

    trigger_reason_value = str(trigger_reason).strip()
    if not trigger_reason_value:
        return {"user_id": user_id, "error": "Empty trigger_reason is not allowed"}
    if len(trigger_reason_value) > 200:
        return {"user_id": user_id, "error": "trigger_reason exceeds 200 characters"}

    instruction_value = str(instruction_value).strip()
    if not instruction_value:
        return {"user_id": user_id, "error": "Empty instruction is not allowed"}
    if len(instruction_value) > 2000:
        return {"user_id": user_id, "error": "instruction exceeds 2000 characters"}

    context_value = None
    if context is not None:
        context_value = str(context).strip()
        if not context_value:
            context_value = None
        elif len(context_value) > 1000:
            return {"user_id": user_id, "error": "context exceeds 1000 characters"}

    schedule_id, created_at, replaced, blocked_reason = db_connection.run_sync(
        _create_or_replace_schedule(
            user_id,
            run_at,
            trigger_reason_value,
            context_value,
            instruction_value,
            recurrence_unit_value,
            recurrence_interval_value,
        )
    )
    if schedule_id is None:
        if blocked_reason == "total_limit":
            return {
                "user_id": user_id,
                "error": (
                    f"Too many schedules (max {MAX_TOTAL_SCHEDULES}). "
                    "No non-pending schedule available to overwrite."
                ),
            }
        return {
            "user_id": user_id,
            "error": (
                f"Too many pending schedules (max {MAX_PENDING_SCHEDULES}). "
                "Cancel or wait for execution before creating new ones."
            ),
        }

    response = {
        "status": "scheduled",
        "schedule_id": schedule_id,
        "timestamp_utc": _format_timestamp_utc(run_at),
        "recurrence_unit": recurrence_unit_value,
        "recurrence_interval": recurrence_interval_value,
        "created_at": _format_timestamp_utc(created_at),
        "trigger_reason": trigger_reason_value,
        "replaced_oldest": replaced,
        "instruction": instruction_value,
    }
    if context_value:
        response["context"] = context_value
    return response


__all__ = ["schedule_ai_message_tool"]
