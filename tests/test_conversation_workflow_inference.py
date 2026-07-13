"""@brief PostgreSQL inference adapter 原子提交测试 / PostgreSQL inference-adapter atomic-commit tests."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from fogmoe_bot.domain.conversation.identity import (
    InferenceActivityId,
    LeaseToken,
    TurnId,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityClaim,
    InferenceActivityStatus,
)
from fogmoe_bot.domain.conversation.message import (
    MessageAppendResult,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import (
    OutboundDraft,
    OutboundEnqueueResult,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.conversation_workflow import (
    inference as inference_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow import (
    outbox as outbox_repository,
)
from fogmoe_bot.infrastructure.database.conversation_workflow import turn_uow
from fogmoe_bot.infrastructure.database.conversation_workflow.inference import (
    PostgresInferenceRepository,
)

from conversation_workflow_testkit import (
    NOW,
    TURN_UUID,
    _activity,
    _Billing,
    _message_draft,
    _message_result,
    _outbound_draft,
    _outbound_row,
    _TransactionContext,
    _turn_row,
)


def test_inference_claim_preserves_conversation_causality_across_workers(
    monkeypatch: Any,
) -> None:
    """@brief 同 Conversation 的后续推理不能越过早期活动 / A later inference cannot overtake an earlier activity in the same Conversation."""

    async def scenario() -> None:
        """@brief 捕获 claim SQL 的会话头部谓词 / Capture the conversation-head predicate in claim SQL."""

        connection = object()
        captured: dict[str, object] = {}

        async def fake_fetch_all(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> list[tuple[object, ...]]:
            """@brief 记录 claim SQL 且返回空队列 / Record claim SQL and return an empty queue."""

            del params
            captured["sql"] = sql
            captured["connection"] = connection
            return []

        monkeypatch.setattr(
            db_connection,
            "transaction",
            lambda: _TransactionContext(connection),
        )
        monkeypatch.setattr(db_connection, "fetch_all", fake_fetch_all)

        claims = await PostgresInferenceRepository().claim_inference_activities(
            now=NOW,
            limit=8,
            lease_for=timedelta(seconds=30),
        )

        assert claims == ()
        sql = str(captured["sql"])
        assert "earlier.conversation_id = candidate.conversation_id" in sql
        assert "earlier.status IN ('pending', 'processing', 'retry')" in sql
        assert "(earlier.created_at, earlier.activity_id)" in sql
        assert "< (candidate.created_at, candidate.activity_id)" in sql
        assert captured["connection"] is connection

    asyncio.run(scenario())


def test_inference_uow_failure_exits_the_single_transaction_for_rollback(
    monkeypatch: Any,
) -> None:
    """@brief outbox 写入失败会让整个推理 UoW 退出并回滚 / An outbox failure exits and rolls back the entire inference unit of work."""

    connection = object()
    transaction = _TransactionContext(connection)
    billing = _Billing()
    repository = PostgresInferenceRepository(billing=billing)  # type: ignore[arg-type]
    assistant_message = _message_draft(role=MessageRole.ASSISTANT)
    outbound = _outbound_draft()
    token = LeaseToken.new()
    claim = InferenceActivityClaim(
        activity=_activity(),
        token=token,
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    persisted = False

    async def fake_load_activity(
        activity_id: InferenceActivityId,
        *,
        connection: object,
    ) -> tuple[InferenceActivity, LeaseToken]:
        """@brief 返回当前 processing 活动 / Return the current processing activity."""

        return claim.activity, token

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回待推理回合 / Return a turn waiting for inference."""

        return turn_uow._map_turn(
            _turn_row(
                state="waiting_inference",
                version=2,
                inference_attempts=1,
            )
        )

    async def fake_append(
        message: MessageDraft,
        *,
        connection: object,
    ) -> MessageAppendResult:
        """@brief 模拟助手消息已在事务中追加 / Simulate assistant-message append in the transaction."""

        return _message_result(message, inserted=True)

    async def fail_enqueue(
        connection: object,
        draft: OutboundDraft,
    ) -> OutboundEnqueueResult:
        """@brief 模拟 outbox 写入失败 / Simulate an outbox write failure."""

        raise RuntimeError("outbox insert failed")

    async def fake_persist(
        turn: object,
        *,
        expected_version: int,
        connection: object,
    ) -> None:
        """@brief 记录不应发生的回合持久化 / Record turn persistence that must not occur."""

        nonlocal persisted
        persisted = True

    async def fake_advisory_lock(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> None:
        """@brief 模拟 conversation advisory lock / Simulate the conversation advisory lock."""

        assert "pg_advisory_xact_lock" in sql
        assert params == ("telegram:chat:-100:user:42:thread:9",)

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(
        db_connection,
        "fetch_one",
        fake_advisory_lock,
    )
    monkeypatch.setattr(
        repository,
        "_load_inference_activity_for_update",
        fake_load_activity,
    )
    monkeypatch.setattr(inference_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(inference_repository, "_append_message", fake_append)
    monkeypatch.setattr(
        repository._outbox,
        "enqueue_outbound_in_transaction",
        fail_enqueue,
    )
    monkeypatch.setattr(inference_repository, "_persist_turn", fake_persist)

    with pytest.raises(RuntimeError, match="outbox insert failed"):
        asyncio.run(
            repository.complete_inference_activity(
                claim,
                assistant_message=assistant_message,
                outbounds=(outbound,),
                completed_at=NOW,
            )
        )

    assert isinstance(transaction.exception, RuntimeError)
    assert persisted is False
    assert billing.settled == []


@pytest.mark.parametrize(
    ("current_state", "current_version", "outbound_status"),
    (
        ("waiting_delivery", 4, "pending"),
        ("delivered", 5, "delivered"),
    ),
)
def test_inference_uow_replay_returns_existing_atomic_effects(
    monkeypatch: Any,
    current_state: str,
    current_version: int,
    outbound_status: str,
) -> None:
    """@brief 已提交推理 UoW 的重放返回规范消息而不再次写入 / Replay of a committed inference unit returns canonical effects without rewriting."""

    connection = object()
    billing = _Billing()
    repository = PostgresInferenceRepository(billing=billing)  # type: ignore[arg-type]
    assistant_message = _message_draft(role=MessageRole.ASSISTANT)
    outbound = _outbound_draft()
    token = LeaseToken.new()
    claim = InferenceActivityClaim(
        activity=_activity(),
        token=token,
        lease_expires_at=NOW + timedelta(minutes=1),
    )

    async def fake_load_activity(
        activity_id: InferenceActivityId,
        *,
        connection: object,
    ) -> tuple[InferenceActivity, None]:
        """@brief 返回已完成活动 / Return the completed activity."""

        return (
            _activity(
                status=InferenceActivityStatus.COMPLETED,
                completion_token=token,
            ),
            None,
        )

    async def fake_load_turn(
        turn_id: TurnId,
        *,
        connection: object,
    ) -> object:
        """@brief 返回已完成组合提交的回合 / Return a turn whose composite commit already completed."""

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
        """@brief 返回已存在助手消息 / Return the existing assistant message."""

        return _message_result(draft, inserted=False)

    async def existing_outbound(
        connection: object,
        draft: OutboundDraft,
        *,
        operation: str,
    ) -> OutboundEnqueueResult:
        """@brief 返回已存在 outbox 消息 / Return the existing outbox message."""

        return OutboundEnqueueResult(
            message=outbox_repository._map_outbound(
                _outbound_row(status=outbound_status)
            ),
            inserted=False,
        )

    async def unexpected_write(*args: object, **kwargs: object) -> None:
        """@brief 拒绝幂等重放中的写操作 / Reject writes during idempotent replay."""

        raise AssertionError("idempotent replay attempted a write")

    async def fake_advisory_lock(
        sql: str,
        params: tuple[object, ...],
        *,
        connection: object,
    ) -> None:
        """@brief 模拟 completion 的 conversation lock / Simulate completion's conversation lock."""

        assert "pg_advisory_xact_lock" in sql
        assert params == ("telegram:chat:-100:user:42:thread:9",)

    monkeypatch.setattr(
        db_connection,
        "transaction",
        lambda: _TransactionContext(connection),
    )
    monkeypatch.setattr(
        db_connection,
        "fetch_one",
        fake_advisory_lock,
    )
    monkeypatch.setattr(
        repository,
        "_load_inference_activity_for_update",
        fake_load_activity,
    )
    monkeypatch.setattr(inference_repository, "_load_turn_for_mutation", fake_load_turn)
    monkeypatch.setattr(
        inference_repository, "_require_existing_message", existing_message
    )
    monkeypatch.setattr(
        repository._outbox,
        "require_existing_outbound_in_transaction",
        existing_outbound,
    )
    monkeypatch.setattr(inference_repository, "_append_message", unexpected_write)
    monkeypatch.setattr(
        repository._outbox,
        "enqueue_outbound_in_transaction",
        unexpected_write,
    )
    monkeypatch.setattr(inference_repository, "_persist_turn", unexpected_write)

    result = asyncio.run(
        repository.complete_inference_activity(
            claim,
            assistant_message=assistant_message,
                outbounds=(outbound,),
            completed_at=NOW,
        )
    )

    assert result.turn.state.value == current_state
    assert result.assistant_message.inserted is False
    assert result.outbounds[0].inserted is False
    assert billing.settled == [(connection, TurnId(TURN_UUID), NOW)]
