"""@brief Scheduled-Assistant PostgreSQL adapter 契约测试 / Contract tests for Scheduled-Assistant PostgreSQL adapters."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.conversation.workflow import PreparedTurnAcceptance
from fogmoe_bot.application.scheduling.assistant_ports import ScheduleDefinition
from fogmoe_bot.domain.conversation.identity import ConversationId, DeliveryStreamId
from fogmoe_bot.domain.scheduling.assistant_schedule import (
    CalendarDaily,
    FixedInterval,
    MisfirePolicy,
    OneShot,
    ScheduleClaim,
    ScheduleStatus,
    ScheduleTarget,
    StaleScheduleClaimError,
)
from fogmoe_bot.domain.temporal import TimeZoneId
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_workflow.turn import (
    PostgresTurnRepository,
)
from fogmoe_bot.infrastructure.scheduling import postgres
from fogmoe_bot.infrastructure.scheduling.postgres import (
    PostgresScheduleCatalog,
    PostgresScheduledOccurrenceAcceptance,
    PostgresScheduleQueue,
)


NOW = datetime(2030, 1, 2, 12, tzinfo=UTC)
"""@brief 测试共享 UTC 时刻 / UTC instant shared by tests."""


class RecordingTransaction:
    """@brief 记录事务使用及退出异常 / Record transaction use and its exit exception."""

    def __init__(self) -> None:
        """@brief 初始化唯一连接 marker / Initialize a unique connection marker."""

        self.connection = object()
        """@brief 事务连接 marker / Transaction connection marker."""
        self.exit_exception: type[BaseException] | None = None
        """@brief ``__aexit__`` 观测的异常类型 / Exception type observed by ``__aexit__``."""

    async def __aenter__(self) -> object:
        """@brief 返回稳定连接 marker / Return the stable connection marker."""

        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        """@brief 记录异常并保持传播 / Record and preserve exception propagation."""

        del exc, traceback
        self.exit_exception = exc_type
        return False


class RecordingTurns:
    """@brief 记录 connection-bound Turn acceptance 的测试替身 / Test double recording connection-bound Turn acceptance."""

    def __init__(self, *, error: Exception | None = None) -> None:
        """@brief 注入可选 Turn 错误 / Inject an optional Turn error."""

        self.error = error
        """@brief 可选失败 / Optional failure."""
        self.calls: list[tuple[object, object, object, object, datetime]] = []
        """@brief 记录的 acceptance 调用 / Recorded acceptance calls."""

    async def create_and_accept_turn_in_transaction(
        self,
        connection: object,
        turn: object,
        *,
        message: object,
        activity: object,
        accepted_at: datetime,
    ) -> object:
        """@brief 记录调用或抛出注入错误 / Record a call or raise the injected error."""

        self.calls.append((connection, turn, message, activity, accepted_at))
        if self.error is not None:
            raise self.error
        return object()


def _row(**overrides: object) -> dict[str, object]:
    """@brief 构造完整 one-shot 数据库映射行 / Build a complete one-shot database mapping row."""

    values: dict[str, object] = {
        "schedule_id": 11,
        "creator_user_id": 42,
        "target_kind": "private",
        "target_chat_id": 42,
        "target_thread_id": None,
        "target_conversation_id": "assistant-user:42",
        "target_delivery_stream_id": "telegram:primary:chat:42:thread:0",
        "trigger_reason": "reminder",
        "context_snapshot": None,
        "instruction": "drink water",
        "cadence_kind": "one_shot",
        "fixed_interval_seconds": None,
        "calendar_interval": None,
        "calendar_anchor_date": None,
        "calendar_local_time": None,
        "calendar_weekday_mask": None,
        "time_zone": "Asia/Shanghai",
        "next_run_at": NOW - timedelta(minutes=1),
        "misfire_policy": "fire_once",
        "misfire_grace_seconds": None,
        "status": "pending",
        "version": 0,
        "attempt_count": 0,
        "next_attempt_at": NOW - timedelta(minutes=1),
        "claim_token": None,
        "lease_expires_at": None,
        "last_accepted_for": None,
        "last_accepted_at": None,
        "misfire_count": 0,
        "last_error": None,
        "created_at": NOW - timedelta(days=1),
        "updated_at": NOW - timedelta(days=1),
        "terminal_at": None,
    }
    values.update(overrides)
    return values


def _definition(*, cadence: object | None = None) -> ScheduleDefinition:
    """@brief 构造未来 schedule 定义 / Build a future schedule definition."""

    return ScheduleDefinition(
        creator_user_id=42,
        target=ScheduleTarget(
            conversation_id=ConversationId("assistant-user:42"),
            delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
            chat_id=42,
            is_group=False,
        ),
        trigger_reason="reminder",
        instruction="drink water",
        cadence=cast(Any, cadence if cadence is not None else OneShot()),
        first_run_at=NOW + timedelta(hours=1),
        time_zone=TimeZoneId("Asia/Shanghai"),
    )


def _claim(*, cadence: object | None = None) -> ScheduleClaim:
    """@brief 构造已到期 fenced claim / Build a due fenced claim."""

    row = _row(status="processing", attempt_count=1)
    if isinstance(cadence, FixedInterval):
        row.update(
            cadence_kind="fixed_interval",
            fixed_interval_seconds=int(cadence.every.total_seconds()),
        )
    schedule = postgres._schedule_from_row(row)
    return ScheduleClaim(
        schedule=schedule,
        attempt_count=1,
        token=uuid4(),
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
    )


def test_catalog_binds_every_crud_query_to_the_caller_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Catalog 锁、配额与 CRUD 必须共享调用方连接 / Catalog locking, quota, and CRUD share the caller connection."""

    connection = object()
    calls: list[tuple[str, object, object]] = []

    async def fake_fetch_one(
        sql: str,
        params: object = None,
        *,
        mapping: bool = False,
        connection: object = None,
    ) -> object:
        """@brief 记录单行 SQL 并返回契约行 / Record single-row SQL and return contract rows."""

        del mapping
        calls.append((sql, params, connection))
        if "pg_advisory_xact_lock" in sql:
            return (None,)
        if "COUNT(*)" in sql:
            return (2,)
        if "RETURNING" in sql:
            return _row(next_run_at=NOW + timedelta(hours=1))
        raise AssertionError(sql)

    async def fake_fetch_all(
        sql: str,
        params: object = None,
        *,
        mapping: bool = False,
        connection: object = None,
    ) -> list[dict[str, object]]:
        """@brief 记录 list SQL / Record list SQL."""

        del mapping
        calls.append((sql, params, connection))
        return [_row()]

    async def fake_execute(
        sql: str,
        params: object = None,
        *,
        connection: object = None,
    ) -> int:
        """@brief 记录 cancel SQL / Record cancel SQL."""

        calls.append((sql, params, connection))
        return 1

    monkeypatch.setattr(db_connection, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(db_connection, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(db_connection, "execute", fake_execute)
    catalog = PostgresScheduleCatalog(cast(AsyncConnection, connection))
    definition = _definition()

    async def exercise() -> None:
        """@brief 执行 catalog 全部端口 / Exercise every catalog endpoint."""

        await catalog.lock_scope(42, "assistant-user:42")
        count = await catalog.count_active(42, "assistant-user:42")
        created = await catalog.create(definition, created_at=NOW)
        replaced = await catalog.replace(11, definition, updated_at=NOW)
        listed = await catalog.list(
            creator_user_id=42,
            conversation_id="assistant-user:42",
            limit=10,
        )
        cancelled = await catalog.cancel(
            schedule_id=11,
            creator_user_id=42,
            conversation_id="assistant-user:42",
            cancelled_at=NOW,
        )
        assert count == 2
        assert created.schedule_id == 11
        assert replaced is not None
        assert len(listed) == 1
        assert cancelled is True

    asyncio.run(exercise())
    assert all(item[2] is connection for item in calls)
    assert all(
        "scheduling.assistant_schedules" in sql or "pg_advisory" in sql
        for sql, _, _ in calls
    )
    insert_call = next(call for call in calls if call[0].startswith("INSERT"))
    assert "calendar_anchor_date" in insert_call[0]
    cancel_call = next(call for call in calls if "status = 'cancelled'" in call[0])
    assert "claim_token = NULL" in cancel_call[0]


def test_calendar_storage_round_trip_and_weekday_bitmap_are_explicit() -> None:
    """@brief Calendar cadence 必须保留本地时间、时区和 anchor / Calendar cadence preserves local time, zone, and anchor."""

    zone = TimeZoneId("Asia/Shanghai")
    cadence = CalendarDaily(local_time=time(9, 30), time_zone=zone, interval=2)
    occurrence = zone.resolve_local(datetime(2030, 1, 3, 9, 30))
    storage = postgres._cadence_storage(cadence, next_run_at=occurrence)
    row = _row(
        cadence_kind="calendar_daily",
        calendar_interval=2,
        calendar_anchor_date=date(2030, 1, 3),
        calendar_local_time=time(9, 30),
        next_run_at=occurrence,
    )

    restored = postgres._schedule_from_row(row)
    assert storage.calendar_anchor_date == date(2030, 1, 3)
    assert restored.cadence == cadence
    assert postgres._encode_weekdays(frozenset({1, 7})) == 65
    assert postgres._decode_weekdays(65) == frozenset({1, 7})


def test_claim_due_uses_skip_locked_head_of_line_and_distinct_uuid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Claim SQL 必须跳过锁并串行化同 Conversation / Claim SQL skips locks and serializes a Conversation."""

    transaction = RecordingTransaction()
    calls: list[tuple[str, tuple[object, ...], object]] = []
    returned = False

    async def fake_fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        mapping: bool = False,
        connection: object = None,
    ) -> object:
        """@brief 首次返回 claim 行，其后表示队列为空 / Return one claim row and then an empty queue."""

        del mapping
        nonlocal returned
        calls.append((sql, params, connection))
        if returned:
            return None
        returned = True
        return _row(status="processing", attempt_count=1)

    monkeypatch.setattr(db_connection, "transaction", lambda: transaction)
    monkeypatch.setattr(db_connection, "fetch_one", fake_fetch_one)
    claims = asyncio.run(
        PostgresScheduleQueue().claim_due(
            now=NOW,
            limit=2,
            lease_for=timedelta(seconds=30),
        )
    )

    assert len(claims) == 1
    assert isinstance(claims[0].token, UUID)
    assert claims[0].attempt_count == 1
    assert all(call[2] is transaction.connection for call in calls)
    sql = calls[0][0]
    assert "FOR UPDATE OF schedule SKIP LOCKED" in sql
    assert "running.status = 'processing'" in sql
    assert "earlier.target_conversation_id" in sql
    assert "scheduling.assistant_schedules" in sql
    assert calls[0][1][2] == claims[0].token


def test_every_claim_finalization_write_is_token_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 任何未命中 token 的终结写都必须报 stale / Every finalization write missing its token reports stale."""

    captured: list[tuple[str, object]] = []

    async def fake_execute(
        sql: str,
        params: object = None,
        *,
        connection: object = None,
    ) -> int:
        """@brief 记录 fencing SQL 并模拟零行命中 / Record fencing SQL and simulate a zero-row match."""

        del connection
        captured.append((sql, params))
        return 0

    monkeypatch.setattr(db_connection, "execute", fake_execute)
    claim = _claim()
    with pytest.raises(StaleScheduleClaimError):
        asyncio.run(
            PostgresScheduleQueue().retry(
                claim,
                retry_at=NOW + timedelta(seconds=5),
                failed_at=NOW,
                error="temporary",
            )
        )
    assert "status = 'processing'" in captured[0][0]
    assert "claim_token = %s" in captured[0][0]
    assert claim.token in cast(tuple[object, ...], captured[0][1])


def test_acceptance_locks_claim_accepts_turn_and_advances_cursor_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Turn acceptance 与 schedule cursor 必须使用同一事务 / Turn acceptance and schedule cursor share one transaction."""

    transaction = RecordingTransaction()
    turns = RecordingTurns()
    executed: list[tuple[str, object, object]] = []

    async def fake_fetch_one(
        sql: str,
        params: object = None,
        *,
        mapping: bool = False,
        connection: object = None,
    ) -> object:
        """@brief 模拟 token 行锁命中 / Simulate a matching token row lock."""

        del params, mapping
        assert "FOR UPDATE" in sql
        assert connection is transaction.connection
        return (11,)

    async def fake_execute(
        sql: str,
        params: object = None,
        *,
        connection: object = None,
    ) -> int:
        """@brief 记录 cursor 推进 / Record cursor advancement."""

        executed.append((sql, params, connection))
        return 1

    monkeypatch.setattr(db_connection, "transaction", lambda: transaction)
    monkeypatch.setattr(db_connection, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(db_connection, "execute", fake_execute)
    claim = _claim(cadence=FixedInterval(timedelta(hours=1)))
    prepared = cast(
        PreparedTurnAcceptance,
        SimpleNamespace(
            turn=object(),
            message=object(),
            activity=object(),
            accepted_at=NOW,
        ),
    )

    asyncio.run(
        PostgresScheduledOccurrenceAcceptance(
            cast(PostgresTurnRepository, turns)
        ).accept(
            claim,
            prepared,
            next_run_at=NOW + timedelta(hours=1),
            accepted_at=NOW,
        )
    )

    assert turns.calls[0][0] is transaction.connection
    assert executed[0][2] is transaction.connection
    assert "last_accepted_for = %s" in executed[0][0]
    assert "claim_token = %s" in executed[0][0]
    assert transaction.exit_exception is None


def test_acceptance_rolls_back_schedule_when_turn_acceptance_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Turn 失败必须在 cursor 写入前退出同一事务 / Turn failure exits the transaction before any cursor write."""

    transaction = RecordingTransaction()
    turns = RecordingTurns(error=RuntimeError("turn failed"))
    writes: list[str] = []

    async def fake_fetch_one(
        sql: str,
        params: object = None,
        *,
        mapping: bool = False,
        connection: object = None,
    ) -> object:
        """@brief 模拟锁定当前 claim / Simulate locking the current claim."""

        del sql, params, mapping
        assert connection is transaction.connection
        return (11,)

    async def unexpected_execute(
        sql: str,
        params: object = None,
        *,
        connection: object = None,
    ) -> int:
        """@brief 捕获不应发生的 cursor 写入 / Capture an unexpected cursor write."""

        del params, connection
        writes.append(sql)
        return 1

    monkeypatch.setattr(db_connection, "transaction", lambda: transaction)
    monkeypatch.setattr(db_connection, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(db_connection, "execute", unexpected_execute)
    claim = _claim()
    prepared = cast(
        PreparedTurnAcceptance,
        SimpleNamespace(
            turn=object(),
            message=object(),
            activity=object(),
            accepted_at=NOW,
        ),
    )

    with pytest.raises(RuntimeError, match="turn failed"):
        asyncio.run(
            PostgresScheduledOccurrenceAcceptance(
                cast(PostgresTurnRepository, turns)
            ).accept(
                claim,
                prepared,
                next_run_at=None,
                accepted_at=NOW,
            )
        )
    assert writes == []
    assert transaction.exit_exception is RuntimeError


def test_acceptance_rejects_a_stale_claim_before_creating_a_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Token 失效时禁止创建 Turn / A stale token prevents Turn creation."""

    transaction = RecordingTransaction()
    turns = RecordingTurns()

    async def fake_fetch_one(
        sql: str,
        params: object = None,
        *,
        mapping: bool = False,
        connection: object = None,
    ) -> None:
        """@brief 模拟 fencing 谓词未命中 / Simulate a fencing predicate miss."""

        del sql, params, mapping, connection
        return None

    monkeypatch.setattr(db_connection, "transaction", lambda: transaction)
    monkeypatch.setattr(db_connection, "fetch_one", fake_fetch_one)
    claim = _claim()
    prepared = cast(
        PreparedTurnAcceptance,
        SimpleNamespace(
            turn=object(),
            message=object(),
            activity=object(),
            accepted_at=NOW,
        ),
    )

    with pytest.raises(StaleScheduleClaimError):
        asyncio.run(
            PostgresScheduledOccurrenceAcceptance(
                cast(PostgresTurnRepository, turns)
            ).accept(
                claim,
                prepared,
                next_run_at=None,
                accepted_at=NOW,
            )
        )
    assert turns.calls == []


def test_misfire_expiration_is_terminal_and_token_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 一次性 misfire 过期必须进入 terminal expired / An exhausted one-shot misfire becomes terminal expired."""

    captured: list[tuple[str, tuple[object, ...]]] = []

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object = None,
    ) -> int:
        """@brief 记录 expired 写入 / Record the expired write."""

        del connection
        captured.append((sql, params))
        return 1

    monkeypatch.setattr(db_connection, "execute", fake_execute)
    claim = _claim()
    asyncio.run(
        PostgresScheduleQueue().skip_misfire(
            claim,
            next_run_at=None,
            skipped_at=NOW,
        )
    )
    sql, params = captured[0]
    assert "status = 'expired'" in sql
    assert "terminal_at = %s" in sql
    assert "claim_token = %s" in sql
    assert claim.token in params
    assert ScheduleStatus.EXPIRED.value == "expired"
    assert MisfirePolicy.SKIP.value == "skip"
