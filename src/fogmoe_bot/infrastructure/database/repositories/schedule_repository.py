import uuid
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.scheduling import (
    PROMPT_JOB_KIND,
    PromptJobPayload,
    Recurrence,
    ScheduleClaim,
    ScheduledJob,
    ScheduleSnapshot,
    ScheduleStatus,
    StaleScheduleClaimError,
    ensure_utc,
    to_storage_datetime,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


async def count_pending_for_user(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
) -> int:
    """@brief 统计用户待执行任务数 / Count a user's pending schedules.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 待执行任务数量 / Pending schedule count.
    """

    row = await db_connection.fetch_one(
        "SELECT COUNT(*) FROM ai_schedules WHERE user_id = %s AND status = 'pending'",
        (user_id,),
        connection=connection,
    )
    return int(row[0] or 0) if row else 0


async def count_total_for_user(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
) -> int:
    """@brief 统计用户任务总数 / Count all schedules for a user.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 任务总数 / Total schedule count.
    """

    row = await db_connection.fetch_one(
        "SELECT COUNT(*) FROM ai_schedules WHERE user_id = %s",
        (user_id,),
        connection=connection,
    )
    return int(row[0] or 0) if row else 0


async def fetch_oldest_non_pending_id(
    user_id: int,
    *,
    connection: AsyncConnection | None = None,
) -> int | None:
    """@brief 读取最旧的非待执行任务 ID / Fetch the oldest non-pending schedule ID.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param connection 可选数据库连接 / Optional database connection.
    @return 任务 ID；不存在时返回 None / Schedule ID, or None when absent.
    """

    row = await db_connection.fetch_one(
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
    connection: AsyncConnection | None = None,
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

    await db_connection.execute(
        "UPDATE ai_schedules "
        "SET run_at = %s, recurrence_unit = %s, recurrence_interval = %s, "
        "trigger_reason = %s, context = %s, prompt = %s, "
        "status = 'pending', created_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP, "
        "executed_at = NULL, last_run_at = NULL, error = NULL, "
        "claim_token = NULL, lease_expires_at = NULL "
        "WHERE id = %s",
        (
            to_storage_datetime(run_at),
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
    connection: AsyncConnection | None = None,
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

    row = await db_connection.fetch_one(
        "INSERT INTO ai_schedules "
        "(user_id, run_at, recurrence_unit, recurrence_interval, trigger_reason, context, prompt) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id",
        (
            user_id,
            to_storage_datetime(run_at),
            recurrence_unit,
            recurrence_interval,
            trigger_reason,
            context_text,
            instruction_text,
        ),
        connection=connection,
    )
    return int(row[0]) if row and row[0] is not None else None


async def list_for_user(
    user_id: int,
    *,
    limit: int,
) -> tuple[ScheduleSnapshot[PromptJobPayload], ...]:
    """@brief 列出用户定时任务 / List schedules for a user.

    @param user_id Telegram 用户 ID / Telegram user ID.
    @param limit 最大返回数量 / Maximum rows to return.
    @return 数据库结果行列表 / Database rows.
    """

    rows = await db_connection.fetch_all(
        "SELECT id, run_at, recurrence_unit, recurrence_interval, created_at, "
        "executed_at, last_run_at, status, trigger_reason, context, prompt, error "
        "FROM ai_schedules WHERE user_id = %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        (user_id, limit),
    )
    snapshots = []
    for row in rows:
        (
            schedule_id,
            run_at,
            recurrence_unit,
            recurrence_interval,
            created_at,
            executed_at,
            last_run_at,
            status,
            reason,
            context,
            prompt,
            error,
        ) = row
        job = ScheduleRepository._map_values(
            schedule_id=schedule_id,
            owner_id=user_id,
            run_at=run_at,
            created_at=created_at,
            reason=reason,
            context=context,
            prompt=prompt,
            unit=recurrence_unit,
            interval=recurrence_interval,
        )
        snapshots.append(
            ScheduleSnapshot(
                job=job,
                status=ScheduleStatus(_decode_text(status)),
                executed_at=ensure_utc(executed_at) if executed_at else None,
                last_run_at=ensure_utc(last_run_at) if last_run_at else None,
                error=_decode_optional_text(error),
            )
        )
    return tuple(snapshots)


class ScheduleRepository:
    """@brief 基于 ai_schedules 表的类型化仓储 / Typed repository backed by ai_schedules."""

    async def recover_stale(self, now: datetime) -> int:
        """@brief 回收超时 executing 任务 / Recover stale executing jobs."""

        return await db_connection.execute(
            "UPDATE ai_schedules SET status = 'pending', updated_at = CURRENT_TIMESTAMP, "
            "error = 'recovered stale execution', claim_token = NULL, lease_expires_at = NULL "
            "WHERE status = 'executing' "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= %s)",
            (to_storage_datetime(now),),
        )

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[ScheduleClaim[PromptJobPayload], ...]:
        """@brief 原子领取并映射到领域对象 / Atomically claim and map due jobs."""

        lease_expires_at = now + lease_for
        async with db_connection.transaction() as connection:
            rows = await db_connection.fetch_all(
                "SELECT id, user_id, run_at, created_at, trigger_reason, context, prompt, "
                "recurrence_unit, recurrence_interval FROM ai_schedules "
                "WHERE status = 'pending' AND run_at <= %s "
                "ORDER BY run_at ASC, id ASC LIMIT %s FOR UPDATE SKIP LOCKED",
                (to_storage_datetime(now), limit),
                connection=connection,
            )
            if not rows:
                return ()
            tokens = tuple(uuid.uuid4() for _ in rows)
            for row, token in zip(rows, tokens, strict=True):
                await db_connection.execute(
                    "UPDATE ai_schedules SET status = 'executing', claim_token = %s, "
                    "lease_expires_at = %s, updated_at = CURRENT_TIMESTAMP, error = NULL "
                    "WHERE id = %s AND status = 'pending'",
                    (token, to_storage_datetime(lease_expires_at), int(row[0])),
                    connection=connection,
                )

        return tuple(
            ScheduleClaim(
                job=self._map_row(row),
                token=str(token),
                lease_expires_at=lease_expires_at,
            )
            for row, token in zip(rows, tokens, strict=True)
        )

    async def mark_executed(self, claim: ScheduleClaim[PromptJobPayload]) -> None:
        """@brief 以 claim token 标记一次性任务完成 / Mark a one-shot job executed with its claim token.

        @param claim 当前领取凭证 / Current claim.
        @return None / None.
        @raise StaleScheduleClaimError claim 已被回收或替换 / The claim was recovered or replaced.
        """

        changed = await db_connection.execute(
            "UPDATE ai_schedules SET status = 'executed', executed_at = CURRENT_TIMESTAMP, "
            "updated_at = CURRENT_TIMESTAMP, error = NULL, claim_token = NULL, "
            "lease_expires_at = NULL WHERE id = %s AND status = 'executing' "
            "AND claim_token = CAST(%s AS uuid)",
            (claim.job.schedule_id, claim.token),
        )
        _require_current_claim(changed, claim)

    async def reschedule(
        self,
        claim: ScheduleClaim[PromptJobPayload],
        *,
        last_run_at: datetime,
        next_run_at: datetime,
    ) -> None:
        """@brief 以 claim token 推进周期任务 / Advance a recurring job with its claim token.

        @param claim 当前领取凭证 / Current claim.
        @param last_run_at 本次计划时刻 / Current scheduled occurrence.
        @param next_run_at 下一计划时刻 / Next scheduled occurrence.
        @return None / None.
        @raise StaleScheduleClaimError claim 已被回收或替换 / The claim was recovered or replaced.
        """

        changed = await db_connection.execute(
            "UPDATE ai_schedules SET status = 'pending', run_at = %s, last_run_at = %s, "
            "executed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP, error = NULL, "
            "claim_token = NULL, lease_expires_at = NULL "
            "WHERE id = %s AND status = 'executing' AND claim_token = CAST(%s AS uuid)",
            (
                to_storage_datetime(next_run_at),
                to_storage_datetime(last_run_at),
                claim.job.schedule_id,
                claim.token,
            ),
        )
        _require_current_claim(changed, claim)

    async def mark_failed(
        self,
        claim: ScheduleClaim[PromptJobPayload],
        error: str,
    ) -> None:
        """@brief 以 claim token 标记执行失败 / Mark execution failed with its claim token.

        @param claim 当前领取凭证 / Current claim.
        @param error 有界错误描述 / Bounded error description.
        @return None / None.
        @raise StaleScheduleClaimError claim 已被回收或替换 / The claim was recovered or replaced.
        """

        changed = await db_connection.execute(
            "UPDATE ai_schedules SET status = 'failed', error = %s, "
            "updated_at = CURRENT_TIMESTAMP, claim_token = NULL, lease_expires_at = NULL "
            "WHERE id = %s AND status = 'executing' AND claim_token = CAST(%s AS uuid)",
            (error[:500], claim.job.schedule_id, claim.token),
        )
        _require_current_claim(changed, claim)

    @staticmethod
    def _map_row(row: Row[Any]) -> ScheduledJob[PromptJobPayload]:
        """@brief 将仓储私有行转换为领域任务 / Map a repository-private row to a domain job."""

        (
            schedule_id,
            owner_id,
            run_at,
            created_at,
            reason,
            context,
            prompt,
            unit,
            interval,
        ) = row
        return ScheduleRepository._map_values(
            schedule_id=schedule_id,
            owner_id=owner_id,
            run_at=run_at,
            created_at=created_at,
            reason=reason,
            context=context,
            prompt=prompt,
            unit=unit,
            interval=interval,
        )

    @staticmethod
    def _map_values(
        *,
        schedule_id: object,
        owner_id: object,
        run_at: datetime,
        created_at: datetime | None,
        reason: object,
        context: object,
        prompt: object,
        unit: object,
        interval: object,
    ) -> ScheduledJob[PromptJobPayload]:
        """@brief 将字段集合转换为领域任务 / Map stored fields to a domain job."""

        return ScheduledJob(
            schedule_id=int(cast(Any, schedule_id)),
            owner_id=int(cast(Any, owner_id)),
            kind=PROMPT_JOB_KIND,
            run_at=ensure_utc(run_at),
            created_at=ensure_utc(created_at) if created_at is not None else None,
            recurrence=Recurrence.from_storage(unit, interval),
            payload=PromptJobPayload(
                trigger_reason=_decode_text(reason),
                context_text=_decode_optional_text(context),
                instruction=_decode_text(prompt),
            ),
        )


def _require_current_claim(
    changed: int,
    claim: ScheduleClaim[PromptJobPayload],
) -> None:
    """@brief 要求终结操作命中唯一当前 claim / Require finalization to match the one current claim.

    @param changed SQL 影响行数 / SQL affected-row count.
    @param claim 尝试终结的领取凭证 / Claim being finalized.
    @return None / None.
    @raise StaleScheduleClaimError 未命中唯一当前 claim / No unique current claim matched.
    """

    if changed != 1:
        raise StaleScheduleClaimError(
            f"schedule claim is stale: schedule_id={claim.job.schedule_id}"
        )


def _decode_text(value: object) -> str:
    """@brief 严格解码必填文本 / Strictly decode required text."""

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return str(value or "")


def _decode_optional_text(value: object) -> str | None:
    """@brief 解码可选文本 / Decode optional text."""

    if value is None:
        return None
    return _decode_text(value)
