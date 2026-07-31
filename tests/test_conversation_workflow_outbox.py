"""@brief PostgreSQL transactional outbox adapter 测试 / PostgreSQL transactional-outbox adapter tests."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from conversation_workflow_testkit import (
    NOW,
    TURN_UUID,
    _outbound_cancellation_row,
    _outbound_claim_row,
    _outbound_draft,
    _outbound_recovery_row,
    _outbound_row,
    _standalone_outbound_draft,
    _TransactionContext,
    _turn_row,
)

from fogmoe_bot.domain.conversation.errors import (
    IdempotencyConflictError,
    StaleClaimError,
)
from fogmoe_bot.domain.conversation.identity import (
    LeaseToken,
    OutboundMessageId,
    TurnId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_ASSISTANT_PROGRESS,
    OutboundClaim,
    OutboundDraft,
    OutboundFailure,
    OutboundStatus,
)
from fogmoe_bot.infrastructure.database import db
from fogmoe_bot.infrastructure.database.conversation_workflow import (
    outbox as outbox_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow import turn_uow
from fogmoe_bot.infrastructure.database.conversation_workflow.outbox import (
    PostgresOutboxRepository,
)


def _claim_for(row: tuple[object, ...]) -> OutboundClaim:
    """@brief 从 processing 数据库行签发测试 claim / Issue a test claim from a processing database row.

    @param row processing outbox 行 / Processing outbox row.
    @return 带 fencing ownership 的 claim / Claim carrying fencing ownership.
    """

    message = outbox_repository._map_outbound(row)
    return OutboundClaim.from_processing(
        message,
        token=LeaseToken.new(),
        lease_expires_at=NOW + timedelta(seconds=30),
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
        if "turn.state = 'failed_final'" in sql:
            return []
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
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
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
    abandoned_sql, _, abandoned_connection = calls[0]
    sql, params, used_connection = calls[1]
    assert "delivery plan cancelled after permanent sibling failure" in abandoned_sql
    assert "turn.state = 'failed_final'" in abandoned_sql
    assert "LIMIT %s" in abandoned_sql
    assert "turn.state = 'waiting_delivery'" in sql
    assert "FOR UPDATE OF candidate SKIP LOCKED" in sql
    assert "candidate.version AS previous_version" in sql
    assert "earlier.delivery_stream_id = candidate.delivery_stream_id" in sql
    assert "earlier.stream_sequence < candidate.stream_sequence" in sql
    assert used_connection is connection
    assert abandoned_connection is connection
    assert params[1] == SEND_TELEGRAM_ASSISTANT_PROGRESS.value
    assert UUID(str(params[3])) == claims[0].token.value
    assert claims[0].expected_version == 1


def test_claim_outbound_rejects_sql_domain_transition_divergence(
    monkeypatch: Any,
) -> None:
    """@brief claim SQL 与领域转换不一致时事务快速失败 / A claim transaction fails fast when SQL diverges from the domain transition.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    connection = object()
    repository = PostgresOutboxRepository()
    divergent = list(_outbound_claim_row(previous_status="pending"))
    divergent[9] = 2

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回错误递增两个版本的 post snapshot / Return a post-snapshot advanced by two versions.

        @param sql claim SQL / Claim SQL.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 错误 post snapshot / Divergent post-snapshot.
        """

        del params, connection
        if "turn.state = 'failed_final'" in sql:
            return []
        return [tuple(divergent)]

    monkeypatch.setattr(db, "transaction", lambda: _TransactionContext(connection))
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)

    with pytest.raises(RuntimeError, match="diverged from the domain"):
        asyncio.run(
            repository.claim_outbound(
                now=NOW,
                limit=1,
                lease_for=timedelta(seconds=30),
            )
        )


def test_claim_outbound_validates_abandoned_plan_cancellation(
    monkeypatch: Any,
) -> None:
    """@brief claim 事务用领域 cancel 对照 abandoned bulk SQL / The claim transaction checks abandoned bulk SQL against the domain cancellation.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    connection = object()
    repository = PostgresOutboxRepository()
    calls = 0

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 先返回 abandoned cancellation，再返回空 claim batch / Return an abandoned cancellation followed by an empty claim batch.

        @param sql 当前 SQL / Current SQL.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 当前阶段的数据库行 / Rows for the current stage.
        """

        nonlocal calls
        del params, connection
        calls += 1
        if "turn.state = 'failed_final'" in sql:
            return [_outbound_cancellation_row(cancelled_at=NOW)]
        return []

    monkeypatch.setattr(db, "transaction", lambda: _TransactionContext(connection))
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)

    claims = asyncio.run(
        repository.claim_outbound(
            now=NOW,
            limit=1,
            lease_for=timedelta(seconds=30),
        )
    )

    assert claims == ()
    assert calls == 2


def test_progress_outbound_is_claimable_while_its_turn_is_processing(
    monkeypatch: Any,
) -> None:
    """@brief progress outbound 可在推理中领取且不要求 waiting_delivery /
    A progress outbound is claimable during inference without requiring waiting_delivery.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    connection = object()
    repository = PostgresOutboxRepository()
    progress_row = list(_outbound_claim_row(previous_status="pending"))
    progress_row[5] = SEND_TELEGRAM_ASSISTANT_PROGRESS.value

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回 progress claim 并验证推理期例外 / Return a progress claim and verify the inference-time exception."""

        if "turn.state = 'failed_final'" in sql:
            return []
        assert "candidate.kind = %s" in sql
        assert params[1] == SEND_TELEGRAM_ASSISTANT_PROGRESS.value
        return [tuple(progress_row)]

    async def unexpected_turn_load(*args: object, **kwargs: object) -> object:
        """@brief progress claim 不应读取 Turn / A progress claim must not load its Turn."""

        del args, kwargs
        raise AssertionError("progress claim required waiting_delivery")

    monkeypatch.setattr(db, "transaction", lambda: _TransactionContext(connection))
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        outbox_repository,
        "_load_turn_for_mutation",
        unexpected_turn_load,
    )

    claims = asyncio.run(
        repository.claim_outbound(
            now=NOW,
            limit=1,
            lease_for=timedelta(seconds=30),
        )
    )

    assert len(claims) == 1
    assert claims[0].message.draft.kind == SEND_TELEGRAM_ASSISTANT_PROGRESS
    assert claims[0].message.status is OutboundStatus.PROCESSING


def test_progress_delivery_and_final_failure_do_not_mutate_the_turn(
    monkeypatch: Any,
) -> None:
    """@brief progress 成功或最终失败都不推进最终 Turn 投递状态 /
    Progress success or final failure never mutates the final Turn delivery state.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    async def scenario() -> None:
        """@brief 分别确认两个 progress claims / Acknowledge two separate progress claims."""

        connection = object()
        repository = PostgresOutboxRepository()
        execute_calls: list[str] = []

        async def fake_execute(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> int:
            """@brief 记录 outbox 自身状态更新 / Record the outbox-only state update."""

            del params
            assert connection is not None
            execute_calls.append(sql)
            return 1

        async def unexpected_turn_load(*args: object, **kwargs: object) -> object:
            """@brief progress 终态不应读取 Turn / Progress terminalization must not load a Turn."""

            del args, kwargs
            raise AssertionError("progress terminalization mutated its Turn")

        async def unexpected_fetch(*args: object, **kwargs: object) -> object:
            """@brief progress 成功不应扫描最终投递计划 / Progress success must not scan the final delivery plan."""

            del args, kwargs
            raise AssertionError("progress delivery inspected the final plan")

        monkeypatch.setattr(
            db,
            "transaction",
            lambda: _TransactionContext(connection),
        )
        monkeypatch.setattr(db, "execute", fake_execute)
        monkeypatch.setattr(db, "fetch_one", unexpected_fetch)
        monkeypatch.setattr(
            outbox_repository,
            "_load_turn_for_mutation",
            unexpected_turn_load,
        )

        def progress_claim() -> OutboundClaim:
            """@brief 构造 processing progress claim / Build a processing progress claim."""

            row = list(_outbound_row(status="processing"))
            row[5] = SEND_TELEGRAM_ASSISTANT_PROGRESS.value
            return _claim_for(tuple(row))

        successful_claim = progress_claim()
        await repository.complete_outbound(
            successful_claim.message.succeed(
                successful_claim,
                delivered_at=NOW + timedelta(seconds=1),
                external_message_id="101",
            )
        )
        failed_claim = progress_claim()
        await repository.dead_letter_outbound(
            failed_claim.message.dead_letter(
                failed_claim,
                failed_at=NOW + timedelta(seconds=1),
                failure=OutboundFailure("permanent Telegram rejection"),
            )
        )

        assert len(execute_calls) == 2
        assert "status = 'delivered'" in execute_calls[0]
        assert "status = 'failed_final'" in execute_calls[1]

    asyncio.run(scenario())


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

        del params, connection
        if "turn.state = 'failed_final'" in sql:
            return []
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
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
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
    assert claims[0].message.draft.turn_id is None
    assert claims[0].message.status is OutboundStatus.PROCESSING


def test_claim_retry_outbound_keeps_delivery_plan_waiting(
    monkeypatch: Any,
) -> None:
    """@brief 重领重试消息不改写整份投递计划状态 / Reclaiming a retry message does not rewrite the delivery-plan state."""

    connection = object()
    repository = PostgresOutboxRepository()

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回 retry_wait 领取结果 / Return a claimed retry_wait row."""

        del params, connection
        if "turn.state = 'failed_final'" in sql:
            return []
        return [_outbound_claim_row(previous_status="retry_wait")]

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回仍在等待整份计划的回合 / Return a Turn still waiting for the whole plan."""

        return turn_uow._map_turn(
            _turn_row(
                state="waiting_delivery",
                version=5,
                inference_attempts=1,
                delivery_attempts=1,
            )
        )

    async def unexpected_persist(*args: object, **kwargs: object) -> None:
        """@brief 重试领取不应写 Turn / A retry claim must not write the Turn."""

        del args, kwargs
        raise AssertionError("retry claim rewrote delivery-plan state")

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(outbox_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(outbox_repository, "_persist_turn", unexpected_persist)

    claims = asyncio.run(
        repository.claim_outbound(
            now=NOW,
            limit=1,
            lease_for=timedelta(seconds=30),
        )
    )

    assert len(claims) == 1
    assert claims[0].message.status is OutboundStatus.PROCESSING


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
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
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
            draft.trace_context.to_traceparent(),
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
        db,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(db, "fetch_one", fake_fetch_one)

    result = asyncio.run(PostgresOutboxRepository().enqueue_standalone_outbound(draft))

    assert result.inserted is True
    assert result.message.draft.turn_id is None
    assert insert_params is not None
    assert insert_params[2] is None
    assert insert_params[-4:] == (
        draft.created_at,
        draft.created_at,
        draft.created_at,
        draft.trace_context.to_traceparent(),
    )
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
        draft.trace_context.to_traceparent(),
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

        assert "AND version = %s AND claim_token" in sql
        assert params[-2] == 1
        return 0

    connection = object()
    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "execute", fake_execute)
    repository = PostgresOutboxRepository()

    message = outbox_repository._map_outbound(_outbound_row())
    stale_claim = OutboundClaim.from_processing(
        message,
        token=LeaseToken.new(),
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    with pytest.raises(StaleClaimError):
        decision = stale_claim.message.succeed(
            stale_claim,
            delivered_at=NOW + timedelta(seconds=1),
            external_message_id="42",
        )
        asyncio.run(
            repository.complete_outbound(decision)
        )


def test_retry_outbound_settlement_uses_version_and_token_cas(
    monkeypatch: Any,
) -> None:
    """@brief retry persistence 同时比较 processing version 与 fencing token / Retry persistence compares both processing version and fencing token.

    @param monkeypatch pytest 替换工具 / pytest replacement utility.
    @return None / None.
    """

    connection = object()
    captured: dict[str, object] = {}

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> int:
        """@brief 捕获 retry CAS / Capture the retry CAS.

        @param sql retry SQL / Retry SQL.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 单行命中 / One affected row.
        """

        captured["sql"] = sql
        captured["params"] = params
        assert connection is not None
        return 1

    monkeypatch.setattr(db, "transaction", lambda: _TransactionContext(connection))
    monkeypatch.setattr(db, "execute", fake_execute)
    repository = PostgresOutboxRepository()
    claim = _claim_for(_outbound_row())
    decision = claim.message.retry(
        claim,
        failed_at=NOW + timedelta(seconds=1),
        retry_at=NOW + timedelta(seconds=2),
        failure=OutboundFailure("temporary Telegram error"),
    )

    asyncio.run(repository.schedule_outbound_retry(decision))

    assert "AND version = %s" in str(captured["sql"])
    params = captured["params"]
    assert isinstance(params, tuple)
    assert params[-2] == claim.expected_version
    assert UUID(str(params[-1])) == claim.token.value


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

    async def fake_fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[bool]:
        """@brief 表示这是计划中最后一条 effect / Report this as the final effect in the plan."""

        assert "status <> 'delivered'" in sql
        assert "kind <> %s" in sql
        assert params[1] == SEND_TELEGRAM_ASSISTANT_PROGRESS.value
        assert connection is not None
        return (False,)

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
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(outbox_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(outbox_repository, "_persist_turn", fake_persist)
    claim = _claim_for(_outbound_row())

    asyncio.run(
        repository.complete_outbound(
            claim.message.succeed(
                claim,
                delivered_at=NOW + timedelta(seconds=1),
                external_message_id="42",
            )
        )
    )

    terminal = captured["turn"]
    assert getattr(terminal, "state").value == "delivered"
    assert captured["outbox_connection"] is connection
    assert captured["turn_connection"] is connection
    assert captured["version"] == 4


def test_delivered_effect_keeps_turn_waiting_until_delivery_plan_is_empty(
    monkeypatch: Any,
) -> None:
    """@brief 中间 effect 成功不终结 Turn / A non-final effect does not complete the Turn."""

    connection = object()
    repository = PostgresOutboxRepository()

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> int:
        """@brief 模拟本条 effect 成功 / Simulate successful delivery of this effect."""

        del sql, params, connection
        return 1

    async def fake_fetch_one(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> tuple[bool]:
        """@brief 表示计划仍有未投递 effect / Report that the plan still has undelivered effects."""

        assert "status <> 'delivered'" in sql
        assert params
        assert connection is not None
        return (True,)

    async def unexpected_turn_load(*args: object, **kwargs: object) -> object:
        """@brief 中间 effect 成功不应加载或推进 Turn / A non-final effect must not load or advance the Turn."""

        del args, kwargs
        raise AssertionError("intermediate delivery completed the Turn")

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        outbox_repository, "_load_turn_for_mutation", unexpected_turn_load
    )
    claim = _claim_for(_outbound_row())

    asyncio.run(
        repository.complete_outbound(
            claim.message.succeed(
                claim,
                delivered_at=NOW + timedelta(seconds=1),
                external_message_id="42",
            )
        )
    )


def test_permanent_delivery_failure_cancels_unclaimed_sibling_effects(
    monkeypatch: Any,
) -> None:
    """@brief 永久失败会取消同一 Turn 尚未领取的 effect / A permanent failure cancels unclaimed sibling effects in the same Turn."""

    connection = object()
    repository = PostgresOutboxRepository()
    statements: list[tuple[str, tuple[object, ...]]] = []
    captured: dict[str, object] = {}

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> int:
        """@brief 记录 fenced 最终失败 SQL / Record fenced final-failure SQL.

        @param sql 执行的 SQL / Executed SQL.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 受影响行数 / Affected-row count.
        """

        assert connection is not None
        statements.append((sql, params))
        return 1

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> list[tuple[object, ...]]:
        """@brief 返回并记录已验证 sibling 取消快照 / Return and record a verified sibling-cancellation snapshot.

        @param sql sibling cancellation SQL / Sibling-cancellation SQL.
        @param params SQL 参数 / SQL parameters.
        @param connection 当前连接 / Current connection.
        @return 取消操作的 pre/post 行 / Cancellation pre/post row.
        """

        assert connection is not None
        statements.append((sql, params))
        return [
            _outbound_cancellation_row(
                cancelled_at=NOW + timedelta(seconds=1),
            )
        ]

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回等待投递的 Turn / Return a Turn waiting for delivery.

        @param turn_id Turn ID / Turn identifier.
        @param connection 当前连接 / Current connection.
        @return 等待投递 Turn / Waiting-delivery Turn.
        """

        del turn_id
        assert connection is not None
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
        """@brief 记录失败终态 Turn / Record the terminal failed Turn.

        @param turn 终态 Turn / Terminal Turn.
        @param expected_version 乐观锁版本 / Optimistic-lock version.
        @param connection 当前连接 / Current connection.
        @return None / None.
        """

        captured["turn"] = turn
        captured["version"] = expected_version
        assert connection is not None

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(outbox_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(outbox_repository, "_persist_turn", fake_persist)
    claim = _claim_for(_outbound_row())

    asyncio.run(
        repository.dead_letter_outbound(
            claim.message.dead_letter(
                claim,
                failed_at=NOW + timedelta(seconds=1),
                failure=OutboundFailure("permanent Telegram error"),
            )
        )
    )

    assert len(statements) == 2
    assert "status = 'failed_final'" in statements[0][0]
    assert "status = 'cancelled'" in statements[1][0]
    assert "message_id <> CAST(%s AS UUID)" in statements[1][0]
    assert getattr(captured["turn"], "state").value == "failed_final"
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
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "execute", fake_execute)
    monkeypatch.setattr(
        outbox_repository, "_load_turn_for_mutation", unexpected_turn_load
    )
    claim = _claim_for(_outbound_row(turn_id=None))

    asyncio.run(
        repository.complete_outbound(
            claim.message.succeed(
                claim,
                delivered_at=NOW + timedelta(seconds=1),
                external_message_id="42",
            )
        )
    )


def test_expired_outbound_recovery_leaves_delivery_plan_state_unchanged(
    monkeypatch: Any,
) -> None:
    """@brief 过期 lease 仅恢复消息，不改写整份投递计划 / Expired leases recover only messages, not the delivery-plan state."""

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
        captured["params"] = params
        return [_outbound_recovery_row(recovered_at=NOW)]

    async def unexpected_turn_load(*args: object, **kwargs: object) -> object:
        """@brief 租约恢复不应加载 Turn / Lease recovery must not load a Turn."""

        del args, kwargs
        raise AssertionError("lease recovery rewrote delivery-plan state")

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        outbox_repository, "_load_turn_for_mutation", unexpected_turn_load
    )

    recovered = asyncio.run(repository.recover_expired_outbound_leases(now=NOW))

    assert recovered == 1
    sql = str(captured["sql"])
    assert "FOR UPDATE OF candidate SKIP LOCKED" in sql
    assert "candidate.claim_token AS previous_claim_token" in sql
    assert "outbound.traceparent" in sql
    assert "lease_expires_at <= %s" in sql
    assert "LIMIT %s FOR UPDATE OF candidate SKIP LOCKED" in sql
    assert "status = 'retry_wait'" in sql
    assert "next_attempt_at = %s" in sql
    params = captured["params"]
    assert isinstance(params, tuple)
    assert params[0] == NOW
    assert params[1] == 512
    assert params[2] == NOW + timedelta(microseconds=1)
    assert params[3] == NOW
    assert params[4] == "recovered expired worker lease"


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
        return [_outbound_recovery_row(recovered_at=NOW, turn_id=None)]

    async def unexpected_turn_load(*args: object, **kwargs: object) -> object:
        """@brief 拒绝 standalone 查询 Turn / Reject a Turn lookup for a standalone row."""

        del args, kwargs
        raise AssertionError("standalone recovery loaded a Turn")

    monkeypatch.setattr(
        db,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(
        outbox_repository, "_load_turn_for_mutation", unexpected_turn_load
    )

    assert asyncio.run(repository.recover_expired_outbound_leases(now=NOW)) == 1
