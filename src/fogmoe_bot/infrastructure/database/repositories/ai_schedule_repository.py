from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fogmoe_bot.infrastructure.database import mysql_connection


@dataclass(frozen=True)
class ScheduleCreateResult:
    """@brief 定时任务创建结果 / Schedule creation result.

    @param schedule_id 定时任务 ID / Schedule ID.
    @param created_at 创建时间 / Creation timestamp.
    @param replaced 是否替换了旧任务 / Whether an old schedule was replaced.
    @param blocked_reason 阻塞原因 / Reason why creation was blocked.
    """

    schedule_id: int | None
    created_at: datetime | None
    replaced: bool
    blocked_reason: str | None


async def count_pending_for_user(user_id: int, *, connection=None) -> int:
    """@brief 统计用户待执行任务数 / Count a user's pending schedules.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 待执行任务数量 / Pending schedule count.
    """

    row = await mysql_connection.fetch_one(
        "SELECT COUNT(*) FROM ai_schedules WHERE user_id = %s AND status = 'pending'",
        (user_id,),
        connection=connection,
    )
    return int(row[0] or 0) if row else 0


async def count_total_for_user(user_id: int, *, connection=None) -> int:
    """@brief 统计用户任务总数 / Count all schedules for a user.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 任务总数 / Total schedule count.
    """

    row = await mysql_connection.fetch_one(
        "SELECT COUNT(*) FROM ai_schedules WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    return int(row[0] or 0) if row else 0


async def fetch_oldest_non_pending_id(user_id: int, *, connection=None) -> int | None:
    """@brief 读取最旧的非待执行任务 ID / Fetch the oldest non-pending schedule ID.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 任务 ID；不存在时返回 None / Schedule ID, or None when absent.
    """

    row = await mysql_connection.fetch_one(
        "SELECT id FROM ai_schedules WHERE user_id = %s AND status != 'pending' "
        "ORDER BY created_at ASC, id ASC LIMIT 1",
        (user_id,),
        connection=connection,
    )
    return int(row[0]) if row else None


async def replace_schedule(
    schedule_id: int,
    *,
    run_at: datetime,
    recurrence_unit: str,
    recurrence_interval: int,
    trigger_reason: str,
    context_text: str | None,
    instruction_text: str,
    connection=None,
) -> None:
    """@brief 替换已有任务内容 / Replace an existing schedule.

    @param schedule_id 定时任务 ID / Schedule ID.
    @param run_at 执行时间 / Run timestamp.
    @param recurrence_unit 重复单位 / Recurrence unit.
    @param recurrence_interval 重复间隔 / Recurrence interval.
    @param trigger_reason 触发原因 / Trigger reason.
    @param context_text 上下文文本 / Context text.
    @param instruction_text 指令文本 / Instruction text.
    @param connection 可选数据库连接 / Optional database connection.
    @return None / None.
    """

    await mysql_connection.execute(
        "UPDATE ai_schedules "
        "SET run_at = %s, recurrence_unit = %s, recurrence_interval = %s, "
        "trigger_reason = %s, context = %s, prompt = %s, "
        "status = 'pending', created_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP(), "
        "executed_at = NULL, last_run_at = NULL, error = NULL "
        "WHERE id = %s",
        (
            run_at,
            recurrence_unit,
            recurrence_interval,
            trigger_reason,
            context_text,
            instruction_text,
            schedule_id,
        ),
        connection=connection,
    )


async def insert_schedule(
    *,
    user_id: int,
    run_at: datetime,
    recurrence_unit: str,
    recurrence_interval: int,
    trigger_reason: str,
    context_text: str | None,
    instruction_text: str,
    connection=None,
) -> int | None:
    """@brief 插入新任务 / Insert a new schedule.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param run_at 执行时间 / Run timestamp.
    @param recurrence_unit 重复单位 / Recurrence unit.
    @param recurrence_interval 重复间隔 / Recurrence interval.
    @param trigger_reason 触发原因 / Trigger reason.
    @param context_text 上下文文本 / Context text.
    @param instruction_text 指令文本 / Instruction text.
    @param connection 可选数据库连接 / Optional database connection.
    @return 新任务 ID；无法读取时返回 None / New schedule ID, or None if it cannot be read.
    """

    result = await connection.exec_driver_sql(
        "INSERT INTO ai_schedules "
        "(user_id, run_at, recurrence_unit, recurrence_interval, trigger_reason, context, prompt) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            user_id,
            run_at,
            recurrence_unit,
            recurrence_interval,
            trigger_reason,
            context_text,
            instruction_text,
        ),
    )
    lastrowid = getattr(result, "lastrowid", None)
    if lastrowid is not None:
        return int(lastrowid)

    row = await mysql_connection.fetch_one("SELECT LAST_INSERT_ID()", connection=connection)
    return int(row[0]) if row and row[0] is not None else None


async def fetch_created_at(schedule_id: int, *, connection=None) -> datetime | None:
    """@brief 读取任务创建时间 / Fetch schedule creation timestamp.

    @param schedule_id 定时任务 ID / Schedule ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 创建时间；不存在时返回 None / Creation timestamp, or None when absent.
    """

    row = await mysql_connection.fetch_one(
        "SELECT created_at FROM ai_schedules WHERE id = %s",
        (schedule_id,),
        connection=connection,
    )
    return row[0] if row else None


async def create_or_replace_for_user(
    *,
    user_id: int,
    run_at: datetime,
    trigger_reason: str,
    context_text: str | None,
    instruction_text: str,
    recurrence_unit: str,
    recurrence_interval: int,
    max_pending: int,
    max_total: int,
) -> ScheduleCreateResult:
    """@brief 为用户创建或替换定时任务 / Create or replace a schedule for a user.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param run_at 执行时间 / Run timestamp.
    @param trigger_reason 触发原因 / Trigger reason.
    @param context_text 上下文文本 / Context text.
    @param instruction_text 指令文本 / Instruction text.
    @param recurrence_unit 重复单位 / Recurrence unit.
    @param recurrence_interval 重复间隔 / Recurrence interval.
    @param max_pending 最大待执行任务数 / Maximum pending schedules.
    @param max_total 最大任务总数 / Maximum total schedules.
    @return 创建结果 / Creation result.
    """

    replaced = False
    async with mysql_connection.transaction() as connection:
        pending_count = await count_pending_for_user(user_id, connection=connection)
        if pending_count >= max_pending:
            return ScheduleCreateResult(None, None, False, "pending_limit")

        total_count = await count_total_for_user(user_id, connection=connection)
        schedule_id: int | None = None
        if total_count >= max_total:
            schedule_id = await fetch_oldest_non_pending_id(user_id, connection=connection)
            if schedule_id is None:
                return ScheduleCreateResult(None, None, False, "total_limit")
            await replace_schedule(
                schedule_id,
                run_at=run_at,
                recurrence_unit=recurrence_unit,
                recurrence_interval=recurrence_interval,
                trigger_reason=trigger_reason,
                context_text=context_text,
                instruction_text=instruction_text,
                connection=connection,
            )
            replaced = True

        if schedule_id is None:
            schedule_id = await insert_schedule(
                user_id=user_id,
                run_at=run_at,
                recurrence_unit=recurrence_unit,
                recurrence_interval=recurrence_interval,
                trigger_reason=trigger_reason,
                context_text=context_text,
                instruction_text=instruction_text,
                connection=connection,
            )

        created_at = None
        if schedule_id is not None:
            created_at = await fetch_created_at(schedule_id, connection=connection)

    return ScheduleCreateResult(schedule_id, created_at, replaced, None)


async def list_for_user(user_id: int, *, limit: int):
    """@brief 列出用户定时任务 / List schedules for a user.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param limit 最大返回数量 / Maximum rows to return.
    @return 数据库结果行列表 / Database rows.
    """

    return await mysql_connection.fetch_all(
        "SELECT id, run_at, recurrence_unit, recurrence_interval, created_at, "
        "executed_at, last_run_at, status, trigger_reason, context, prompt, error "
        "FROM ai_schedules WHERE user_id = %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        (user_id, limit),
    )


async def cancel_pending_for_user(schedule_id: int, user_id: int) -> bool:
    """@brief 取消用户待执行任务 / Cancel a user's pending schedule.

    @param schedule_id 定时任务 ID / Schedule ID.
    @param user_id Telegram 用户 ID / Telegram user ID.
    @return 取消成功返回 True / True when a row was cancelled.
    """

    rowcount = await mysql_connection.execute(
        "UPDATE ai_schedules SET status = 'cancelled' "
        "WHERE id = %s AND user_id = %s AND status = 'pending'",
        (schedule_id, user_id),
    )
    return rowcount > 0


async def mark_status(schedule_id: int, status: str, *, error: str | None = None) -> None:
    """@brief 标记任务状态 / Mark schedule status.

    @param schedule_id 定时任务 ID / Schedule ID.
    @param status 新状态 / New status.
    @param error 可选错误文本 / Optional error text.
    @return None / None.
    """

    if error is not None:
        await mysql_connection.execute(
            "UPDATE ai_schedules SET status = %s, error = %s WHERE id = %s",
            (status, error, schedule_id),
        )
        return

    if status == "executed":
        await mysql_connection.execute(
            "UPDATE ai_schedules SET status = %s, executed_at = UTC_TIMESTAMP() WHERE id = %s",
            (status, schedule_id),
        )
        return

    await mysql_connection.execute(
        "UPDATE ai_schedules SET status = %s WHERE id = %s",
        (status, schedule_id),
    )


async def reschedule_recurring(schedule_id: int, *, last_run_at: datetime, next_run_at: datetime) -> None:
    """@brief 重排循环任务 / Reschedule a recurring schedule.

    @param schedule_id 定时任务 ID / Schedule ID.
    @param last_run_at 上次执行时间 / Last run timestamp.
    @param next_run_at 下次执行时间 / Next run timestamp.
    @return None / None.
    """

    await mysql_connection.execute(
        "UPDATE ai_schedules "
        "SET status = 'pending', run_at = %s, last_run_at = %s, "
        "executed_at = UTC_TIMESTAMP(), error = NULL "
        "WHERE id = %s",
        (next_run_at, last_run_at, schedule_id),
    )


async def claim_due(limit: int):
    """@brief 领取到期任务 / Claim due schedules.

    @param limit 最大领取数量 / Maximum number of schedules to claim.
    @return 已领取的任务行 / Claimed schedule rows.
    """

    async with mysql_connection.transaction() as connection:
        rows = await mysql_connection.fetch_all(
            "SELECT id, user_id, run_at, created_at, trigger_reason, context, prompt, "
            "recurrence_unit, recurrence_interval "
            "FROM ai_schedules "
            "WHERE status = 'pending' AND run_at <= UTC_TIMESTAMP() "
            "ORDER BY run_at ASC, id ASC "
            "LIMIT %s FOR UPDATE",
            (limit,),
            connection=connection,
        )
        if not rows:
            return []

        schedule_ids = [row[0] for row in rows]
        placeholders = ", ".join(["%s"] * len(schedule_ids))
        await connection.exec_driver_sql(
            f"UPDATE ai_schedules SET status = 'executing' WHERE id IN ({placeholders})",
            tuple(schedule_ids),
        )

    return rows
