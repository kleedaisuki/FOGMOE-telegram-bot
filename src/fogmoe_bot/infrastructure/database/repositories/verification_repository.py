"""@brief PostgreSQL 成员验证工作流仓储 / PostgreSQL member-verification workflow repository."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fogmoe_bot.domain.moderation.models import (
    ChatId,
    MessageId,
    ModerationToggleResult,
    UserId,
)
from fogmoe_bot.domain.moderation.verification import (
    StaleVerificationVersion,
    VerificationClaim,
    VerificationEvent,
    VerificationFencingError,
    VerificationKey,
    VerificationNotFound,
    VerificationStatus,
    VerificationTask,
    VerificationVersion,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.toggle_command_receipts import (
    load_toggle_receipt,
    lock_toggle_command,
    lock_toggle_scope,
    save_toggle_receipt,
)


_VERIFICATION_TOGGLE_OPERATION = "member_verification"
"""@brief 成员验证开关的持久操作名 / Persisted operation name for the member-verification switch."""


class PostgresVerificationRepository:
    """@brief 单表、乐观版本与 lease fencing 的验证仓储 / Single-table verification repository with OCC and lease fencing."""

    async def group_enabled(self, chat_id: ChatId) -> bool:
        """@brief 查询群组验证开关 / Read the group verification switch.

        @param chat_id 群组 ID / Chat ID.
        @return 已启用时为 True / True when enabled.
        """

        row = await db_connection.fetch_one(
            "SELECT group_id FROM moderation.group_verification WHERE group_id = %s",
            (int(chat_id),),
        )
        return row is not None

    async def enable_group(self, chat_id: ChatId, group_name: str) -> None:
        """@brief 开启群组验证 / Enable group verification.

        @param chat_id 群组 ID / Chat ID.
        @param group_name 群组名称 / Group name.
        @return None / None.
        """

        async with db_connection.transaction() as connection:
            await lock_toggle_scope(
                operation_kind=_VERIFICATION_TOGGLE_OPERATION,
                chat_id=int(chat_id),
                connection=connection,
            )
            await db_connection.execute(
                "INSERT INTO moderation.group_verification (group_id, group_name) VALUES (%s, %s) "
                "ON CONFLICT (group_id) DO UPDATE SET group_name = EXCLUDED.group_name",
                (int(chat_id), group_name),
                connection=connection,
            )

    async def disable_group(self, chat_id: ChatId) -> None:
        """@brief 关闭群组验证 / Disable group verification.

        @param chat_id 群组 ID / Chat ID.
        @return None / None.
        """

        async with db_connection.transaction() as connection:
            await lock_toggle_scope(
                operation_kind=_VERIFICATION_TOGGLE_OPERATION,
                chat_id=int(chat_id),
                connection=connection,
            )
            await db_connection.execute(
                "DELETE FROM moderation.group_verification WHERE group_id = %s",
                (int(chat_id),),
                connection=connection,
            )

    async def toggle_group(
        self,
        chat_id: ChatId,
        *,
        group_name: str,
        actor_id: UserId,
        idempotency_key: str,
    ) -> ModerationToggleResult:
        """@brief 原子切换验证并保存 source receipt / Atomically toggle verification and save its source receipt.

        @param chat_id 群组 ID / Chat ID.
        @param group_name 群组名称 / Group name.
        @param actor_id 管理员 ID / Administrator ID.
        @param idempotency_key source Update 稳定键 / Stable source-Update key.
        @return 首次提交或回放结果 / First committed or replayed result.
        """

        request_payload: dict[str, object] = {"group_name": group_name}
        async with db_connection.transaction() as connection:
            await lock_toggle_command(
                idempotency_key=idempotency_key,
                operation_kind=_VERIFICATION_TOGGLE_OPERATION,
                chat_id=int(chat_id),
                connection=connection,
            )
            replay = await load_toggle_receipt(
                idempotency_key=idempotency_key,
                operation_kind=_VERIFICATION_TOGGLE_OPERATION,
                chat_id=int(chat_id),
                actor_id=int(actor_id),
                request_payload=request_payload,
                connection=connection,
            )
            if replay is not None:
                return replay

            existing = await db_connection.fetch_one(
                "SELECT group_id FROM moderation.group_verification "
                "WHERE group_id = %s FOR UPDATE",
                (int(chat_id),),
                connection=connection,
            )
            enabled = existing is None
            if enabled:
                changed = await db_connection.execute(
                    "INSERT INTO moderation.group_verification (group_id, group_name) "
                    "VALUES (%s, %s) ON CONFLICT (group_id) DO NOTHING",
                    (int(chat_id), group_name),
                    connection=connection,
                )
            else:
                changed = await db_connection.execute(
                    "DELETE FROM moderation.group_verification WHERE group_id = %s",
                    (int(chat_id),),
                    connection=connection,
                )
            if changed != 1:
                raise RuntimeError(
                    "Verification toggle lost its serialized policy mutation"
                )
            await save_toggle_receipt(
                idempotency_key=idempotency_key,
                operation_kind=_VERIFICATION_TOGGLE_OPERATION,
                chat_id=int(chat_id),
                actor_id=int(actor_id),
                request_payload=request_payload,
                enabled=enabled,
                connection=connection,
            )
            return ModerationToggleResult(enabled=enabled)

    async def create(
        self,
        task: VerificationTask,
        *,
        recover_at: datetime,
    ) -> VerificationTask:
        """@brief 创建或替换 CREATING 聚合 / Create or replace a CREATING aggregate.

        @param task 初始创建意图 / Initial creation intent.
        @param recover_at 创建流程失联后的恢复时间 / Recovery time for an abandoned creation.
        @return 数据库分配版本后的规范聚合 / Canonical aggregate with database-assigned version.
        """

        if (
            task.status is not VerificationStatus.CREATING
            or task.message_id is not None
        ):
            raise ValueError("create requires a CREATING task without message_id")
        async with db_connection.transaction() as connection:
            row = await db_connection.fetch_one(
                "INSERT INTO moderation.verification_tasks "
                "(user_id, group_id, message_id, expire_time, token_hash, member_name, status, version, "
                "next_attempt_at, claim_token, lease_expires_at, attempt_count, last_error, updated_at) "
                "VALUES (%s, %s, NULL, %s, %s, %s, 'creating', 0, %s, NULL, NULL, 0, NULL, %s) "
                "ON CONFLICT (user_id, group_id) DO UPDATE SET "
                "message_id = NULL, expire_time = EXCLUDED.expire_time, "
                "token_hash = EXCLUDED.token_hash, member_name = EXCLUDED.member_name, "
                "status = 'creating', version = moderation.verification_tasks.version + 1, "
                "next_attempt_at = EXCLUDED.next_attempt_at, claim_token = NULL, "
                "lease_expires_at = NULL, attempt_count = 0, last_error = NULL, "
                "updated_at = EXCLUDED.updated_at "
                "RETURNING user_id, group_id, message_id, expire_time, token_hash, "
                "member_name, status, version",
                (
                    int(task.user_id),
                    int(task.chat_id),
                    _utc(task.expires_at),
                    task.token_hash,
                    task.member_name,
                    _utc(recover_at),
                    _utc(recover_at),
                ),
                connection=connection,
            )
        if row is None:
            raise RuntimeError("verification create did not return a row")
        return _task_from_row(row)

    async def load(self, key: VerificationKey) -> VerificationTask | None:
        """@brief 读取聚合快照 / Load an aggregate snapshot.

        @param key 聚合键 / Aggregate key.
        @return 聚合；不存在时为 None / Aggregate, or None.
        """

        row = await db_connection.fetch_one(
            "SELECT user_id, group_id, message_id, expire_time, token_hash, member_name, status, version "
            "FROM moderation.verification_tasks WHERE user_id = %s AND group_id = %s",
            (int(key.user_id), int(key.chat_id)),
        )
        return _task_from_row(row) if row is not None else None

    async def apply(
        self,
        key: VerificationKey,
        *,
        expected_version: VerificationVersion,
        event: VerificationEvent,
        now: datetime,
        message_id: MessageId | None = None,
    ) -> VerificationTask:
        """@brief 在短事务内锁行并应用纯领域事件 / Lock a row and apply a pure domain event in a short transaction.

        @param key 聚合键 / Aggregate key.
        @param expected_version 调用方版本 / Caller-observed version.
        @param event 领域事件 / Domain event.
        @param now 事件时刻 / Event instant.
        @param message_id ACTIVATE 绑定消息 / Message bound by ACTIVATE.
        @return 更新后聚合 / Updated aggregate.
        """

        timestamp = _utc(now)
        async with db_connection.transaction() as connection:
            row = await db_connection.fetch_one(
                "SELECT user_id, group_id, message_id, expire_time, token_hash, member_name, status, version "
                "FROM moderation.verification_tasks WHERE user_id = %s AND group_id = %s FOR UPDATE",
                (int(key.user_id), int(key.chat_id)),
                connection=connection,
            )
            if row is None:
                raise VerificationNotFound("verification does not exist")
            current = _task_from_row(row)
            if current.version != expected_version:
                raise StaleVerificationVersion(
                    f"verification is version {current.version.value}, not {expected_version.value}"
                )
            updated = current.evolve(
                event,
                expected_version=expected_version,
                now=timestamp,
                message_id=message_id,
            )
            next_attempt_at = _next_attempt(updated, timestamp)
            changed = await db_connection.execute(
                "UPDATE moderation.verification_tasks SET message_id = %s, status = %s, version = %s, "
                "next_attempt_at = %s, claim_token = NULL, lease_expires_at = NULL, "
                "last_error = NULL, updated_at = %s "
                "WHERE user_id = %s AND group_id = %s AND version = %s",
                (
                    int(updated.message_id) if updated.message_id is not None else None,
                    updated.status.value,
                    updated.version.value,
                    next_attempt_at,
                    timestamp,
                    int(key.user_id),
                    int(key.chat_id),
                    expected_version.value,
                ),
                connection=connection,
            )
            if changed != 1:
                raise StaleVerificationVersion("verification changed during transition")
        return updated

    async def claim_ready(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[VerificationClaim, ...]:
        """@brief 以 SKIP LOCKED 有界领取到期工作 / Claim a bounded due batch with SKIP LOCKED.

        @param now 当前时刻 / Current instant.
        @param limit 最大领取数 / Maximum claims.
        @param lease_for 租约时长 / Lease duration.
        @return fencing claims / Fencing claims.
        """

        return await self._claim(now=now, limit=limit, lease_for=lease_for, key=None)

    async def claim_one(
        self,
        key: VerificationKey,
        *,
        now: datetime,
        lease_for: timedelta,
    ) -> VerificationClaim | None:
        """@brief 领取指定聚合的就绪副作用 / Claim a ready effect for one aggregate.

        @param key 聚合键 / Aggregate key.
        @param now 当前时刻 / Current instant.
        @param lease_for 租约时长 / Lease duration.
        @return claim；未就绪或已被领取时为 None / Claim, or None when not ready/already claimed.
        """

        claims = await self._claim(now=now, limit=1, lease_for=lease_for, key=key)
        return claims[0] if claims else None

    async def complete(
        self, claim: VerificationClaim, *, now: datetime
    ) -> VerificationTask:
        """@brief 以 fencing token 确认副作用并进入终态 / Acknowledge effects with fencing and enter a terminal state.

        @param claim 当前 claim / Current claim.
        @param now 完成时刻 / Completion instant.
        @return 终态聚合 / Terminal aggregate.
        """

        timestamp = _utc(now)
        updated = claim.task.evolve(
            VerificationEvent.EFFECT_DELIVERED,
            expected_version=claim.task.version,
            now=timestamp,
        )
        changed = await db_connection.execute(
            "UPDATE moderation.verification_tasks SET status = %s, version = %s, "
            "next_attempt_at = NULL, claim_token = NULL, lease_expires_at = NULL, "
            "last_error = NULL, updated_at = %s "
            "WHERE user_id = %s AND group_id = %s AND version = %s "
            "AND claim_token = CAST(%s AS UUID)",
            (
                updated.status.value,
                updated.version.value,
                timestamp,
                int(updated.user_id),
                int(updated.chat_id),
                claim.task.version.value,
                claim.token,
            ),
        )
        if changed != 1:
            raise VerificationFencingError("verification claim is stale")
        return updated

    async def retry(
        self,
        claim: VerificationClaim,
        *,
        retry_at: datetime,
        error: str,
        now: datetime,
    ) -> None:
        """@brief 以 fencing token 释放 claim 并安排重试 / Release a claim with fencing and schedule retry.

        @param claim 当前 claim / Current claim.
        @param retry_at 下次领取时间 / Next claim time.
        @param error 有界错误摘要 / Bounded error summary.
        @param now 当前时刻 / Current instant.
        @return None / None.
        """

        changed = await db_connection.execute(
            "UPDATE moderation.verification_tasks SET next_attempt_at = %s, claim_token = NULL, "
            "lease_expires_at = NULL, last_error = %s, updated_at = %s "
            "WHERE user_id = %s AND group_id = %s AND version = %s "
            "AND claim_token = CAST(%s AS UUID)",
            (
                _utc(retry_at),
                error[:1000],
                _utc(now),
                int(claim.task.user_id),
                int(claim.task.chat_id),
                claim.task.version.value,
                claim.token,
            ),
        )
        if changed != 1:
            raise VerificationFencingError("verification claim is stale")

    async def recover_expired_leases(self, *, now: datetime) -> int:
        """@brief 回收崩溃 worker 留下的过期租约 / Recover expired leases left by crashed workers.

        @param now 当前时刻 / Current instant.
        @return 回收行数 / Number of recovered rows.
        """

        timestamp = _utc(now)
        return await db_connection.execute(
            "UPDATE moderation.verification_tasks SET claim_token = NULL, lease_expires_at = NULL, "
            "next_attempt_at = %s, last_error = 'recovered expired verification lease', updated_at = %s "
            "WHERE claim_token IS NOT NULL AND lease_expires_at <= %s "
            "AND status IN ('passing', 'expiring', 'cancelling')",
            (timestamp, timestamp, timestamp),
        )

    async def _claim(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
        key: VerificationKey | None,
    ) -> tuple[VerificationClaim, ...]:
        """@brief 共享的有界 claim 实现 / Shared bounded claim implementation.

        @param now 当前时刻 / Current instant.
        @param limit 最大领取数 / Maximum claims.
        @param lease_for 租约时长 / Lease duration.
        @param key 可选单聚合过滤 / Optional aggregate filter.
        @return claims / Claims.
        """

        if limit <= 0:
            raise ValueError("claim limit must be positive")
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        timestamp = _utc(now)
        lease_expires_at = timestamp + lease_for
        token = str(uuid.uuid4())
        key_clause = ""
        params: list[object] = [timestamp]
        if key is not None:
            key_clause = " AND user_id = %s AND group_id = %s"
            params.extend((int(key.user_id), int(key.chat_id)))
        params.extend((limit, token, lease_expires_at, timestamp))
        async with db_connection.transaction() as connection:
            rows = await db_connection.fetch_all(
                "WITH candidate AS ("
                "SELECT user_id, group_id FROM moderation.verification_tasks "
                "WHERE next_attempt_at <= %s AND claim_token IS NULL "
                "AND status IN ('creating', 'pending', 'passing', 'expiring', 'cancelling')"
                f"{key_clause} ORDER BY next_attempt_at, group_id, user_id "
                "LIMIT %s FOR UPDATE SKIP LOCKED"
                ") UPDATE moderation.verification_tasks AS task SET "
                "status = CASE task.status WHEN 'creating' THEN 'cancelling' "
                "WHEN 'pending' THEN 'expiring' ELSE task.status END, "
                "version = CASE WHEN task.status IN ('creating', 'pending') "
                "THEN task.version + 1 ELSE task.version END, "
                "claim_token = CAST(%s AS UUID), lease_expires_at = %s, "
                "attempt_count = task.attempt_count + 1, updated_at = %s "
                "FROM candidate WHERE task.user_id = candidate.user_id "
                "AND task.group_id = candidate.group_id "
                "RETURNING task.user_id, task.group_id, task.message_id, task.expire_time, "
                "task.token_hash, task.member_name, task.status, task.version, "
                "task.attempt_count",
                tuple(params),
                connection=connection,
            )
        return tuple(
            VerificationClaim(
                task=_task_from_row(row[:8]),
                token=token,
                lease_expires_at=lease_expires_at,
                attempt_count=int(row[8]),
            )
            for row in rows
        )


def _task_from_row(row: Sequence[object]) -> VerificationTask:
    """@brief 将数据库行映射为领域聚合 / Map a database row to a domain aggregate.

    @param row 八列验证行 / Eight-column verification row.
    @return 验证聚合 / Verification aggregate.
    """

    (
        user_id,
        group_id,
        message_id,
        expires_at,
        token_hash,
        member_name,
        status,
        version,
    ) = row
    return VerificationTask(
        key=VerificationKey(ChatId(_integer(group_id)), UserId(_integer(user_id))),
        version=VerificationVersion(_integer(version)),
        token_hash=_text(token_hash),
        member_name=_text(member_name),
        expires_at=_utc_timestamp(expires_at),
        status=VerificationStatus(_text(status)),
        message_id=MessageId(_integer(message_id)) if message_id is not None else None,
    )


def _next_attempt(task: VerificationTask, now: datetime) -> datetime | None:
    """@brief 从领域状态推导持久化调度时间 / Derive persistence scheduling time from domain state.

    @param task 更新后的聚合 / Updated aggregate.
    @param now 转移时刻 / Transition time.
    @return 下次领取时间；终态为 None / Next claim time, or None for terminal state.
    """

    if task.status is VerificationStatus.PENDING:
        return _utc(task.expires_at)
    if task.status.needs_delivery:
        return _utc(now)
    return None


def _utc(value: datetime) -> datetime:
    """@brief 规范化为 aware UTC / Normalize to aware UTC.

    @param value 时间 / Timestamp.
    @return UTC 时间 / UTC timestamp.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("verification repository requires timezone-aware timestamps")
    return value.astimezone(UTC)


def _utc_timestamp(value: object) -> datetime:
    """@brief 从驱动值恢复 UTC 时间 / Restore UTC time from a driver value.

    @param value 驱动时间值 / Driver timestamp value.
    @return aware UTC 时间 / Aware UTC timestamp.
    """

    if not isinstance(value, datetime):
        raise TypeError("verification timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _text(value: object) -> str:
    """@brief 解码数据库文本 / Decode database text.

    @param value 驱动值 / Driver value.
    @return 文本 / Text.
    """

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return str(value)


def _integer(value: object) -> int:
    """@brief 将数据库标量规范化为整数 / Normalize a database scalar to an integer.

    @param value 驱动标量 / Driver scalar.
    @return 整数 / Integer.
    """

    if isinstance(value, bool):
        raise TypeError("boolean is not a valid verification integer")
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return int(value.decode("ascii", errors="strict"))
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"unsupported verification integer: {type(value).__name__}")
