"""@brief PostgreSQL transactional outbox adapter 测试 / PostgreSQL transactional-outbox adapter tests."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from fogmoe_bot.domain.conversation.identity import (
    LeaseToken,
    OutboundMessageId,
    TurnId,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundClaim,
    OutboundDraft,
    OutboundStatus,
)
from fogmoe_bot.domain.conversation.errors import (
    IdempotencyConflictError,
    StaleClaimError,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_workflow import (
    outbox as outbox_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow import turn_uow
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)

from conversation_workflow_testkit import (
    NOW,
    TURN_UUID,
    _outbound_claim_row,
    _outbound_draft,
    _outbound_row,
    _standalone_outbound_draft,
    _TransactionContext,
    _turn_row,
)


def test_claim_outbound_uses_skip_locked_fencing_and_delivery_stream_head(
    monkeypatch: Any,
) -> None:
    """@brief outbox 领取使用 SKIP LOCKED、租约 token 与投递流头 / Outbox claim uses SKIP LOCKED, lease token, and delivery-stream head."""

    connection = object()
    calls: list[tuple[str, tuple[object, ...], object]] = []
    repository = PostgresOutboxRepository()

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 捕获领取 SQL / Capture claim SQL."""

        calls.append((sql, params, connection))
        return [_outbound_claim_row(previous_status="pending")]

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回等待投递的关联回合 / Return the associated turn waiting for delivery."""

        return turn_uow._map_turn(
            _turn_row(
                state="waiting_delivery",
                version=4,
                inference_attempts=1,
                delivery_attempts=1,
            )
        )

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db_connection, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(outbox_repository, "_load_turn_for_mutation", fake_load_turn)

    claims = asyncio.run(
        repository.claim_outbound(
            now=NOW,
            limit=5,
            lease_for=timedelta(seconds=30),
        )
    )

    assert len(claims) == 1
    assert claims[0].message.status is OutboundStatus.PROCESSING
    assert int(claims[0].message.stream_sequence) == 7
    assert claims[0].lease_expires_at == NOW + timedelta(seconds=30)
    sql, params, used_connection = calls[0]
    assert "FOR UPDATE OF candidate SKIP LOCKED" in sql
    assert "earlier.delivery_stream_id = candidate.delivery_stream_id" in sql
    assert "earlier.stream_sequence < candidate.stream_sequence" in sql
    assert used_connection is connection
    assert UUID(str(params[2])) == claims[0].token.value


def test_claim_standalone_outbound_does_not_load_or_transition_a_turn(
    monkeypatch: Any,
) -> None:
    """@brief standalone claim 只推进 outbox 自身 / A standalone claim advances only the outbox itself."""

    connection = object()
    repository = PostgresOutboxRepository()

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回无 Turn 的已领取行 / Return a claimed row without a Turn."""

        del sql, params, connection
        return [
            _outbound_claim_row(
                previous_status="pending",
                turn_id=None,
            )
        ]

    async def unexpected_turn_load(*args: object, **kwargs: object) -> object:
        """@brief 拒绝 standalone 查询 Turn / Reject a Turn lookup for a standalone row."""

        del args, kwargs
        raise AssertionError("standalone claim loaded a Turn")

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db_connection, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        outbox_repository, "_load_turn_for_mutation", unexpected_turn_load
    )

    claims = asyncio.run(
        repository.claim_outbound(
            now=NOW,
            limit=1,
            lease_for=timedelta(seconds=30),
        )
    )

    assert len(claims) == 1
    assert claims[0].message.turn_id is None
    assert claims[0].message.status is OutboundStatus.PROCESSING


def test_claim_retry_outbound_atomically_resumes_delivery_turn(
    monkeypatch: Any,
) -> None:
    """@brief 重领 outbox retry row 会在同事务恢复投递回合 / Reclaiming an outbox retry row resumes its delivery turn in the same transaction."""

    connection = object()
    repository = PostgresOutboxRepository()
    captured: dict[str, object] = {}

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回 retry_wait 领取结果 / Return a claimed retry_wait row."""

        return [_outbound_claim_row(previous_status="retry_wait")]

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回等待投递重试的回合 / Return a turn waiting for a delivery retry."""

        return turn_uow._map_turn(
            _turn_row(
                state="delivery_retry_wait",
                version=5,
                inference_attempts=1,
                delivery_attempts=1,
                next_retry_at=NOW,
                last_error="rate limited",
            )
        )

    async def fake_persist(
        turn: object,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 捕获恢复后的回合 / Capture the resumed turn."""

        captured["turn"] = turn
        captured["version"] = expected_version
        captured["connection"] = connection

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db_connection, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(outbox_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(outbox_repository, "_persist_turn", fake_persist)

    claims = asyncio.run(
        repository.claim_outbound(
            now=NOW,
            limit=1,
            lease_for=timedelta(seconds=30),
        )
    )

    resumed = captured["turn"]
    assert len(claims) == 1
    assert getattr(resumed, "state").value == "waiting_delivery"
    assert getattr(resumed, "delivery_attempts") == 2
    assert captured["version"] == 5
    assert captured["connection"] is connection


def test_transactional_outbox_allocates_stream_sequence_under_advisory_lock(
    monkeypatch: Any,
) -> None:
    """@brief outbox 入队在事务锁内分配投递流序号 / Outbox enqueue allocates stream sequence under a transaction lock."""

    connection = object()
    sql_calls: list[str] = []
    responses: list[object | None] = [
        None,
        (None,),
        (None,),
        None,
        (8,),
        _outbound_row(status="pending"),
    ]

    async def fake_fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> object | None:
        """@brief 按顺序返回查询结果 / Return query results in order."""

        sql_calls.append(sql)
        return responses.pop(0)

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db_connection, "fetch_one", fake_fetch_one)
    draft = _outbound_draft()

    result = asyncio.run(
        PostgresOutboxRepository().enqueue_outbound_in_transaction(
            connection,
            draft,
        )
    )

    assert result.inserted is True
    assert sum("pg_advisory_xact_lock" in sql for sql in sql_calls) == 2
    assert any("MAX(stream_sequence)" in sql for sql in sql_calls)
    assert any("delivery_stream_id, stream_sequence" in sql for sql in sql_calls)


def test_standalone_outbox_uses_one_short_transaction_and_persists_null_turn(
    monkeypatch: Any,
) -> None:
    """@brief standalone 入队复用 outbox 且显式持久化 NULL Turn / Standalone enqueue reuses the outbox and persists a NULL Turn."""

    connection = object()
    transaction = _TransactionContext(connection)
    draft = _standalone_outbound_draft()
    responses: list[object | None] = [
        None,
        (None,),
        (None,),
        None,
        (1,),
        (
            draft.message_id.value,
            str(draft.conversation_id),
            None,
            str(draft.delivery_stream_id),
            1,
            draft.kind.value,
            draft.payload,
            draft.idempotency_key,
            "pending",
            0,
            0,
            draft.created_at,
            draft.created_at,
            draft.created_at,
            None,
            None,
            None,
        ),
    ]
    insert_params: tuple[object, ...] | None = None

    async def fake_fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> object | None:
        """@brief 返回确定性查询序列并捕获 INSERT / Return deterministic rows and capture the INSERT."""

        nonlocal insert_params
        if "INSERT INTO conversation.outbound_messages" in sql:
            insert_params = params
        return responses.pop(0)

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(db_connection, "fetch_one", fake_fetch_one)

    result = asyncio.run(PostgresOutboxRepository().enqueue_standalone_outbound(draft))

    assert result.inserted is True
    assert result.message.turn_id is None
    assert insert_params is not None
    assert insert_params[2] is None
    assert transaction.exception is None


def test_standalone_outbox_replay_and_conflict_share_semantic_validator(
    monkeypatch: Any,
) -> None:
    """@brief standalone 同语义重放收敛、异载荷冲突 / Standalone replay converges while a changed payload conflicts."""

    draft = _standalone_outbound_draft()
    row = (
        draft.message_id.value,
        str(draft.conversation_id),
        None,
        str(draft.delivery_stream_id),
        1,
        draft.kind.value,
        draft.payload,
        draft.idempotency_key,
        "pending",
        0,
        0,
        draft.created_at,
        draft.created_at,
        draft.created_at,
        None,
        None,
        None,
    )

    async def fake_find(
        requested: OutboundDraft,
        *,
        connection: object,
    ) -> tuple[object, ...]:
        """@brief 返回规范 standalone 行 / Return the canonical standalone row."""

        del requested, connection
        return row

    repository = PostgresOutboxRepository()
    monkeypatch.setattr(repository, "_find_outbound", fake_find)

    replay = asyncio.run(
        repository.enqueue_standalone_outbound_in_transaction(
            object(),  # type: ignore[arg-type]
            draft,
        )
    )
    assert replay.inserted is False

    changed = OutboundDraft(
        message_id=draft.message_id,
        conversation_id=draft.conversation_id,
        turn_id=None,
        delivery_stream_id=draft.delivery_stream_id,
        kind=draft.kind,
        payload={"chat_id": -100, "text": "different"},
        idempotency_key=draft.idempotency_key,
        created_at=draft.created_at,
    )
    with pytest.raises(IdempotencyConflictError, match="different semantics"):
        asyncio.run(
            repository.enqueue_standalone_outbound_in_transaction(
                object(),  # type: ignore[arg-type]
                changed,
            )
        )


def test_standalone_outbox_rejects_turn_or_noncanonical_message_id() -> None:
    """@brief standalone primitive 拒绝伪装 Turn 与随机 ID / Standalone primitive rejects a Turn reference and a random ID."""

    repository = PostgresOutboxRepository()
    standalone = _standalone_outbound_draft()
    with_turn = OutboundDraft(
        message_id=OutboundMessageId.for_turn(TurnId(TURN_UUID), "feedback"),
        conversation_id=standalone.conversation_id,
        turn_id=TurnId(TURN_UUID),
        delivery_stream_id=standalone.delivery_stream_id,
        kind=standalone.kind,
        payload=standalone.payload,
        idempotency_key=standalone.idempotency_key,
        created_at=standalone.created_at,
    )
    with pytest.raises(ValueError, match="cannot reference a Turn"):
        asyncio.run(
            repository.enqueue_standalone_outbound_in_transaction(
                object(),  # type: ignore[arg-type]
                with_turn,
            )
        )

    random_id = OutboundDraft(
        message_id=OutboundMessageId.new(),
        conversation_id=standalone.conversation_id,
        turn_id=None,
        delivery_stream_id=standalone.delivery_stream_id,
        kind=standalone.kind,
        payload=standalone.payload,
        idempotency_key=standalone.idempotency_key,
        created_at=standalone.created_at,
    )
    with pytest.raises(ValueError, match="deterministic conversation ID"):
        asyncio.run(
            repository.enqueue_standalone_outbound_in_transaction(
                object(),  # type: ignore[arg-type]
                random_id,
            )
        )


def test_stale_outbound_claim_cannot_ack_newer_lease(monkeypatch: Any) -> None:
    """@brief 陈旧 outbox token 无法确认新租约 / A stale outbox token cannot acknowledge a newer lease."""

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object | None = None,
    ) -> int:
        """@brief 模拟 fencing 条件未命中 / Simulate a fencing predicate miss."""

        return 0

    connection = object()
    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db_connection, "execute", fake_execute)
    repository = PostgresOutboxRepository()

    message = outbox_repository._map_outbound(_outbound_row())
    stale_claim = OutboundClaim(
        message=message,
        token=LeaseToken.new(),
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    with pytest.raises(StaleClaimError):
        asyncio.run(
            repository.mark_outbound_delivered(
                stale_claim,
                delivered_at=NOW + timedelta(seconds=1),
                external_message_id="42",
            )
        )


def test_delivered_outbound_atomically_completes_turn(monkeypatch: Any) -> None:
    """@brief outbox 成功与 Turn DELIVERED 在同事务提交 / Outbox success and Turn DELIVERED commit in one transaction."""

    connection = object()
    repository = PostgresOutboxRepository()
    captured: dict[str, object] = {}

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> int:
        """@brief 模拟 fencing 更新成功 / Simulate a successful fenced update."""

        captured["outbox_connection"] = connection
        return 1

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回等待投递的回合 / Return a turn waiting for delivery."""

        return turn_uow._map_turn(
            _turn_row(
                state="waiting_delivery",
                version=4,
                inference_attempts=1,
                delivery_attempts=1,
            )
        )

    async def fake_persist(
        turn: object,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 捕获终态回合 / Capture the terminal turn."""

        captured["turn"] = turn
        captured["turn_connection"] = connection
        captured["version"] = expected_version

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db_connection, "execute", fake_execute)
    monkeypatch.setattr(outbox_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(outbox_repository, "_persist_turn", fake_persist)
    claim = OutboundClaim(
        message=outbox_repository._map_outbound(_outbound_row()),
        token=LeaseToken.new(),
        lease_expires_at=NOW + timedelta(seconds=30),
    )

    asyncio.run(
        repository.mark_outbound_delivered(
            claim,
            delivered_at=NOW + timedelta(seconds=1),
            external_message_id="42",
        )
    )

    terminal = captured["turn"]
    assert getattr(terminal, "state").value == "delivered"
    assert captured["outbox_connection"] is connection
    assert captured["turn_connection"] is connection
    assert captured["version"] == 4


def test_delivered_standalone_outbound_never_transitions_a_turn(
    monkeypatch: Any,
) -> None:
    """@brief standalone 投递成功不加载或推进 Turn / Successful standalone delivery neither loads nor advances a Turn."""

    connection = object()
    repository = PostgresOutboxRepository()

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> int:
        """@brief 模拟 fenced outbox 成功 / Simulate a successful fenced outbox update."""

        del sql, params, connection
        return 1

    async def unexpected_turn_load(*args: object, **kwargs: object) -> object:
        """@brief 拒绝 standalone 查询 Turn / Reject a Turn lookup for a standalone row."""

        del args, kwargs
        raise AssertionError("standalone finalization loaded a Turn")

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db_connection, "execute", fake_execute)
    monkeypatch.setattr(
        outbox_repository, "_load_turn_for_mutation", unexpected_turn_load
    )
    claim = OutboundClaim(
        message=outbox_repository._map_outbound(_outbound_row(turn_id=None)),
        token=LeaseToken.new(),
        lease_expires_at=NOW + timedelta(seconds=30),
    )

    asyncio.run(
        repository.mark_outbound_delivered(
            claim,
            delivered_at=NOW + timedelta(seconds=1),
            external_message_id="42",
        )
    )


def test_expired_outbound_recovery_atomically_schedules_turn_retry(
    monkeypatch: Any,
) -> None:
    """@brief 过期 outbox lease 与 Turn retry 在同事务恢复 / Expired outbox lease and Turn retry recover in one transaction."""

    connection = object()
    repository = PostgresOutboxRepository()
    captured: dict[str, object] = {}

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回已恢复成 retry_wait 的 outbox 行 / Return an outbox row recovered into retry_wait."""

        captured["sql"] = sql
        return [_outbound_row(status="retry_wait")]

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回等待投递的回合 / Return a turn waiting for delivery."""

        return turn_uow._map_turn(
            _turn_row(
                state="waiting_delivery",
                version=4,
                inference_attempts=1,
                delivery_attempts=1,
            )
        )

    async def fake_persist(
        turn: object,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 捕获 retry-wait 回合 / Capture the retry-wait turn."""

        captured["turn"] = turn
        captured["connection"] = connection

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db_connection, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(outbox_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(outbox_repository, "_persist_turn", fake_persist)

    recovered = asyncio.run(repository.recover_expired_outbound_leases(now=NOW))

    retrying = captured["turn"]
    assert recovered == 1
    assert "FOR UPDATE SKIP LOCKED" in str(captured["sql"])
    assert getattr(retrying, "state").value == "delivery_retry_wait"
    assert getattr(retrying, "next_retry_at") > NOW
    assert captured["connection"] is connection


def test_expired_standalone_outbound_recovery_does_not_touch_a_turn(
    monkeypatch: Any,
) -> None:
    """@brief standalone lease 回收只恢复 outbox / Standalone lease recovery restores only the outbox."""

    connection = object()
    repository = PostgresOutboxRepository()

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回已恢复 standalone 行 / Return a recovered standalone row."""

        del sql, params, connection
        return [_outbound_row(status="retry_wait", turn_id=None)]

    async def unexpected_turn_load(*args: object, **kwargs: object) -> object:
        """@brief 拒绝 standalone 查询 Turn / Reject a Turn lookup for a standalone row."""

        del args, kwargs
        raise AssertionError("standalone recovery loaded a Turn")

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db_connection, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        outbox_repository, "_load_turn_for_mutation", unexpected_turn_load
    )

    assert asyncio.run(repository.recover_expired_outbound_leases(now=NOW)) == 1
