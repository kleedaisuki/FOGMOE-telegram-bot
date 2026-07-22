"""@brief Assistant schedule 工具适配器 / Assistant schedule tool adapter."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.assistant.tools.catalog import (
    CalendarDailyScheduleArgs,
    CalendarWeeklyScheduleArgs,
    FixedIntervalScheduleArgs,
    OneShotScheduleArgs,
    ScheduleAIMessageArgs,
)
from fogmoe_bot.application.scheduling.assistant_ports import ScheduleDefinition
from fogmoe_bot.application.scheduling.service import SchedulingService
from fogmoe_bot.application.timekeeping.service import TimeService
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    CalendarDaily,
    CalendarWeekly,
    Cadence,
    FixedInterval,
    MisfirePolicy,
    OneShot,
    ScheduleSnapshot,
    ScheduleTarget,
    ScheduledAssistantTurn,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.scheduling.postgres import PostgresScheduleCatalog

from .parsing import required_connection


async def execute_schedule(
    request: ToolEffectRequest,
    *,
    connection: AsyncConnection | None,
    scheduling: SchedulingService,
    time: TimeService,
) -> JsonValue:
    """@brief 在当前授权 Conversation 中执行 schedule 用例 / Execute a schedule use case in the authorized Conversation.

    @param request 已由权威工具目录校验的请求 / Request validated by the authoritative tool catalog.
    @param connection mutation receipt 的活动事务 / Active mutation-receipt transaction.
    @param scheduling 调度应用服务 / Scheduling application service.
    @param time 统一时间解释服务 / Unified temporal interpretation service.
    @return 可安全回填模型的 JSON / JSON safe to feed back to the model.
    @note 目标完全来自 ToolExecutionContext，模型不能指定 chat 或 Conversation。/
        The target comes solely from ToolExecutionContext; the model cannot choose a chat or Conversation.
    """

    arguments = ScheduleAIMessageArgs.model_validate(request.arguments)
    if arguments.action == "list":
        async with db_connection.transaction() as read_connection:
            snapshots = await scheduling.list(
                creator_user_id=request.context.user_id,
                conversation_id=str(request.context.conversation_id),
                limit=arguments.limit,
                catalog=PostgresScheduleCatalog(read_connection),
            )
        return {
            "status": "ok",
            "schedules": [_snapshot_result(item) for item in snapshots],
        }

    transaction = required_connection(connection)
    catalog = PostgresScheduleCatalog(transaction)
    if arguments.action == "cancel":
        schedule_id = cast(int, arguments.schedule_id)
        cancelled = await scheduling.cancel(
            schedule_id=schedule_id,
            creator_user_id=request.context.user_id,
            conversation_id=str(request.context.conversation_id),
            catalog=catalog,
        )
        return (
            {"status": "cancelled", "schedule_id": schedule_id}
            if cancelled
            else {
                "error": "Schedule not found in this conversation or already terminal",
                "schedule_id": schedule_id,
            }
        )

    try:
        definition = _definition(arguments, request=request, time=time)
        if arguments.action == "create":
            schedule = await scheduling.create(definition, catalog=catalog)
            status = "scheduled"
        elif arguments.action == "update":
            schedule_id = cast(int, arguments.schedule_id)
            replacement = await scheduling.replace(
                schedule_id,
                definition,
                catalog=catalog,
            )
            if replacement is None:
                return {
                    "error": (
                        "Schedule not found in this conversation, terminal, or currently processing"
                    ),
                    "schedule_id": schedule_id,
                }
            schedule = replacement
            status = "updated"
        else:  # pragma: no cover - catalog validation and earlier branches are exhaustive
            raise AssertionError(f"Unhandled schedule action: {arguments.action}")
    except (TypeError, ValueError) as error:
        return {"error": str(error)}
    return {"status": status, "schedule": _schedule_result(schedule)}


def _definition(
    arguments: ScheduleAIMessageArgs,
    *,
    request: ToolEffectRequest,
    time: TimeService,
) -> ScheduleDefinition:
    """@brief 从工具参数构造完整应用定义 / Build a complete application definition from tool arguments.

    @param arguments 已验证工具参数 / Validated tool arguments.
    @param request 当前授权上下文 / Current authorization context.
    @param time 时间解析服务 / Temporal parsing service.
    @return 完整 schedule 定义 / Complete schedule definition.
    """

    cadence_arguments = arguments.cadence
    if (
        cadence_arguments is None
        or arguments.trigger_reason is None
        or arguments.instruction is None
    ):
        raise ValueError("Schedule definition is incomplete")
    zone = time.time_zone(arguments.timezone)
    first_run_at = time.resolve(
        cadence_arguments.first_at,
        time_zone=zone.value,
    )
    cadence = _cadence(
        cadence_arguments, first_run_at=first_run_at, time=time, zone_name=zone.value
    )
    try:
        chat_id = int(request.context.chat_id)
    except (TypeError, ValueError) as error:
        raise ValueError("Current chat_id is not an integer") from error
    target = ScheduleTarget(
        conversation_id=request.context.conversation_id,
        delivery_stream_id=request.context.delivery_stream_id,
        chat_id=chat_id,
        is_group=request.context.is_group,
        message_thread_id=request.context.message_thread_id,
    )
    grace = (
        None
        if arguments.misfire_grace_seconds is None
        else timedelta(seconds=arguments.misfire_grace_seconds)
    )
    return ScheduleDefinition(
        creator_user_id=request.context.user_id,
        target=target,
        trigger_reason=arguments.trigger_reason,
        instruction=arguments.instruction,
        cadence=cadence,
        first_run_at=first_run_at,
        time_zone=zone,
        context_snapshot=arguments.context,
        misfire_policy=MisfirePolicy(arguments.misfire_policy),
        misfire_grace=grace,
    )


def _cadence(
    arguments: OneShotScheduleArgs
    | FixedIntervalScheduleArgs
    | CalendarDailyScheduleArgs
    | CalendarWeeklyScheduleArgs,
    *,
    first_run_at: datetime,
    time: TimeService,
    zone_name: str,
) -> Cadence:
    """@brief 将显式工具 cadence 映射为领域值 / Map an explicit tool cadence to a domain value.

    @param arguments cadence 参数联合 / Cadence argument union.
    @param first_run_at 首次唯一 UTC 瞬间 / First unique UTC instant.
    @param time 时间服务 / Time service.
    @param zone_name 已验证 IANA 时区 / Validated IANA zone name.
    @return 领域 cadence / Domain cadence.
    """

    if isinstance(arguments, OneShotScheduleArgs):
        return OneShot()
    if isinstance(arguments, FixedIntervalScheduleArgs):
        return FixedInterval(timedelta(seconds=arguments.every_seconds))
    zone = time.time_zone(zone_name)
    local = zone.localize(first_run_at)
    local_time = local.replace(tzinfo=None).time()
    if isinstance(arguments, CalendarDailyScheduleArgs):
        return CalendarDaily(
            local_time=local_time,
            time_zone=zone,
            interval=arguments.every_days,
        )
    weekdays = frozenset(arguments.weekdays)
    if local.isoweekday() not in weekdays:
        raise ValueError("calendar_weekly first_at must fall on one configured weekday")
    return CalendarWeekly(
        local_time=local_time,
        time_zone=zone,
        weekdays=weekdays,
        interval=arguments.every_weeks,
    )


def _snapshot_result(snapshot: ScheduleSnapshot) -> JsonObject:
    """@brief 序列化列表快照 / Serialize a list snapshot.

    @param snapshot 领域查询快照 / Domain query snapshot.
    @return JSON 对象 / JSON object.
    """

    result = _schedule_result(snapshot.schedule)
    result.update(
        {
            "status": snapshot.status.value,
            "attempt_count": snapshot.attempt_count,
            "last_accepted_for": _optional_instant(snapshot.last_accepted_for),
            "last_accepted_at": _optional_instant(snapshot.last_accepted_at),
            "last_error": snapshot.last_error,
            "terminal_at": _optional_instant(snapshot.terminal_at),
        }
    )
    return result


def _schedule_result(schedule: ScheduledAssistantTurn) -> JsonObject:
    """@brief 序列化 schedule 定义 / Serialize a schedule definition.

    @param schedule 领域 schedule / Domain schedule.
    @return 稳定 JSON 对象 / Stable JSON object.
    """

    local = schedule.time_zone.localize(schedule.next_run_at)
    return {
        "schedule_id": schedule.schedule_id,
        "next_run_at_utc": _instant(schedule.next_run_at),
        "next_run_at_local": local.isoformat(timespec="seconds"),
        "timezone": schedule.time_zone.value,
        "trigger_reason": schedule.trigger_reason,
        "instruction": schedule.instruction,
        "context": schedule.context_snapshot,
        "cadence": _cadence_result(schedule.cadence),
    }


def _cadence_result(cadence: Cadence) -> JsonObject:
    """@brief 序列化 recurrence 变体 / Serialize a recurrence variant.

    @param cadence 领域 cadence / Domain cadence.
    @return 带 kind 判别器的 JSON / JSON carrying a kind discriminator.
    """

    if isinstance(cadence, OneShot):
        return {"kind": "one_shot"}
    if isinstance(cadence, FixedInterval):
        return {
            "kind": "fixed_interval",
            "every_seconds": int(cadence.every.total_seconds()),
        }
    if isinstance(cadence, CalendarDaily):
        return {
            "kind": "calendar_daily",
            "local_time": cadence.local_time.isoformat(),
            "every_days": cadence.interval,
        }
    weekdays: list[JsonValue] = [day for day in sorted(cadence.weekdays)]
    return {
        "kind": "calendar_weekly",
        "local_time": cadence.local_time.isoformat(),
        "weekdays": weekdays,
        "every_weeks": cadence.interval,
    }


def _instant(value: datetime) -> str:
    """@brief 输出 RFC3339 风格 UTC 文本 / Render RFC3339-style UTC text.

    @param value aware 瞬间 / Aware instant.
    @return Z 结尾文本 / Z-suffixed text.
    """

    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _optional_instant(value: datetime | None) -> str | None:
    """@brief 序列化可选瞬间 / Serialize an optional instant.

    @param value 可选 aware 瞬间 / Optional aware instant.
    @return ISO 文本或 None / ISO text or None.
    """

    return None if value is None else _instant(value)


__all__ = ["execute_schedule"]
