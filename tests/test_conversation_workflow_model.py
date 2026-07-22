"""@brief 可持久化会话工作流领域测试 / Durable conversation-workflow domain tests."""

from datetime import datetime, timedelta, timezone

import pytest

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    DeliveryStreamId,
    OutboundMessageId,
    TurnId,
    TurnSource,
    UpdateId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)
from fogmoe_bot.domain.conversation.turn import (
    ConversationTurn,
    InvalidTurnTransition,
    TurnEvent,
    TurnState,
)

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
"""@brief 测试用确定性 UTC 时间 / Deterministic UTC timestamp for tests."""


def _received_turn() -> ConversationTurn:
    """@brief 创建测试回合 / Create a test turn.

    @return RECEIVED 回合 / RECEIVED turn.
    """

    return ConversationTurn.received(
        turn_id=TurnId.new(),
        conversation_id=ConversationId("telegram:chat:-100:user:42:thread:0"),
        source=TurnSource.telegram(UpdateId(100)),
        received_at=NOW,
    )


def test_happy_path_is_explicit_and_versioned() -> None:
    """@brief 正常路径显式推进且每步递增版本 / Happy path is explicit and increments every version."""

    turn = _received_turn()
    events = (
        TurnEvent.ACCEPT,
        TurnEvent.REQUEST_INFERENCE,
        TurnEvent.INFERENCE_SUCCEEDED,
        TurnEvent.REQUEST_DELIVERY,
        TurnEvent.DELIVERY_SUCCEEDED,
    )

    for offset, event in enumerate(events, start=1):
        turn = turn.transition(event, occurred_at=NOW + timedelta(seconds=offset))

    assert turn.state is TurnState.DELIVERED
    assert turn.version == len(events)
    assert turn.inference_attempts == 1
    assert turn.delivery_attempts == 1
    assert turn.is_terminal is True


def test_retry_states_preserve_the_activity_to_resume() -> None:
    """@brief 重试状态显式保存恢复活动 / Retry state explicitly preserves the activity to resume."""

    turn = _received_turn()
    turn = turn.transition(TurnEvent.ACCEPT, occurred_at=NOW)
    turn = turn.transition(TurnEvent.REQUEST_INFERENCE, occurred_at=NOW)
    retry_at = NOW + timedelta(minutes=1)

    waiting = turn.transition(
        TurnEvent.SCHEDULE_INFERENCE_RETRY,
        occurred_at=NOW + timedelta(seconds=1),
        retry_at=retry_at,
        error="provider timeout",
    )

    assert waiting.state is TurnState.INFERENCE_RETRY_WAIT
    assert waiting.next_retry_at == retry_at
    assert waiting.last_error == "provider timeout"

    resumed = waiting.transition(TurnEvent.RETRY_INFERENCE, occurred_at=retry_at)

    assert resumed.state is TurnState.WAITING_INFERENCE
    assert resumed.next_retry_at is None
    assert resumed.last_error is None
    assert resumed.inference_attempts == 2


def test_illegal_and_terminal_transitions_are_rejected() -> None:
    """@brief 非法跳转和终态后续事件均被拒绝 / Illegal jumps and post-terminal events are rejected."""

    turn = _received_turn()

    with pytest.raises(
        InvalidTurnTransition, match="received rejects inference_succeeded"
    ):
        turn.transition(TurnEvent.INFERENCE_SUCCEEDED, occurred_at=NOW)

    cancelled = turn.transition(TurnEvent.CANCEL, occurred_at=NOW)
    with pytest.raises(InvalidTurnTransition, match="Terminal state cancelled"):
        cancelled.transition(TurnEvent.ACCEPT, occurred_at=NOW)


def test_retry_transition_requires_future_time_and_error() -> None:
    """@brief 重试转移必须携带未来时间与错误 / Retry transition requires a future time and an error."""

    turn = _received_turn().transition(TurnEvent.ACCEPT, occurred_at=NOW)
    turn = turn.transition(TurnEvent.REQUEST_INFERENCE, occurred_at=NOW)

    with pytest.raises(ValueError, match="require retry_at"):
        turn.transition(
            TurnEvent.SCHEDULE_INFERENCE_RETRY,
            occurred_at=NOW,
            error="timeout",
        )
    with pytest.raises(ValueError, match="later than occurred_at"):
        turn.transition(
            TurnEvent.SCHEDULE_INFERENCE_RETRY,
            occurred_at=NOW,
            retry_at=NOW,
            error="timeout",
        )
    with pytest.raises(ValueError, match="require an error"):
        turn.transition(
            TurnEvent.SCHEDULE_INFERENCE_RETRY,
            occurred_at=NOW,
            retry_at=NOW + timedelta(seconds=1),
        )


def test_timestamps_must_be_timezone_aware() -> None:
    """@brief 工作流拒绝 naive datetime / Workflow rejects naive datetime."""

    with pytest.raises(ValueError, match="timezone-aware"):
        ConversationTurn.received(
            turn_id=TurnId.new(),
            conversation_id=ConversationId("conversation:1"),
            source=TurnSource.telegram(UpdateId(1)),
            received_at=datetime(2030, 1, 1),
        )


def test_outbound_draft_carries_explicit_delivery_stream() -> None:
    """@brief 出站副作用显式携带 chat/thread 顺序流 / Outbound effect explicitly carries its chat/thread ordering stream."""

    turn = _received_turn()
    draft = OutboundDraft(
        message_id=OutboundMessageId.new(),
        conversation_id=turn.conversation_id,
        turn_id=turn.turn_id,
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:-100:thread:9"),
        kind=SEND_TELEGRAM_MESSAGE,
        payload={"chat_id": -100, "message_thread_id": 9, "text": "hello"},
        idempotency_key=f"turn:{turn.turn_id}:answer",
        created_at=NOW,
    )

    assert str(draft.delivery_stream_id) == "telegram:primary:chat:-100:thread:9"
    assert draft.payload["text"] == "hello"


def test_standalone_outbound_uses_conversation_scoped_deterministic_id() -> None:
    """@brief 无 Turn 副作用按会话幂等键稳定派生 / Standalone effects derive stably from the conversation idempotency key."""

    conversation_id = ConversationId("assistant-user:42")
    idempotency_key = "update:100:assistant-feedback:text_too_long"
    message_id = OutboundMessageId.for_conversation(
        conversation_id,
        idempotency_key,
    )
    draft = OutboundDraft(
        message_id=message_id,
        conversation_id=conversation_id,
        turn_id=None,
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:42:thread:0"),
        kind=SEND_TELEGRAM_MESSAGE,
        payload={"chat_id": 42, "text": "too long"},
        idempotency_key=idempotency_key,
        created_at=NOW,
    )

    assert draft.turn_id is None
    assert message_id == OutboundMessageId.for_conversation(
        conversation_id,
        idempotency_key,
    )
    assert message_id != OutboundMessageId.for_conversation(
        ConversationId("assistant-user:43"),
        idempotency_key,
    )


def test_turn_id_is_stable_per_source_update() -> None:
    """@brief Update 重放得到稳定且互不冲突的 Turn ID / Update replay yields stable, non-colliding Turn IDs."""

    first = TurnId.for_source(TurnSource.telegram(UpdateId(42)))
    replay = TurnId.for_source(TurnSource.telegram(UpdateId(42)))
    another = TurnId.for_source(TurnSource.telegram(UpdateId(43)))

    assert first == replay
    assert first != another
    assert str(first) == "bc2d7d87-ad90-5d4e-b349-86274b380617"


def test_effect_ids_are_stable_per_turn_and_semantic_key() -> None:
    """@brief 消息/outbox ID 按 Turn 与语义键稳定派生 / Message and outbox IDs derive stably from Turn and semantic key."""

    turn_id = TurnId.for_source(TurnSource.telegram(UpdateId(42)))

    message_id = ConversationMessageId.for_turn(turn_id, "assistant.final")
    outbound_id = OutboundMessageId.for_turn(turn_id, "telegram.primary")

    assert message_id == ConversationMessageId.for_turn(turn_id, "assistant.final")
    assert outbound_id == OutboundMessageId.for_turn(turn_id, "telegram.primary")
    assert str(message_id) == "61c34ddf-9037-58ba-85ae-6763ec17d011"
    assert str(outbound_id) == "1f966ff5-41c0-5039-b2de-de40c71728b6"
    assert message_id != ConversationMessageId.for_turn(turn_id, "assistant.tool")

    with pytest.raises(ValueError, match="cannot be empty"):
        OutboundMessageId.for_turn(turn_id, "  ")
