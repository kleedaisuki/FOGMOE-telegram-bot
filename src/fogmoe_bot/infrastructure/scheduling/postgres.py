"""@brief Scheduled-Assistant 的 PostgreSQL 持久化边界 / PostgreSQL persistence boundary for Scheduled Assistant."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.conversation.workflow import PreparedTurnAcceptance
from fogmoe_bot.application.scheduling.assistant_ports import ScheduleDefinition
from fogmoe_bot.domain.conversation.identity import ConversationId, DeliveryStreamId
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    CalendarDaily,
    CalendarWeekly,
    Cadence,
    FixedInterval,
    MisfirePolicy,
    OneShot,
    ScheduleClaim,
    ScheduleSnapshot,
    ScheduleStatus,
    ScheduleTarget,
    ScheduledAssistantTurn,
    StaleScheduleClaimError,
)
from fogmoe_bot.domain.temporal import TimeZoneId, ensure_utc
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_workflow.turn import (
    PostgresTurnRepository,
)


_SCHEDULE_COLUMNS = (
    "schedule_id, creator_user_id, target_kind, target_chat_id, "
    "target_thread_id, target_conversation_id, target_delivery_stream_id, "
    "trigger_reason, context_snapshot, instruction, cadence_kind, "
    "fixed_interval_seconds, calendar_interval, calendar_anchor_date, "
    "calendar_local_time, calendar_weekday_mask, time_zone, next_run_at, "
    "misfire_policy, misfire_grace_seconds, status, version, attempt_count, "
    "next_attempt_at, claim_token, lease_expires_at, last_accepted_for, "
    "last_accepted_at, misfire_count, last_error, created_at, updated_at, terminal_at"
)
"""@brief 所有 adapter 共享的规范列顺序 / Canonical column order shared by all adapters."""


class PostgresScheduleCatalog:
    """@brief 绑定调用方事务的 schedule catalog / Schedule catalog bound to a caller-owned transaction.

    @note 本类不自行 commit 或 rollback，以便应用服务把 scope lock、配额检查与写入
        放在同一事务内。/ This class never commits or rolls back, allowing scope locking,
        quota checks, and mutation to share one transaction.
    """

    def __init__(self, connection: AsyncConnection) -> None:
        """@brief 绑定已活跃的 PostgreSQL 事务连接 / Bind an active PostgreSQL transaction connection.

        @param connection 调用方拥有的事务连接 / Caller-owned transaction connection.
        """

        self._connection = connection
        """@brief 当前 catalog 的事务连接 / Transaction connection for this catalog."""

    async def lock_scope(self, creator_user_id: int, conversation_id: str) -> None:
        """@brief 用 transaction advisory lock 串行化 scope mutation / Serialize scope mutations with a transaction advisory lock.

        @param creator_user_id schedule 创建者 / Schedule creator.
        @param conversation_id 目标会话标识 / Target conversation identifier.
        @return None / None.
        """

        scope_key = _scope_key(creator_user_id, conversation_id)
        await db_connection.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (scope_key,),
            connection=self._connection,
        )

    async def count_active(
        self,
        creator_user_id: int,
        conversation_id: str,
    ) -> int:
        """@brief 统计 scope 内活跃 schedules / Count active schedules in a scope.

        @param creator_user_id schedule 创建者 / Schedule creator.
        @param conversation_id 目标会话标识 / Target conversation identifier.
        @return 活跃 schedule 数 / Active schedule count.
        """

        row = await db_connection.fetch_one(
            "SELECT COUNT(*) FROM scheduling.assistant_schedules "
            "WHERE creator_user_id = %s AND target_conversation_id = %s "
            "AND status IN ('pending', 'processing', 'retry_wait')",
            (creator_user_id, conversation_id),
            connection=self._connection,
        )
        if row is None:
            raise RuntimeError("PostgreSQL COUNT returned no row")
        return int(row[0])

    async def create(
        self,
        definition: ScheduleDefinition,
        *,
        created_at: datetime,
    ) -> ScheduledAssistantTurn:
        """@brief 插入新 schedule identity / Insert a new schedule identity.

        @param definition 已验证定义 / Validated schedule definition.
        @param created_at 创建时刻 / Creation instant.
        @return 数据库规范 schedule / Canonical persisted schedule.
        """

        timestamp = ensure_utc(created_at)
        _validate_definition_for_storage(
            definition,
            schedule_id=1,
            boundary_at=timestamp,
        )
        storage = _cadence_storage(
            definition.cadence,
            next_run_at=definition.first_run_at,
        )
        row = await db_connection.fetch_one(
            "INSERT INTO scheduling.assistant_schedules ("
            "creator_user_id, target_kind, target_chat_id, target_thread_id, "
            "target_conversation_id, target_delivery_stream_id, trigger_reason, "
            "context_snapshot, instruction, cadence_kind, fixed_interval_seconds, "
            "calendar_interval, calendar_anchor_date, calendar_local_time, "
            "calendar_weekday_mask, time_zone, next_run_at, misfire_policy, "
            "misfire_grace_seconds, status, version, attempt_count, next_attempt_at, "
            "misfire_count, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, 'pending', 0, 0, %s, 0, %s, %s) "
            f"RETURNING {_SCHEDULE_COLUMNS}",
            (
                definition.creator_user_id,
                _target_kind(definition.target),
                definition.target.chat_id,
                definition.target.message_thread_id,
                str(definition.target.conversation_id),
                str(definition.target.delivery_stream_id),
                definition.trigger_reason,
                definition.context_snapshot,
                definition.instruction,
                storage.kind,
                storage.fixed_interval_seconds,
                storage.calendar_interval,
                storage.calendar_anchor_date,
                storage.calendar_local_time,
                storage.calendar_weekday_mask,
                definition.time_zone.value,
                definition.first_run_at,
                definition.misfire_policy.value,
                _optional_seconds(definition.misfire_grace),
                definition.first_run_at,
                timestamp,
                timestamp,
            ),
            mapping=True,
            connection=self._connection,
        )
        return _schedule_from_row(_required_mapping(row, operation="create"))

    async def replace(
        self,
        schedule_id: int,
        definition: ScheduleDefinition,
        *,
        updated_at: datetime,
    ) -> ScheduledAssistantTurn | None:
        """@brief 完整替换尚未被 claim 的 schedule / Fully replace a schedule that is not claimed.

        @param schedule_id 保留的 schedule identity / Schedule identity to retain.
        @param definition 新的完整定义 / New complete definition.
        @param updated_at 替换时刻 / Replacement instant.
        @return 替换后的 schedule；不存在、越 scope 或处理中时为 None /
            Replaced schedule, or None when absent, outside the scope, or processing.
        """

        timestamp = ensure_utc(updated_at)
        _validate_definition_for_storage(
            definition,
            schedule_id=schedule_id,
            boundary_at=timestamp,
        )
        storage = _cadence_storage(
            definition.cadence,
            next_run_at=definition.first_run_at,
        )
        row = await db_connection.fetch_one(
            "UPDATE scheduling.assistant_schedules SET "
            "target_kind = %s, target_chat_id = %s, target_thread_id = %s, "
            "target_delivery_stream_id = %s, trigger_reason = %s, "
            "context_snapshot = %s, instruction = %s, cadence_kind = %s, "
            "fixed_interval_seconds = %s, calendar_interval = %s, "
            "calendar_anchor_date = %s, calendar_local_time = %s, "
            "calendar_weekday_mask = %s, time_zone = %s, next_run_at = %s, "
            "misfire_policy = %s, misfire_grace_seconds = %s, status = 'pending', "
            "version = version + 1, attempt_count = 0, next_attempt_at = %s, "
            "claim_token = NULL, lease_expires_at = NULL, last_accepted_for = NULL, "
            "last_accepted_at = NULL, misfire_count = 0, last_error = NULL, "
            "updated_at = %s, terminal_at = NULL "
            "WHERE schedule_id = %s AND creator_user_id = %s "
            "AND target_conversation_id = %s AND status IN ('pending', 'retry_wait') "
            f"RETURNING {_SCHEDULE_COLUMNS}",
            (
                _target_kind(definition.target),
                definition.target.chat_id,
                definition.target.message_thread_id,
                str(definition.target.delivery_stream_id),
                definition.trigger_reason,
                definition.context_snapshot,
                definition.instruction,
                storage.kind,
                storage.fixed_interval_seconds,
                storage.calendar_interval,
                storage.calendar_anchor_date,
                storage.calendar_local_time,
                storage.calendar_weekday_mask,
                definition.time_zone.value,
                definition.first_run_at,
                definition.misfire_policy.value,
                _optional_seconds(definition.misfire_grace),
                definition.first_run_at,
                timestamp,
                schedule_id,
                definition.creator_user_id,
                str(definition.target.conversation_id),
            ),
            mapping=True,
            connection=self._connection,
        )
        return None if row is None else _schedule_from_row(_mapping(row))

    async def list(
        self,
        *,
        creator_user_id: int,
        conversation_id: str,
        limit: int,
    ) -> Sequence[ScheduleSnapshot]:
        """@brief 读取 scope 内 schedule 生命周期快照 / Read lifecycle snapshots in a scope.

        @param creator_user_id schedule 创建者 / Schedule creator.
        @param conversation_id 目标会话标识 / Target conversation identifier.
        @param limit 最大行数 / Maximum row count.
        @return 按创建时间逆序的快照 / Snapshots ordered newest first.
        """

        rows = await db_connection.fetch_all(
            f"SELECT {_SCHEDULE_COLUMNS} FROM scheduling.assistant_schedules "
            "WHERE creator_user_id = %s AND target_conversation_id = %s "
            "ORDER BY created_at DESC, schedule_id DESC LIMIT %s",
            (creator_user_id, conversation_id, limit),
            mapping=True,
            connection=self._connection,
        )
        return tuple(_snapshot_from_row(_mapping(row)) for row in rows)

    async def cancel(
        self,
        *,
        schedule_id: int,
        creator_user_id: int,
        conversation_id: str,
        cancelled_at: datetime,
    ) -> bool:
        """@brief 取消当前 scope 内的活跃 schedule / Cancel an active schedule in the current scope.

        @param schedule_id schedule identity / Schedule identity.
        @param creator_user_id 调用者 identity / Calling creator identity.
        @param conversation_id 调用会话 scope / Calling conversation scope.
        @param cancelled_at 取消时刻 / Cancellation instant.
        @return 发生状态转换时为 True / True when a state transition occurred.
        @note 取消 processing 行会清除 token，从而 fencing 正在运行的 worker。/
            Cancelling a processing row clears its token and thereby fences its worker.
        """

        timestamp = ensure_utc(cancelled_at)
        changed = await db_connection.execute(
            "UPDATE scheduling.assistant_schedules SET status = 'cancelled', "
            "version = version + 1, next_attempt_at = NULL, claim_token = NULL, "
            "lease_expires_at = NULL, updated_at = %s, terminal_at = %s "
            "WHERE schedule_id = %s AND creator_user_id = %s "
            "AND target_conversation_id = %s "
            "AND status IN ('pending', 'processing', 'retry_wait')",
            (
                timestamp,
                timestamp,
                schedule_id,
                creator_user_id,
                conversation_id,
            ),
            connection=self._connection,
        )
        return changed == 1


class PostgresScheduleQueue:
    """@brief 以行锁、lease 和 UUID token 实现的 fenced queue / Fenced queue using row locks, leases, and UUID tokens."""

    async def recover_expired(self, *, now: datetime) -> int:
        """@brief 回收已过期 processing leases / Recover expired processing leases.

        @param now worker 观测的 UTC 时刻 / UTC instant observed by the worker.
        @return 回收行数 / Number of recovered rows.
        """

        timestamp = ensure_utc(now)
        return await db_connection.execute(
            "UPDATE scheduling.assistant_schedules SET status = 'retry_wait', "
            "version = version + 1, next_attempt_at = %s, claim_token = NULL, "
            "lease_expires_at = NULL, last_error = 'recovered expired lease', "
            "updated_at = %s WHERE status = 'processing' AND lease_expires_at <= %s",
            (timestamp, timestamp, timestamp),
        )

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[ScheduleClaim]:
        """@brief 原子领取到期 occurrences，每会话最多一个 / Atomically claim due occurrences, at most one per conversation.

        @param now 领取时刻 / Claim instant.
        @param limit 最大 claim 数 / Maximum number of claims.
        @param lease_for 独占租约时长 / Exclusive lease duration.
        @return 带独立 UUID fencing token 的 claims / Claims carrying distinct UUID fencing tokens.
        @note ``FOR UPDATE SKIP LOCKED`` 与会话 head-of-line 谓词共同防止同一
            Conversation 并发执行；partial unique index 是最终数据库护栏。/
            ``FOR UPDATE SKIP LOCKED`` and a per-conversation head-of-line predicate prevent
            concurrent execution in one Conversation; the partial unique index is the final guardrail.
        """

        timestamp, lease_expires_at = _claim_window(now, limit, lease_for)
        if limit == 0:
            return ()
        claims: list[ScheduleClaim] = []
        async with db_connection.transaction() as connection:
            for _ in range(limit):
                token = uuid4()
                row = await db_connection.fetch_one(
                    "WITH candidate AS ("
                    "SELECT schedule.schedule_id "
                    "FROM scheduling.assistant_schedules AS schedule "
                    "WHERE schedule.status IN ('pending', 'retry_wait') "
                    "AND schedule.next_attempt_at <= %s "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM scheduling.assistant_schedules AS running "
                    "WHERE running.target_conversation_id = "
                    "schedule.target_conversation_id AND running.status = 'processing') "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM scheduling.assistant_schedules AS earlier "
                    "WHERE earlier.target_conversation_id = "
                    "schedule.target_conversation_id "
                    "AND earlier.status IN ('pending', 'retry_wait') "
                    "AND earlier.next_attempt_at <= %s AND "
                    "(earlier.next_attempt_at, earlier.next_run_at, earlier.schedule_id) < "
                    "(schedule.next_attempt_at, schedule.next_run_at, schedule.schedule_id)) "
                    "ORDER BY schedule.next_attempt_at, schedule.next_run_at, "
                    "schedule.schedule_id LIMIT 1 FOR UPDATE OF schedule SKIP LOCKED) "
                    "UPDATE scheduling.assistant_schedules AS claimed SET "
                    "status = 'processing', version = claimed.version + 1, "
                    "attempt_count = claimed.attempt_count + 1, next_attempt_at = NULL, "
                    "claim_token = %s, lease_expires_at = %s, last_error = NULL, "
                    "updated_at = %s FROM candidate "
                    "WHERE claimed.schedule_id = candidate.schedule_id "
                    f"RETURNING {_qualified_schedule_columns('claimed')}",
                    (
                        timestamp,
                        timestamp,
                        token,
                        lease_expires_at,
                        timestamp,
                    ),
                    mapping=True,
                    connection=connection,
                )
                if row is None:
                    break
                mapped = _mapping(row)
                claims.append(
                    ScheduleClaim(
                        schedule=_schedule_from_row(mapped),
                        attempt_count=int(mapped["attempt_count"]),
                        token=token,
                        claimed_at=timestamp,
                        lease_expires_at=lease_expires_at,
                    )
                )
        return tuple(claims)

    async def retry(
        self,
        claim: ScheduleClaim,
        *,
        retry_at: datetime,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 用 claim token 安排同一 occurrence 重试 / Schedule a retry of the same occurrence using its claim token.

        @param claim 当前 claim / Current claim.
        @param retry_at 下次尝试时刻 / Next-attempt instant.
        @param failed_at 本次失败时刻 / Failure instant.
        @param error 有界错误摘要 / Bounded error summary.
        @return None / None.
        @raise StaleScheduleClaimError token 已失效时抛出 / Raised when the token is stale.
        """

        failed = ensure_utc(failed_at)
        retry = ensure_utc(retry_at)
        if retry < failed:
            raise ValueError("retry_at cannot precede failed_at")
        await _fenced_update(
            "UPDATE scheduling.assistant_schedules SET status = 'retry_wait', "
            "version = version + 1, next_attempt_at = %s, claim_token = NULL, "
            "lease_expires_at = NULL, last_error = %s, updated_at = %s "
            "WHERE schedule_id = %s AND status = 'processing' AND claim_token = %s",
            (
                retry,
                _required_error(error),
                failed,
                claim.schedule.schedule_id,
                claim.token,
            ),
            claim,
        )

    async def fail_final(
        self,
        claim: ScheduleClaim,
        *,
        failed_at: datetime,
        error: str,
    ) -> None:
        """@brief 以 token fencing 终结失败 schedule / Finally fail a schedule with token fencing.

        @param claim 当前 claim / Current claim.
        @param failed_at 失败时刻 / Failure instant.
        @param error 有界错误摘要 / Bounded error summary.
        @return None / None.
        @raise StaleScheduleClaimError token 已失效时抛出 / Raised when the token is stale.
        """

        timestamp = ensure_utc(failed_at)
        await _fenced_update(
            "UPDATE scheduling.assistant_schedules SET status = 'failed_final', "
            "version = version + 1, next_attempt_at = NULL, claim_token = NULL, "
            "lease_expires_at = NULL, last_error = %s, updated_at = %s, "
            "terminal_at = %s WHERE schedule_id = %s AND status = 'processing' "
            "AND claim_token = %s",
            (
                _required_error(error),
                timestamp,
                timestamp,
                claim.schedule.schedule_id,
                claim.token,
            ),
            claim,
        )

    async def skip_misfire(
        self,
        claim: ScheduleClaim,
        *,
        next_run_at: datetime | None,
        skipped_at: datetime,
    ) -> None:
        """@brief 不产生 Turn 地推进或过期 occurrence / Advance or expire an occurrence without creating a Turn.

        @param claim 当前 claim / Current claim.
        @param next_run_at 下一 occurrence，None 表示耗尽 / Next occurrence, or None when exhausted.
        @param skipped_at 跳过时刻 / Skip instant.
        @return None / None.
        @raise StaleScheduleClaimError token 已失效时抛出 / Raised when the token is stale.
        """

        timestamp = ensure_utc(skipped_at)
        if next_run_at is None:
            await _fenced_update(
                "UPDATE scheduling.assistant_schedules SET status = 'expired', "
                "version = version + 1, attempt_count = 0, next_attempt_at = NULL, "
                "claim_token = NULL, lease_expires_at = NULL, misfire_count = "
                "misfire_count + 1, last_error = NULL, updated_at = %s, terminal_at = %s "
                "WHERE schedule_id = %s AND status = 'processing' AND claim_token = %s",
                (
                    timestamp,
                    timestamp,
                    claim.schedule.schedule_id,
                    claim.token,
                ),
                claim,
            )
            return

        next_run = ensure_utc(next_run_at)
        if next_run <= claim.schedule.next_run_at:
            raise ValueError("next_run_at must advance the schedule cursor")
        anchor_date = _calendar_anchor_date(claim.schedule.cadence, next_run)
        await _fenced_update(
            "UPDATE scheduling.assistant_schedules SET status = 'pending', "
            "version = version + 1, attempt_count = 0, next_run_at = %s, "
            "next_attempt_at = %s, calendar_anchor_date = %s, claim_token = NULL, "
            "lease_expires_at = NULL, misfire_count = misfire_count + 1, "
            "last_error = NULL, updated_at = %s, terminal_at = NULL "
            "WHERE schedule_id = %s AND status = 'processing' AND claim_token = %s",
            (
                next_run,
                next_run,
                anchor_date,
                timestamp,
                claim.schedule.schedule_id,
                claim.token,
            ),
            claim,
        )


class PostgresScheduledOccurrenceAcceptance:
    """@brief 原子推进 schedule cursor 并接受 Conversation Turn / Atomically advance a schedule cursor and accept a Conversation Turn."""

    def __init__(self, turns: PostgresTurnRepository) -> None:
        """@brief 注入 connection-bound Turn repository primitive / Inject the connection-bound Turn repository primitive.

        @param turns Conversation Turn 持久化 adapter / Conversation Turn persistence adapter.
        """

        self._turns = turns
        """@brief 同事务 Turn acceptance primitive / Same-transaction Turn acceptance primitive."""

    async def accept(
        self,
        claim: ScheduleClaim,
        prepared: PreparedTurnAcceptance,
        *,
        next_run_at: datetime | None,
        accepted_at: datetime,
    ) -> None:
        """@brief 在一个短事务内接受 Turn 并 fenced 推进 schedule / Accept a Turn and fenced-advance its schedule in one short transaction.

        @param claim 当前 schedule claim / Current schedule claim.
        @param prepared 纯构造的 Turn acceptance / Purely prepared Turn acceptance.
        @param next_run_at 下一 occurrence，None 表示一次性完成 / Next occurrence, or None for one-shot completion.
        @param accepted_at 原子接受时刻 / Atomic acceptance instant.
        @return None / None.
        @raise StaleScheduleClaimError token 已被取消、回收或替代时抛出 /
            Raised when the token was cancelled, recovered, or superseded.
        @note 事务内只包含 PostgreSQL I/O，不调用 LLM、Telegram 或 sleep。/
            The transaction contains PostgreSQL I/O only, with no LLM, Telegram, or sleep call.
        """

        timestamp = ensure_utc(accepted_at)
        if timestamp != prepared.accepted_at:
            raise ValueError("accepted_at must match the prepared acceptance")
        next_run = ensure_utc(next_run_at) if next_run_at is not None else None
        if next_run is not None and next_run <= claim.schedule.next_run_at:
            raise ValueError("next_run_at must advance the schedule cursor")

        async with db_connection.transaction() as connection:
            locked = await db_connection.fetch_one(
                "SELECT schedule_id FROM scheduling.assistant_schedules "
                "WHERE schedule_id = %s AND status = 'processing' "
                "AND claim_token = %s FOR UPDATE",
                (claim.schedule.schedule_id, claim.token),
                connection=connection,
            )
            if locked is None:
                raise _stale_claim(claim)

            await self._turns.create_and_accept_turn_in_transaction(
                connection,
                prepared.turn,
                message=prepared.message,
                activity=prepared.activity,
                accepted_at=prepared.accepted_at,
            )

            anchor_date = (
                _calendar_anchor_date(claim.schedule.cadence, next_run)
                if next_run is not None
                else _calendar_anchor_date(
                    claim.schedule.cadence,
                    claim.schedule.next_run_at,
                )
            )
            if next_run is None:
                changed = await db_connection.execute(
                    "UPDATE scheduling.assistant_schedules SET status = 'completed', "
                    "version = version + 1, attempt_count = 0, next_attempt_at = NULL, "
                    "claim_token = NULL, lease_expires_at = NULL, last_accepted_for = %s, "
                    "last_accepted_at = %s, last_error = NULL, updated_at = %s, "
                    "terminal_at = %s WHERE schedule_id = %s AND status = 'processing' "
                    "AND claim_token = %s",
                    (
                        claim.schedule.next_run_at,
                        timestamp,
                        timestamp,
                        timestamp,
                        claim.schedule.schedule_id,
                        claim.token,
                    ),
                    connection=connection,
                )
            else:
                changed = await db_connection.execute(
                    "UPDATE scheduling.assistant_schedules SET status = 'pending', "
                    "version = version + 1, attempt_count = 0, next_run_at = %s, "
                    "next_attempt_at = %s, calendar_anchor_date = %s, claim_token = NULL, "
                    "lease_expires_at = NULL, last_accepted_for = %s, "
                    "last_accepted_at = %s, last_error = NULL, updated_at = %s, "
                    "terminal_at = NULL WHERE schedule_id = %s AND status = 'processing' "
                    "AND claim_token = %s",
                    (
                        next_run,
                        next_run,
                        anchor_date,
                        claim.schedule.next_run_at,
                        timestamp,
                        timestamp,
                        claim.schedule.schedule_id,
                        claim.token,
                    ),
                    connection=connection,
                )
            _require_current_claim(changed, claim)


@dataclass(frozen=True, slots=True, kw_only=True)
class _CadenceStorage:
    """@brief cadence 数据库判别式字段 / Discriminated database fields for a cadence.

    @param kind cadence discriminator / Cadence discriminator.
    @param fixed_interval_seconds 固定间隔秒数 / Fixed interval in seconds.
    @param calendar_interval calendar 周期倍数 / Calendar interval multiplier.
    @param calendar_anchor_date 当前 occurrence 本地日期 / Local date of the current occurrence.
    @param calendar_local_time 周期本地墙钟时间 / Recurring local wall-clock time.
    @param calendar_weekday_mask ISO weekday 位图 / ISO-weekday bitmap.
    """

    kind: str
    fixed_interval_seconds: int | None = None
    calendar_interval: int | None = None
    calendar_anchor_date: date | None = None
    calendar_local_time: time | None = None
    calendar_weekday_mask: int | None = None


def _validate_definition_for_storage(
    definition: ScheduleDefinition,
    *,
    schedule_id: int,
    boundary_at: datetime,
) -> None:
    """@brief 在 SQL 前复用聚合不变量验证完整定义 / Reuse aggregate invariants to validate a complete definition before SQL.

    @param definition 应用层定义 / Application-layer definition.
    @param schedule_id 新建 placeholder 或已有 identity / Creation placeholder or existing identity.
    @param boundary_at 创建或替换时刻 / Creation or replacement instant.
    @return None / None.
    @note 这会在写入前拒绝 cadence/time-zone/cursor 不一致，避免依赖
        INSERT RETURNING 后的映射失败才回滚。/ This rejects cadence/time-zone/cursor
        mismatches before writing rather than relying on a mapping failure after INSERT RETURNING.
    """

    ScheduledAssistantTurn(
        schedule_id=schedule_id,
        creator_user_id=definition.creator_user_id,
        target=definition.target,
        trigger_reason=definition.trigger_reason,
        instruction=definition.instruction,
        cadence=definition.cadence,
        next_run_at=definition.first_run_at,
        created_at=boundary_at,
        time_zone=definition.time_zone,
        context_snapshot=definition.context_snapshot,
        misfire_policy=definition.misfire_policy,
        misfire_grace=definition.misfire_grace,
    )


def _cadence_storage(
    cadence: Cadence,
    *,
    next_run_at: datetime,
) -> _CadenceStorage:
    """@brief 将 cadence 编码为正交数据库字段 / Encode a cadence into orthogonal database fields.

    @param cadence 领域 recurrence 规则 / Domain recurrence rule.
    @param next_run_at 当前 occurrence，用于持久化 calendar anchor / Current occurrence used to persist the calendar anchor.
    @return cadence 持久化数据 / Cadence persistence data.
    """

    if isinstance(cadence, OneShot):
        return _CadenceStorage(kind="one_shot")
    if isinstance(cadence, FixedInterval):
        return _CadenceStorage(
            kind="fixed_interval",
            fixed_interval_seconds=_required_whole_seconds(cadence.every),
        )
    if isinstance(cadence, CalendarDaily):
        return _CadenceStorage(
            kind="calendar_daily",
            calendar_interval=cadence.interval,
            calendar_anchor_date=_calendar_anchor_date(cadence, next_run_at),
            calendar_local_time=cadence.local_time,
        )
    if isinstance(cadence, CalendarWeekly):
        return _CadenceStorage(
            kind="calendar_weekly",
            calendar_interval=cadence.interval,
            calendar_anchor_date=_calendar_anchor_date(cadence, next_run_at),
            calendar_local_time=cadence.local_time,
            calendar_weekday_mask=_encode_weekdays(cadence.weekdays),
        )
    raise TypeError("Unsupported assistant schedule cadence")


def _cadence_from_row(row: Mapping[str, Any], time_zone: TimeZoneId) -> Cadence:
    """@brief 从判别式数据库字段还原 cadence / Restore a cadence from discriminated database fields.

    @param row schedule 行 / Schedule row.
    @param time_zone 已验证 schedule 时区 / Validated schedule time zone.
    @return 领域 cadence / Domain cadence.
    """

    kind = str(row["cadence_kind"])
    if kind == "one_shot":
        return OneShot()
    if kind == "fixed_interval":
        return FixedInterval(
            every=timedelta(seconds=_required_int(row["fixed_interval_seconds"]))
        )
    local_time = row["calendar_local_time"]
    if not isinstance(local_time, time):
        raise TypeError("Stored calendar_local_time must be a time")
    interval = _required_int(row["calendar_interval"])
    if kind == "calendar_daily":
        return CalendarDaily(
            local_time=local_time,
            time_zone=time_zone,
            interval=interval,
        )
    if kind == "calendar_weekly":
        return CalendarWeekly(
            local_time=local_time,
            time_zone=time_zone,
            weekdays=_decode_weekdays(_required_int(row["calendar_weekday_mask"])),
            interval=interval,
        )
    raise ValueError(f"Unknown stored cadence_kind: {kind}")


def _schedule_from_row(row: Mapping[str, Any]) -> ScheduledAssistantTurn:
    """@brief 将数据库行映射为 schedule 聚合 / Map a database row to a schedule aggregate.

    @param row 数据库映射行 / Database mapping row.
    @return 已验证 schedule 聚合 / Validated schedule aggregate.
    """

    time_zone = TimeZoneId(str(row["time_zone"]))
    cadence = _cadence_from_row(row, time_zone)
    next_run_at = _datetime(row["next_run_at"])
    _validate_calendar_anchor(row, cadence, next_run_at)
    target_kind = str(row["target_kind"])
    if target_kind not in {"private", "group"}:
        raise ValueError(f"Unknown stored target_kind: {target_kind}")
    return ScheduledAssistantTurn(
        schedule_id=_required_int(row["schedule_id"]),
        creator_user_id=_required_int(row["creator_user_id"]),
        target=ScheduleTarget(
            conversation_id=ConversationId(str(row["target_conversation_id"])),
            delivery_stream_id=DeliveryStreamId(str(row["target_delivery_stream_id"])),
            chat_id=_required_int(row["target_chat_id"]),
            is_group=target_kind == "group",
            message_thread_id=_optional_int(row["target_thread_id"]),
        ),
        trigger_reason=str(row["trigger_reason"]),
        instruction=str(row["instruction"]),
        cadence=cadence,
        next_run_at=next_run_at,
        created_at=_datetime(row["created_at"]),
        time_zone=time_zone,
        context_snapshot=_optional_text(row["context_snapshot"]),
        misfire_policy=MisfirePolicy(str(row["misfire_policy"])),
        misfire_grace=_optional_timedelta(row["misfire_grace_seconds"]),
    )


def _snapshot_from_row(row: Mapping[str, Any]) -> ScheduleSnapshot:
    """@brief 映射 schedule 生命周期快照 / Map a schedule lifecycle snapshot.

    @param row 数据库映射行 / Database mapping row.
    @return 领域查询快照 / Domain query snapshot.
    """

    return ScheduleSnapshot(
        schedule=_schedule_from_row(row),
        status=ScheduleStatus(str(row["status"])),
        attempt_count=_required_int(row["attempt_count"]),
        last_accepted_for=_optional_datetime(row["last_accepted_for"]),
        last_accepted_at=_optional_datetime(row["last_accepted_at"]),
        last_error=_optional_text(row["last_error"]),
        terminal_at=_optional_datetime(row["terminal_at"]),
    )


def _calendar_anchor_date(cadence: Cadence, instant: datetime) -> date | None:
    """@brief 计算 calendar cadence 当前 occurrence 的本地日期 / Compute the local date of a calendar cadence occurrence.

    @param cadence recurrence 规则 / Recurrence rule.
    @param instant occurrence 瞬间 / Occurrence instant.
    @return calendar 本地日期，非 calendar 为 None / Calendar local date, or None for a non-calendar cadence.
    """

    if isinstance(cadence, (CalendarDaily, CalendarWeekly)):
        return cadence.time_zone.localize(instant).date()
    return None


def _validate_calendar_anchor(
    row: Mapping[str, Any], cadence: Cadence, next_run_at: datetime
) -> None:
    """@brief 验证持久化 anchor 与 cursor 一致 / Validate that the persisted anchor matches the cursor.

    @param row schedule 行 / Schedule row.
    @param cadence 已解码 cadence / Decoded cadence.
    @param next_run_at 当前 cursor / Current cursor.
    @return None / None.
    """

    expected = _calendar_anchor_date(cadence, next_run_at)
    stored = row["calendar_anchor_date"]
    if expected is None:
        if stored is not None:
            raise ValueError("Non-calendar cadence cannot carry calendar_anchor_date")
        return
    if not isinstance(stored, date) or isinstance(stored, datetime):
        raise TypeError("Stored calendar_anchor_date must be a date")
    if stored != expected:
        raise ValueError("Stored calendar_anchor_date does not match next_run_at")


def _encode_weekdays(weekdays: frozenset[int]) -> int:
    """@brief 将 ISO weekday 1..7 编码为位图 / Encode ISO weekdays 1..7 as a bitmap.

    @param weekdays ISO weekday 集合 / ISO-weekday set.
    @return 1..127 位图 / Bitmap from 1 through 127.
    """

    return sum(1 << (weekday - 1) for weekday in weekdays)


def _decode_weekdays(mask: int) -> frozenset[int]:
    """@brief 将 7-bit 位图解码为 ISO weekdays / Decode a seven-bit bitmap into ISO weekdays.

    @param mask 1..127 位图 / Bitmap from 1 through 127.
    @return ISO weekday 集合 / ISO-weekday set.
    """

    if not 1 <= mask <= 127:
        raise ValueError("Stored calendar_weekday_mask must be between 1 and 127")
    return frozenset(day for day in range(1, 8) if mask & (1 << (day - 1)))


async def _fenced_update(
    sql: str,
    params: tuple[object, ...],
    claim: ScheduleClaim,
) -> None:
    """@brief 执行必须命中当前 token 的写入 / Execute a write that must match the current token.

    @param sql 带 status 和 token 谓词的 SQL / SQL carrying status and token predicates.
    @param params SQL 绑定参数 / SQL bind parameters.
    @param claim 用于失效错误的 claim / Claim used for stale-error context.
    @return None / None.
    @raise StaleScheduleClaimError 更新未命中唯一行时抛出 / Raised unless exactly one row is updated.
    """

    changed = await db_connection.execute(sql, params)
    _require_current_claim(changed, claim)


def _require_current_claim(changed: int, claim: ScheduleClaim) -> None:
    """@brief 要求 fenced 写入命中唯一行 / Require a fenced write to affect exactly one row.

    @param changed 数据库报告的行数 / Row count reported by the database.
    @param claim 当前 claim / Current claim.
    @return None / None.
    @raise StaleScheduleClaimError 行数不是 1 时抛出 / Raised when the row count is not one.
    """

    if changed != 1:
        raise _stale_claim(claim)


def _stale_claim(claim: ScheduleClaim) -> StaleScheduleClaimError:
    """@brief 构造不泄露 token 的 stale-claim 错误 / Build a stale-claim error without disclosing its token.

    @param claim 失效 claim / Stale claim.
    @return 领域 fencing 异常 / Domain fencing exception.
    """

    return StaleScheduleClaimError(
        f"Stale schedule claim for {claim.schedule.schedule_id}"
    )


def _claim_window(
    now: datetime,
    limit: int,
    lease_for: timedelta,
) -> tuple[datetime, datetime]:
    """@brief 验证并计算 claim 时间窗 / Validate and calculate the claim window.

    @param now 领取时刻 / Claim instant.
    @param limit 领取上限 / Claim limit.
    @param lease_for 租约时长 / Lease duration.
    @return ``(claimed_at, lease_expires_at)`` / ``(claimed_at, lease_expires_at)``.
    """

    if isinstance(limit, bool) or limit < 0:
        raise ValueError("claim limit cannot be negative")
    if not isinstance(lease_for, timedelta) or lease_for <= timedelta():
        raise ValueError("lease_for must be a positive timedelta")
    timestamp = ensure_utc(now)
    return timestamp, timestamp + lease_for


def _scope_key(creator_user_id: int, conversation_id: str) -> str:
    """@brief 构造无歧义 advisory-lock key / Build an unambiguous advisory-lock key.

    @param creator_user_id schedule 创建者 / Schedule creator.
    @param conversation_id 会话标识 / Conversation identifier.
    @return 带边界分隔的 key / Boundary-delimited key.
    """

    if isinstance(creator_user_id, bool) or creator_user_id <= 0:
        raise ValueError("creator_user_id must be positive")
    normalized = conversation_id.strip()
    if not normalized:
        raise ValueError("conversation_id cannot be empty")
    return f"assistant_schedule\x00{creator_user_id}\x00{normalized}"


def _target_kind(target: ScheduleTarget) -> str:
    """@brief 将目标编码为数据库 discriminator / Encode a target as a database discriminator.

    @param target 投递目标 / Delivery target.
    @return ``private`` 或 ``group`` / ``private`` or ``group``.
    """

    return "group" if target.is_group else "private"


def _required_whole_seconds(value: timedelta) -> int:
    """@brief 将时长编码为正整数秒 / Encode a duration as positive whole seconds.

    @param value 待编码时长 / Duration to encode.
    @return 正整数秒 / Positive whole seconds.
    """

    if value.microseconds != 0:
        raise ValueError("Persisted durations must be positive whole seconds")
    seconds = value.days * 86_400 + value.seconds
    if seconds <= 0:
        raise ValueError("Persisted durations must be positive whole seconds")
    return seconds


def _optional_seconds(value: timedelta | None) -> int | None:
    """@brief 编码可选正整数秒 / Encode optional positive whole seconds.

    @param value 可选时长 / Optional duration.
    @return 秒数或 None / Seconds or None.
    """

    return None if value is None else _required_whole_seconds(value)


def _required_error(error: str) -> str:
    """@brief 规范化必填有界错误摘要 / Normalize a required bounded error summary.

    @param error 原始错误文本 / Raw error text.
    @return 1..4000 字符摘要 / Summary containing 1 through 4000 characters.
    """

    normalized = error.strip()
    if not normalized:
        raise ValueError("error cannot be empty")
    return normalized[:4_000]


def _qualified_schedule_columns(alias: str) -> str:
    """@brief 为 RETURNING 列添加固定内部 alias / Qualify RETURNING columns with a fixed internal alias.

    @param alias adapter 内部固定 SQL alias / Fixed adapter-internal SQL alias.
    @return 全部限定列 / Fully qualified columns.
    @note alias 从不接受外部输入 / The alias never accepts external input.
    """

    return ", ".join(
        f"{alias}.{column.strip()}" for column in _SCHEDULE_COLUMNS.split(",")
    )


def _required_mapping(row: object, *, operation: str) -> Mapping[str, Any]:
    """@brief 要求写入返回映射行 / Require a write to return a mapping row.

    @param row 数据库返回值 / Database return value.
    @param operation 用于错误上下文的操作名 / Operation name for error context.
    @return 映射行 / Mapping row.
    """

    if row is None:
        raise RuntimeError(f"Schedule {operation} returned no row")
    return _mapping(row)


def _mapping(row: object) -> Mapping[str, Any]:
    """@brief 收窄 SQLAlchemy mapping row 类型 / Narrow a SQLAlchemy mapping-row type.

    @param row 原始行 / Raw row.
    @return 字符串键映射 / String-keyed mapping.
    """

    if not isinstance(row, Mapping):
        raise TypeError("Expected a database mapping row")
    return cast(Mapping[str, Any], row)


def _required_int(value: object) -> int:
    """@brief 严格解码整数 / Strictly decode an integer.

    @param value 数据库值 / Database value.
    @return 整数 / Integer.
    """

    if isinstance(value, bool):
        raise TypeError("Boolean is not a stored integer")
    return int(cast(Any, value))


def _optional_int(value: object) -> int | None:
    """@brief 解码可选整数 / Decode an optional integer.

    @param value 数据库值 / Database value.
    @return 整数或 None / Integer or None.
    """

    return None if value is None else _required_int(value)


def _datetime(value: object) -> datetime:
    """@brief 严格解码 aware datetime / Strictly decode an aware datetime.

    @param value 数据库值 / Database value.
    @return UTC aware datetime / UTC-aware datetime.
    """

    if not isinstance(value, datetime):
        raise TypeError("Stored timestamp must be a datetime")
    return ensure_utc(value)


def _optional_datetime(value: object) -> datetime | None:
    """@brief 解码可选 aware datetime / Decode an optional aware datetime.

    @param value 数据库值 / Database value.
    @return UTC aware datetime 或 None / UTC-aware datetime or None.
    """

    return None if value is None else _datetime(value)


def _optional_timedelta(value: object) -> timedelta | None:
    """@brief 将可选秒数解码为 timedelta / Decode optional seconds as a timedelta.

    @param value 数据库秒数 / Stored seconds.
    @return 时长或 None / Duration or None.
    """

    return None if value is None else timedelta(seconds=_required_int(value))


def _optional_text(value: object) -> str | None:
    """@brief 解码可选文本 / Decode optional text.

    @param value 数据库值 / Database value.
    @return 文本或 None / Text or None.
    """

    return None if value is None else str(value)


__all__ = [
    "PostgresScheduleCatalog",
    "PostgresScheduledOccurrenceAcceptance",
    "PostgresScheduleQueue",
]
