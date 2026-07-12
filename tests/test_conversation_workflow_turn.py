"""@brief PostgreSQL Turn 与 acceptance adapter 测试 / PostgreSQL Turn-and-acceptance adapter tests."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    TurnId,
)
from fogmoe_bot.domain.conversation.turn import ConversationTurn
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityDraft,
    InferenceActivityEnqueueResult,
)
from fogmoe_bot.domain.conversation.message import (
    MessageAppendResult,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.errors import ConcurrentTurnUpdateError
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_workflow import (
    turn as turn_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow import turn_uow
from fogmoe_bot.infrastructure.database.conversation_workflow.turn import (
    PostgresTurnRepository,
)

from conversation_workflow_testkit import (
    NOW,
    TURN_UUID,
    _activity_draft,
    _Billing,
    _initial_turn,
    _message_draft,
    _message_result,
    _TransactionContext,
    _turn_row,
)


def test_accept_turn_commits_state_and_user_message_in_one_transaction(
    monkeypatch: Any,
) -> None:
    """@brief 接受回合在一个事务中锁行、追加消息并推进状态 / Turn acceptance locks, appends, and advances in one transaction."""

    connection = object()
    transaction = _TransactionContext(connection)
    captured: dict[str, object] = {}
    repository = PostgresTurnRepository()
    draft = _message_draft(role=MessageRole.USER)
    activity_draft = _activity_draft()
    initial_turn = _initial_turn()

    async def fake_insert_or_load(
        turn: ConversationTurn,
        *,
        connection: object,
    ) -> ConversationTurn:
        """@brief 模拟插入初始 Turn / Simulate inserting the initial Turn."""

        captured["turn_connection"] = connection
        return turn

    async def fake_append(
        message: MessageDraft,
        *,
        connection: object,
    ) -> MessageAppendResult:
        """@brief 模拟同事务消息追加 / Simulate message append in the same transaction."""

        captured["append_connection"] = connection
        return _message_result(message, inserted=True)

    async def fake_persist(
        turn: object,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 捕获同事务回合更新 / Capture turn persistence in the same transaction."""

        captured["persist_connection"] = connection
        captured["persisted_turn"] = turn
        captured["expected_version"] = expected_version

    async def fake_enqueue_activity(
        activity: InferenceActivityDraft,
        *,
        connection: object,
    ) -> InferenceActivityEnqueueResult:
        """@brief 模拟同事务活动意图写入 / Simulate activity-intent persistence in the same transaction."""

        captured["activity_connection"] = connection
        return InferenceActivityEnqueueResult(
            activity=InferenceActivity.pending(activity),
            inserted=True,
        )

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(
        repository,
        "_insert_or_load_turn",
        fake_insert_or_load,
    )
    monkeypatch.setattr(turn_repository, "_append_message", fake_append)
    monkeypatch.setattr(
        repository,
        "_enqueue_inference_activity",
        fake_enqueue_activity,
    )
    monkeypatch.setattr(turn_repository, "_persist_turn", fake_persist)

    result = asyncio.run(
        repository.create_and_accept_turn(
            initial_turn,
            message=draft,
            activity=activity_draft,
            accepted_at=NOW,
        )
    )

    assert result.turn.version == 2
    assert result.turn.state.value == "waiting_inference"
    assert result.turn.inference_attempts == 1
    assert captured["turn_connection"] is connection
    assert captured["append_connection"] is connection
    assert captured["activity_connection"] is connection
    assert captured["persist_connection"] is connection
    assert captured["expected_version"] == 0
    assert transaction.exception is None


def test_create_and_accept_failure_rolls_back_without_persisting_turn_state(
    monkeypatch: Any,
) -> None:
    """@brief activity 写入失败使完整 create-and-accept UoW 回滚 / Activity failure rolls back the complete create-and-accept unit of work."""

    connection = object()
    transaction = _TransactionContext(connection)
    repository = PostgresTurnRepository()
    persisted = False

    async def fake_insert(
        turn: ConversationTurn,
        *,
        connection: object,
    ) -> ConversationTurn:
        """@brief 模拟事务内 Turn 插入 / Simulate the in-transaction Turn insert."""

        return turn

    async def fake_append(
        message: MessageDraft,
        *,
        connection: object,
    ) -> MessageAppendResult:
        """@brief 模拟用户消息插入 / Simulate user-message insertion."""

        return _message_result(message, inserted=True)

    async def fail_activity(
        activity: InferenceActivityDraft,
        *,
        connection: object,
    ) -> InferenceActivityEnqueueResult:
        """@brief 注入 activity 写入故障 / Inject an activity-persistence failure."""

        raise RuntimeError("activity insert failed")

    async def fake_persist(
        turn: ConversationTurn,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 记录不应发生的状态写入 / Record a state write that must not occur."""

        nonlocal persisted
        persisted = True

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(repository, "_insert_or_load_turn", fake_insert)
    monkeypatch.setattr(turn_repository, "_append_message", fake_append)
    monkeypatch.setattr(repository, "_enqueue_inference_activity", fail_activity)
    monkeypatch.setattr(turn_repository, "_persist_turn", fake_persist)

    with pytest.raises(RuntimeError, match="activity insert failed"):
        asyncio.run(
            repository.create_and_accept_turn(
                _initial_turn(),
                message=_message_draft(role=MessageRole.USER),
                activity=_activity_draft(),
                accepted_at=NOW,
            )
        )

    assert isinstance(transaction.exception, RuntimeError)
    assert persisted is False


def test_connection_bound_acceptance_uses_caller_transaction_without_nesting(
    monkeypatch: Any,
) -> None:
    """@brief connection-bound primitive 不打开嵌套事务 / The connection-bound primitive does not open a nested transaction."""

    connection = object()
    repository = PostgresTurnRepository()

    async def fake_insert(
        turn: ConversationTurn,
        *,
        connection: object,
    ) -> ConversationTurn:
        """@brief 返回调用方事务中的初始 Turn / Return the initial Turn in the caller transaction."""

        return turn

    async def fake_append(
        message: MessageDraft,
        *,
        connection: object,
    ) -> MessageAppendResult:
        """@brief 返回消息回执 / Return a message receipt."""

        return _message_result(message, inserted=True)

    async def fake_activity(
        activity: InferenceActivityDraft,
        *,
        connection: object,
    ) -> InferenceActivityEnqueueResult:
        """@brief 返回 activity 回执 / Return an activity receipt."""

        return InferenceActivityEnqueueResult(
            InferenceActivity.pending(activity),
            True,
        )

    async def fake_persist(
        turn: ConversationTurn,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 接受状态写入 / Accept the state write."""

        return None

    def unexpected_transaction() -> object:
        """@brief 拒绝嵌套事务 / Reject a nested transaction."""

        raise AssertionError("connection-bound primitive opened a nested transaction")

    monkeypatch.setattr(
        db_connection,
        "transaction",
        unexpected_transaction,
    )
    monkeypatch.setattr(repository, "_insert_or_load_turn", fake_insert)
    monkeypatch.setattr(turn_repository, "_append_message", fake_append)
    monkeypatch.setattr(repository, "_enqueue_inference_activity", fake_activity)
    monkeypatch.setattr(turn_repository, "_persist_turn", fake_persist)

    result = asyncio.run(
        repository.create_and_accept_turn_in_transaction(
            connection,  # type: ignore[arg-type]
            _initial_turn(),
            message=_message_draft(role=MessageRole.USER),
            activity=_activity_draft(),
            accepted_at=NOW,
        )
    )
    assert result.turn.state.value == "waiting_inference"


def test_history_reader_uses_turn_sequence_cutoff_and_bounded_ascending_window(
    monkeypatch: Any,
) -> None:
    """@brief 历史读取以当前 Turn sequence 截止并保持升序 / History reading uses the current Turn sequence cutoff and ascending order."""

    captured: dict[str, object] = {}

    async def fake_fetch_all(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object | None = None,
    ) -> list[tuple[object, ...]]:
        """@brief 返回两个规范消息行 / Return two canonical message rows."""

        captured["sql"] = sql
        captured["params"] = params
        captured["connection"] = connection
        return [
            (
                UUID("55555555-5555-4555-8555-555555555555"),
                "telegram:chat:-100:user:42:thread:9",
                1,
                TURN_UUID,
                99,
                "user",
                {"text": "hello"},
                "history:1",
                NOW,
            ),
            (
                UUID("66666666-6666-4666-8666-666666666666"),
                "telegram:chat:-100:user:42:thread:9",
                2,
                TURN_UUID,
                None,
                "assistant",
                {"text": "world"},
                "history:2",
                NOW + timedelta(seconds=1),
            ),
        ]

    monkeypatch.setattr(db_connection, "fetch_all", fake_fetch_all)
    repository = PostgresTurnRepository()
    messages = asyncio.run(
        repository.read_conversation_messages(
            ConversationId("telegram:chat:-100:user:42:thread:9"),
            through_turn_id=TurnId(TURN_UUID),
            limit=64,
        )
    )

    assert [int(message.sequence) for message in messages] == [1, 2]
    assert "MAX(sequence)" in str(captured["sql"])
    assert "message.sequence > reset_boundary.sequence" in str(captured["sql"])
    assert "message.sequence <= turn_bounds.last_sequence" in str(captured["sql"])
    assert "exclude_from_assistant" in str(captured["sql"])
    assert "message.turn_id = CAST(%s AS UUID)" in str(captured["sql"])
    assert "history_reset.through_sequence < turn_bounds.first_sequence" in str(
        captured["sql"]
    )
    assert "ORDER BY sequence ASC" in str(captured["sql"])
    assert captured["params"] == (
        "telegram:chat:-100:user:42:thread:9",
        str(TurnId(TURN_UUID)),
        "telegram:chat:-100:user:42:thread:9",
        "telegram:chat:-100:user:42:thread:9",
        str(TurnId(TURN_UUID)),
        64,
    )


@pytest.mark.parametrize(
    ("current_state", "current_version"),
    (("waiting_delivery", 4), ("delivered", 5)),
)
def test_accept_uow_late_replay_returns_descendant_turn(
    monkeypatch: Any,
    current_state: str,
    current_version: int,
) -> None:
    """@brief acceptance 晚到重放可返回已推进的规范回合 / Late acceptance replay may return the canonical descendant turn."""

    connection = object()
    repository = PostgresTurnRepository()
    user_message = _message_draft(role=MessageRole.USER)
    activity_draft = _activity_draft()

    async def fake_insert_or_load(
        turn: ConversationTurn,
        *,
        connection: object,
    ) -> ConversationTurn:
        """@brief 返回已越过 acceptance 的回合 / Return a turn already beyond acceptance."""

        return turn_uow._map_turn(
            _turn_row(
                state=current_state,
                version=current_version,
                inference_attempts=1,
                delivery_attempts=1,
            )
        )

    async def existing_message(
        draft: MessageDraft,
        *,
        operation: str,
        connection: object,
    ) -> MessageAppendResult:
        """@brief 返回 acceptance 已提交的用户消息 / Return the user message committed by acceptance."""

        return _message_result(draft, inserted=False)

    async def unexpected_write(*args: object, **kwargs: object) -> None:
        """@brief 拒绝晚到重放写操作 / Reject writes during late replay."""

        raise AssertionError("late acceptance replay attempted a write")

    async def existing_activity(
        draft: InferenceActivityDraft,
        *,
        operation: str,
        connection: object,
    ) -> InferenceActivityEnqueueResult:
        """@brief 返回 acceptance 已提交活动 / Return the activity committed by acceptance."""

        return InferenceActivityEnqueueResult(
            activity=InferenceActivity.pending(draft),
            inserted=False,
        )

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(
        repository,
        "_insert_or_load_turn",
        fake_insert_or_load,
    )
    monkeypatch.setattr(turn_repository, "_require_existing_message", existing_message)
    monkeypatch.setattr(
        repository,
        "_require_existing_inference_activity",
        existing_activity,
    )
    monkeypatch.setattr(turn_repository, "_append_message", unexpected_write)
    monkeypatch.setattr(turn_repository, "_persist_turn", unexpected_write)

    result = asyncio.run(
        repository.create_and_accept_turn(
            _initial_turn(),
            message=user_message,
            activity=activity_draft,
            accepted_at=NOW,
        )
    )

    assert result.turn.state.value == current_state
    assert result.user_message.inserted is False


def test_accept_uow_rejects_non_idempotent_version_conflict(
    monkeypatch: Any,
) -> None:
    """@brief 接受 UoW 拒绝非幂等版本冲突 / Acceptance unit rejects a non-idempotent version conflict."""

    connection = object()

    async def fake_insert_or_load(
        turn: ConversationTurn,
        *,
        connection: object,
    ) -> ConversationTurn:
        """@brief 返回不匹配的中间版本 / Return a mismatched intermediate version."""

        return turn_uow._map_turn(_turn_row(state="accepted", version=1))

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    repository = PostgresTurnRepository()
    monkeypatch.setattr(
        repository,
        "_insert_or_load_turn",
        fake_insert_or_load,
    )

    with pytest.raises(ConcurrentTurnUpdateError, match="expected initial version 0"):
        asyncio.run(
            repository.create_and_accept_turn(
                _initial_turn(),
                message=_message_draft(role=MessageRole.USER),
                activity=_activity_draft(),
                accepted_at=NOW,
            )
        )


def test_cancel_turn_rechecks_outbox_after_acquiring_turn_lock(
    monkeypatch: Any,
) -> None:
    """@brief cancel 两阶段重查捕获等待 turn 锁期间出现的 outbox / Two-pass cancellation catches an outbox appearing while waiting for the turn lock."""

    connection = object()
    billing = _Billing()
    repository = PostgresTurnRepository(billing=billing)  # type: ignore[arg-type]
    captured: dict[str, object] = {"lock_calls": 0, "sql": []}

    async def fake_lock_inference(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> list[tuple[str]]:
        """@brief 模拟无活动推理行 / Simulate no active inference row."""

        return []

    async def fake_lock(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> list[tuple[str]]:
        """@brief 首次不可见、turn 锁后可见 pending outbox / Hide the pending outbox first and reveal it after the turn lock."""

        captured["lock_calls"] = int(captured["lock_calls"]) + 1
        return [] if captured["lock_calls"] == 1 else [("pending",)]

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回并发 completion 后的回合 / Return the turn after concurrent completion."""

        return turn_uow._map_turn(
            _turn_row(
                state="waiting_delivery",
                version=4,
                inference_attempts=1,
                delivery_attempts=1,
            )
        )

    async def fake_execute(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> int:
        """@brief 捕获 outbox 取消 / Capture outbox cancellation."""

        sql_calls = captured["sql"]
        assert isinstance(sql_calls, list)
        sql_calls.append(sql)
        captured["outbox_connection"] = connection
        return 1

    async def fake_persist(
        turn: object,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 捕获回合取消 / Capture turn cancellation."""

        captured["turn"] = turn
        captured["turn_connection"] = connection

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(repository, "_lock_active_outbound_for_turn", fake_lock)
    monkeypatch.setattr(
        repository,
        "_lock_active_inference_for_turn",
        fake_lock_inference,
    )
    monkeypatch.setattr(turn_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(db_connection, "execute", fake_execute)
    monkeypatch.setattr(turn_repository, "_persist_turn", fake_persist)

    cancelled = asyncio.run(
        repository.cancel_turn(
            TurnId(TURN_UUID),
            expected_version=4,
            cancelled_at=NOW + timedelta(seconds=1),
        )
    )

    assert captured["lock_calls"] == 2
    assert all("status = 'cancelled'" in sql for sql in captured["sql"])
    assert cancelled.state.value == "cancelled"
    assert captured["outbox_connection"] is connection
    assert captured["turn_connection"] is connection
    assert billing.released == [
        (connection, TurnId(TURN_UUID), NOW + timedelta(seconds=1))
    ]
