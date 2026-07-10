from datetime import datetime, timezone
from typing import Optional

from fogmoe_bot.domain.scheduling import (
    Recurrence,
    RecurrenceUnit,
    ScheduleCreationBlockReason,
    ScheduleStatus,
    ensure_utc,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import schedule_repository

from .context import get_tool_request_context

MAX_PENDING_SCHEDULES = 3
MAX_TOTAL_SCHEDULES = 12
RECURRENCE_ALIASES = {
    "": RecurrenceUnit.NONE,
    "no": RecurrenceUnit.NONE,
    "once": RecurrenceUnit.NONE,
    "one_time": RecurrenceUnit.NONE,
    "one-time": RecurrenceUnit.NONE,
    "none": RecurrenceUnit.NONE,
    "minute": RecurrenceUnit.MINUTE,
    "minutes": RecurrenceUnit.MINUTE,
    "mins": RecurrenceUnit.MINUTE,
    "min": RecurrenceUnit.MINUTE,
    "hour": RecurrenceUnit.HOUR,
    "hours": RecurrenceUnit.HOUR,
    "hourly": RecurrenceUnit.HOUR,
    "day": RecurrenceUnit.DAY,
    "days": RecurrenceUnit.DAY,
    "daily": RecurrenceUnit.DAY,
}


def _normalise_recurrence_unit(value: Optional[str]) -> RecurrenceUnit | None:
    """@brief 将 Agent 别名归一为领域枚举 / Normalize Agent aliases to a domain enum."""

    return RECURRENCE_ALIASES.get((value or "none").strip().lower())


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

    return ensure_utc(dt)


def _format_timestamp_utc(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


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

        snapshots = db_connection.run_sync(
            schedule_repository.list_for_user(user_id, limit=MAX_TOTAL_SCHEDULES)
        )
        tasks = []
        pending_count = 0
        for snapshot in snapshots:
            job = snapshot.job
            payload = job.payload
            if snapshot.status is ScheduleStatus.PENDING:
                pending_count += 1
            task = {
                "schedule_id": job.schedule_id,
                "timestamp_utc": _format_timestamp_utc(job.run_at),
                "recurrence_unit": job.recurrence.unit.value,
                "recurrence_interval": job.recurrence.interval,
                "created_at": _format_timestamp_utc(job.created_at),
                "status": snapshot.status.value,
                "trigger_reason": payload.trigger_reason,
                "context": payload.context_text,
                "instruction": payload.instruction,
            }
            if snapshot.executed_at:
                task["executed_at"] = _format_timestamp_utc(snapshot.executed_at)
            if snapshot.last_run_at:
                task["last_run_at"] = _format_timestamp_utc(snapshot.last_run_at)
            if snapshot.error:
                task["error"] = snapshot.error
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
            schedule_repository.cancel_pending_for_user(schedule_id_value, user_id)
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
    if recurrence_unit_value is None:
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
    try:
        recurrence = Recurrence(recurrence_unit_value, recurrence_interval_value)
    except ValueError as exc:
        return {"user_id": user_id, "error": str(exc)}

    if not timestamp_utc and recurrence.unit is RecurrenceUnit.NONE:
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
    elif recurrence.unit is not RecurrenceUnit.NONE:
        duration = recurrence.duration()
        if duration is None:
            return {"user_id": user_id, "error": "Invalid recurrence"}
        run_at = datetime.now(timezone.utc) + duration
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

    creation = db_connection.run_sync(
        schedule_repository.create_or_replace_for_user(
            user_id=user_id,
            run_at=run_at,
            trigger_reason=trigger_reason_value,
            context_text=context_value,
            instruction_text=instruction_value,
            recurrence_unit=recurrence.unit.value,
            recurrence_interval=recurrence.interval,
            max_pending=MAX_PENDING_SCHEDULES,
            max_total=MAX_TOTAL_SCHEDULES,
        )
    )
    if creation.schedule_id is None:
        if creation.blocked_reason is ScheduleCreationBlockReason.TOTAL_LIMIT:
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
        "schedule_id": creation.schedule_id,
        "timestamp_utc": _format_timestamp_utc(run_at),
        "recurrence_unit": recurrence.unit.value,
        "recurrence_interval": recurrence.interval,
        "created_at": _format_timestamp_utc(creation.created_at),
        "trigger_reason": trigger_reason_value,
        "replaced_oldest": creation.replaced,
        "instruction": instruction_value,
    }
    if context_value:
        response["context"] = context_value
    return response


__all__ = ["schedule_ai_message_tool"]
