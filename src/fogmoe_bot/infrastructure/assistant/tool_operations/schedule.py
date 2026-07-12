"""Assistant schedule read/mutation operations / Assistant 定时任务读写 operations."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.domain.scheduling import Recurrence, RecurrenceUnit, ensure_utc
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.repositories import schedule_repository

from .parsing import (
    bounded_int,
    iso_instant,
    optional_text,
    required_connection,
    required_text,
)


_MAX_PENDING_SCHEDULES = 3
_MAX_TOTAL_SCHEDULES = 12


async def execute_schedule(
    request: ToolEffectRequest,
    *,
    connection: AsyncConnection | None,
) -> JsonValue:
    """读取 schedule，或在 receipt transaction 中 create/cancel。"""

    action = str(request.arguments.get("action", "create"))
    if action == "list":
        snapshots = await schedule_repository.list_for_user(
            request.context.user_id,
            limit=_MAX_TOTAL_SCHEDULES,
        )
        return {
            "status": "ok",
            "tasks": [
                {
                    "schedule_id": snapshot.job.schedule_id,
                    "timestamp_utc": iso_instant(snapshot.job.run_at),
                    "status": snapshot.status.value,
                    "instruction": snapshot.job.payload.instruction,
                }
                for snapshot in snapshots
            ],
        }

    transaction = required_connection(connection)
    await db_connection.fetch_one(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"assistant-schedule:{request.context.user_id}",),
        connection=transaction,
    )
    if action == "cancel":
        schedule_id = bounded_int(request.arguments, "schedule_id", minimum=1)
        rowcount = await db_connection.execute(
            "UPDATE assistant.ai_schedules SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND user_id = %s AND status = 'pending'",
            (schedule_id, request.context.user_id),
            connection=transaction,
        )
        return (
            {"status": "cancelled", "schedule_id": schedule_id}
            if rowcount == 1
            else {"error": "Schedule not found or not pending"}
        )
    return await _create_schedule(request, connection=transaction)


async def _create_schedule(
    request: ToolEffectRequest,
    *,
    connection: AsyncConnection,
) -> JsonValue:
    """在 caller UoW 创建或替换一个 schedule。"""

    reason = required_text(request.arguments, "trigger_reason")
    instruction = required_text(request.arguments, "instruction")
    unit = RecurrenceUnit(str(request.arguments.get("recurrence_unit", "none")))
    interval = bounded_int(request.arguments, "recurrence_interval", minimum=1)
    recurrence = Recurrence(unit, interval)
    raw_time = optional_text(request.arguments, "timestamp_utc")
    run_at = _parse_utc(raw_time) if raw_time is not None else None
    if run_at is None:
        duration = recurrence.duration()
        if duration is None:
            return {"error": "timestamp_utc is required for one-time schedules"}
        run_at = datetime.now(UTC) + duration
    pending = await schedule_repository.count_pending_for_user(
        request.context.user_id,
        connection=connection,
    )
    if pending >= _MAX_PENDING_SCHEDULES:
        return {"error": f"Too many pending schedules (max {_MAX_PENDING_SCHEDULES})"}
    total = await schedule_repository.count_total_for_user(
        request.context.user_id,
        connection=connection,
    )
    replaced = False
    if total >= _MAX_TOTAL_SCHEDULES:
        schedule_id = await schedule_repository.fetch_oldest_non_pending_id(
            request.context.user_id,
            connection=connection,
        )
        if schedule_id is None:
            return {"error": f"Too many schedules (max {_MAX_TOTAL_SCHEDULES})"}
        await schedule_repository.replace_schedule(
            schedule_id,
            run_at=run_at,
            recurrence_unit=unit.value,
            recurrence_interval=interval,
            trigger_reason=reason,
            context_text=optional_text(request.arguments, "context"),
            instruction_text=instruction,
            connection=connection,
        )
        replaced = True
    else:
        schedule_id = await schedule_repository.insert_schedule(
            user_id=request.context.user_id,
            run_at=run_at,
            recurrence_unit=unit.value,
            recurrence_interval=interval,
            trigger_reason=reason,
            context_text=optional_text(request.arguments, "context"),
            instruction_text=instruction,
            connection=connection,
        )
    if schedule_id is None:
        raise RuntimeError("Schedule insert returned no ID")
    return {
        "status": "scheduled",
        "schedule_id": schedule_id,
        "timestamp_utc": iso_instant(run_at),
        "recurrence_unit": unit.value,
        "recurrence_interval": interval,
        "replaced_oldest": replaced,
    }


def _parse_utc(value: str) -> datetime | None:
    """解析 ISO UTC；非法文本返回 None / Parse ISO UTC or return None."""

    raw = value.strip().replace("Z", "+00:00")
    try:
        return ensure_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None
