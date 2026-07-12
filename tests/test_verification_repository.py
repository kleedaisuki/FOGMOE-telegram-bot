"""@brief PostgreSQL 成员验证工作流仓储测试 / PostgreSQL member-verification workflow repository tests."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID

from fogmoe_bot.domain.moderation.models import (
    ChatId,
    MessageId,
    ModerationToggleResult,
    UserId,
)
from fogmoe_bot.domain.moderation.verification import (
    VerificationClaim,
    VerificationEvent,
    VerificationKey,
    VerificationStatus,
    VerificationTask,
    VerificationVersion,
    hash_verification_token,
)
from fogmoe_bot.infrastructure.database.repositories import verification_repository
from fogmoe_bot.infrastructure.database.repositories.verification_repository import (
    PostgresVerificationRepository,
)


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 确定性 UTC 测试时刻 / Deterministic UTC test instant."""

KEY = VerificationKey(ChatId(-1001), UserId(42))
"""@brief 固定验证聚合键 / Fixed verification aggregate key."""

TOKEN_HASH = hash_verification_token("0123456789abcdef")
"""@brief 固定合法 token 摘要 / Fixed valid token digest."""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


class RecordingTransaction:
    """@brief 记录短事务连接与退出异常 / Record a short transaction connection and exit error."""

    def __init__(self) -> None:
        """@brief 初始化事务记录 / Initialize transaction recording.

        @return None / None.
        """

        self.connection = object()
        """@brief 模拟连接身份 / Fake connection identity."""
        self.exit_exception: type[BaseException] | None = None
        """@brief 退出时异常类型 / Exception type observed on exit."""

    async def __aenter__(self) -> object:
        """@brief 进入事务 / Enter the transaction.

        @return 模拟连接 / Fake connection.
        """

        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """@brief 记录事务退出且不吞异常 / Record transaction exit without suppressing errors.

        @param exc_type 异常类型 / Exception type.
        @param exc 异常对象 / Exception object.
        @param traceback 回溯 / Traceback.
        @return False，传播异常 / False to propagate errors.
        """

        del exc, traceback
        self.exit_exception = exc_type
        return False


def _row(
    *,
    status: VerificationStatus,
    version: int,
    message_id: int | None = 7,
) -> tuple[object, ...]:
    """@brief 构造仓储八列聚合行 / Build the repository's eight-column aggregate row.

    @param status 持久化状态 / Persisted status.
    @param version 聚合版本 / Aggregate version.
    @param message_id 可选欢迎消息 ID / Optional welcome-message ID.
    @return 数据库行 / Database row.
    """

    return (
        int(KEY.user_id),
        int(KEY.chat_id),
        message_id,
        NOW + timedelta(minutes=5),
        TOKEN_HASH,
        "Alice",
        status.value,
        version,
    )


def _task(
    *,
    status: VerificationStatus,
    version: int,
    message_id: MessageId | None = MessageId(7),
) -> VerificationTask:
    """@brief 构造领域验证聚合 / Build a domain verification aggregate.

    @param status 生命周期状态 / Lifecycle state.
    @param version 聚合版本 / Aggregate version.
    @param message_id 可选消息 ID / Optional message ID.
    @return 验证聚合 / Verification aggregate.
    """

    return VerificationTask(
        key=KEY,
        version=VerificationVersion(version),
        token_hash=TOKEN_HASH,
        member_name="Alice",
        expires_at=NOW + timedelta(minutes=5),
        status=status,
        message_id=message_id,
    )


def test_create_upserts_the_existing_single_workflow_table(monkeypatch: Any) -> None:
    """@brief 创建在原表原键上 upsert，不引入双写 / Creation upserts the existing table and key without dual writes."""

    captured: dict[str, object] = {}
    transaction = RecordingTransaction()

    async def fake_fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 捕获创建 SQL / Capture creation SQL.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @return CREATING 行 / CREATING row.
        """

        captured["sql"] = sql
        captured["params"] = params
        captured["connection"] = connection
        return _row(status=VerificationStatus.CREATING, version=4, message_id=None)

    monkeypatch.setattr(
        verification_repository.db_connection,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(
        verification_repository.db_connection, "fetch_one", fake_fetch_one
    )
    recover_at = NOW + timedelta(seconds=30)
    created = asyncio.run(
        PostgresVerificationRepository().create(
            _task(status=VerificationStatus.CREATING, version=0, message_id=None),
            recover_at=recover_at,
        )
    )

    sql = str(captured["sql"])
    assert "INSERT INTO moderation.verification_tasks" in sql
    assert "ON CONFLICT (user_id, group_id) DO UPDATE" in sql
    assert "version = moderation.verification_tasks.version + 1" in sql
    assert "verification_tasks_v2" not in sql
    assert created.status is VerificationStatus.CREATING
    assert created.version == VerificationVersion(4)
    assert captured["params"][-2:] == (recover_at, recover_at)
    assert captured["connection"] is transaction.connection
    assert transaction.exit_exception is None


def test_verification_toggle_commits_policy_and_receipt_once(
    monkeypatch: Any,
) -> None:
    """@brief 同一 source key 只切换一次且回放首次结果 / One source key toggles once and replays its first result."""

    transaction = RecordingTransaction()
    calls: list[tuple[str, str, object]] = []
    receipt_reads = 0

    async def fake_fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...] | None:
        """@brief 模拟 advisory locks、回执与空 policy / Simulate advisory locks, receipts, and an empty policy.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前事务 / Current transaction.
        @return 模拟行 / Simulated row.
        """

        nonlocal receipt_reads
        calls.append(("fetch", sql, connection))
        if "pg_advisory_xact_lock" in sql:
            return (None,)
        if "FROM moderation.toggle_command_receipts" in sql:
            receipt_reads += 1
            if receipt_reads == 1:
                return None
            return (
                "member_verification",
                int(KEY.chat_id),
                42,
                {"group_name": "Test Group"},
                True,
            )
        if "FROM moderation.group_verification" in sql:
            return None
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> int:
        """@brief 记录 policy 与 receipt 写入 / Record policy and receipt writes.

        @param sql SQL 文本 / SQL text.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前事务 / Current transaction.
        @return 一影响行 / One affected row.
        """

        calls.append(("execute", sql, connection))
        return 1

    monkeypatch.setattr(
        verification_repository.db_connection,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(
        verification_repository.db_connection,
        "fetch_one",
        fake_fetch_one,
    )
    monkeypatch.setattr(
        verification_repository.db_connection,
        "execute",
        fake_execute,
    )
    repository = PostgresVerificationRepository()
    key = "telegram-update:77:moderation.verification-toggle"

    async def scenario() -> tuple[ModerationToggleResult, ModerationToggleResult]:
        """@brief 执行首次命令与回放 / Execute the first command and its replay.

        @return 两次结果 / Both results.
        """

        first = await repository.toggle_group(
            KEY.chat_id,
            group_name="Test Group",
            actor_id=UserId(42),
            idempotency_key=key,
        )
        replay = await repository.toggle_group(
            KEY.chat_id,
            group_name="Test Group",
            actor_id=UserId(42),
            idempotency_key=key,
        )
        return first, replay

    first, replay = asyncio.run(scenario())
    assert first.enabled is True and not first.replayed
    assert replay.enabled is True and replay.replayed
    writes = [sql for kind, sql, _connection in calls if kind == "execute"]
    assert (
        sum("INSERT INTO moderation.group_verification" in sql for sql in writes) == 1
    )
    assert (
        sum("INSERT INTO moderation.toggle_command_receipts" in sql for sql in writes)
        == 1
    )
    assert all(
        connection is transaction.connection for _kind, _sql, connection in calls
    )
    assert transaction.exit_exception is None


def test_apply_locks_one_row_and_commits_occ_transition_in_one_short_transaction(
    monkeypatch: Any,
) -> None:
    """@brief apply 在单个短事务中锁行并按版本提交 / Apply locks one row and commits by version in one short transaction."""

    transaction = RecordingTransaction()
    calls: list[tuple[str, str, tuple[object, ...], object | None]] = []

    async def fake_fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回 PENDING 聚合并记录行锁 / Return a PENDING aggregate and record its row lock."""

        calls.append(("fetch", sql, params, connection))
        return _row(status=VerificationStatus.PENDING, version=1)

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> int:
        """@brief 记录 OCC UPDATE / Record the OCC update."""

        calls.append(("execute", sql, params, connection))
        return 1

    monkeypatch.setattr(
        verification_repository.db_connection,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(
        verification_repository.db_connection, "fetch_one", fake_fetch_one
    )
    monkeypatch.setattr(verification_repository.db_connection, "execute", fake_execute)

    updated = asyncio.run(
        PostgresVerificationRepository().apply(
            KEY,
            expected_version=VerificationVersion(1),
            event=VerificationEvent.PASS_REQUESTED,
            now=NOW,
        )
    )

    assert updated.status is VerificationStatus.PASSING
    assert updated.version == VerificationVersion(2)
    assert calls[0][0] == "fetch"
    assert calls[0][1].endswith("FOR UPDATE")
    assert calls[1][0] == "execute"
    assert "WHERE user_id = %s AND group_id = %s AND version = %s" in calls[1][1]
    assert "SKIP LOCKED" not in calls[0][1] + calls[1][1]
    assert calls[0][3] is transaction.connection
    assert calls[1][3] is transaction.connection
    assert calls[1][2][1:4] == (VerificationStatus.PASSING.value, 2, NOW)
    assert transaction.exit_exception is None


def test_claim_ready_is_bounded_and_uses_skip_locked_with_lease_fencing(
    monkeypatch: Any,
) -> None:
    """@brief claim 使用有界 SKIP LOCKED 与 UUID fencing / Claim uses bounded SKIP LOCKED and UUID fencing."""

    captured: dict[str, object] = {}
    transaction = RecordingTransaction()

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 捕获 claim SQL 并返回 EXPIRING 行 / Capture claim SQL and return an EXPIRING row."""

        captured["sql"] = sql
        captured["params"] = params
        captured["connection"] = connection
        return [(*_row(status=VerificationStatus.EXPIRING, version=2), 3)]

    monkeypatch.setattr(
        verification_repository.db_connection,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(
        verification_repository.db_connection, "fetch_all", fake_fetch_all
    )
    claims = asyncio.run(
        PostgresVerificationRepository().claim_ready(
            now=NOW,
            limit=8,
            lease_for=timedelta(seconds=30),
        )
    )

    assert len(claims) == 1
    claim = claims[0]
    assert claim.task.status is VerificationStatus.EXPIRING
    assert claim.attempt_count == 3
    assert claim.lease_expires_at == NOW + timedelta(seconds=30)
    assert UUID(claim.token)
    sql = str(captured["sql"])
    params = captured["params"]
    assert isinstance(params, tuple)
    assert "LIMIT %s FOR UPDATE SKIP LOCKED" in sql
    assert "WHEN 'pending' THEN 'expiring'" in sql
    assert "claim_token = CAST(%s AS UUID)" in sql
    assert params[1] == 8
    assert UUID(str(params[2])) == UUID(claim.token)
    assert captured["connection"] is transaction.connection
    assert transaction.exit_exception is None


def test_complete_retry_and_recovery_all_require_or_preserve_fencing(
    monkeypatch: Any,
) -> None:
    """@brief ack/retry 使用版本与 token fencing，恢复只释放过期 lease / Ack/retry fence by version and token; recovery releases only expired leases."""

    calls: list[tuple[str, tuple[object, ...]]] = []

    async def fake_execute(sql: str, params: tuple[object, ...]) -> int:
        """@brief 捕获 fencing SQL / Capture fencing SQL."""

        calls.append((sql, params))
        return 1

    monkeypatch.setattr(verification_repository.db_connection, "execute", fake_execute)
    claim = VerificationClaim(
        task=_task(status=VerificationStatus.PASSING, version=2),
        token="11111111-1111-4111-8111-111111111111",
        lease_expires_at=NOW + timedelta(seconds=30),
        attempt_count=2,
    )
    repository = PostgresVerificationRepository()

    completed = asyncio.run(repository.complete(claim, now=NOW))
    asyncio.run(
        repository.retry(
            claim,
            retry_at=NOW + timedelta(seconds=2),
            error="network error",
            now=NOW,
        )
    )
    recovered = asyncio.run(repository.recover_expired_leases(now=NOW))

    assert completed.status is VerificationStatus.PASSED
    assert completed.version == VerificationVersion(3)
    assert recovered == 1
    complete_sql, complete_params = calls[0]
    retry_sql, retry_params = calls[1]
    recovery_sql, recovery_params = calls[2]
    assert "AND version = %s AND claim_token = CAST(%s AS UUID)" in complete_sql
    assert "AND version = %s AND claim_token = CAST(%s AS UUID)" in retry_sql
    assert complete_params[-1] == claim.token
    assert retry_params[-1] == claim.token
    assert "lease_expires_at <= %s" in recovery_sql
    assert "status IN ('passing', 'expiring', 'cancelling')" in recovery_sql
    assert recovery_params == (NOW, NOW, NOW)


def test_skip_locked_is_confined_to_claim_and_migration_evolves_the_old_table() -> None:
    """@brief SKIP LOCKED 仅存在于 claim；0017 原位演进旧表 / SKIP LOCKED is claim-only and 0017 evolves the old table in place."""

    repository_type = PostgresVerificationRepository
    assert "SKIP LOCKED" in inspect.getsource(repository_type._claim)
    for method_name in (
        "create",
        "load",
        "apply",
        "complete",
        "retry",
        "recover_expired_leases",
    ):
        assert "SKIP LOCKED" not in inspect.getsource(
            getattr(repository_type, method_name)
        )

    migration = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0017_verification_workflow.sql"
    ).read_text(encoding="utf-8")
    normalized = migration.lower()
    assert "alter table moderation.verification_tasks" in normalized
    assert "create table moderation.verification_tasks" not in normalized
    assert "verification_tasks_v2" not in normalized
    assert "add column status" in normalized
    assert "add column version" in normalized
    assert "add column claim_token" in normalized
    assert "add column lease_expires_at" in normalized
    assert "verification_tasks_schedule_ck" in normalized
    assert "verification_tasks_ready_idx" in normalized
    assert "skip locked" not in normalized
