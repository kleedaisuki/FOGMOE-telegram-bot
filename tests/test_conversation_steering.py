"""@brief Conversation steer revision 与 generation fencing 测试 / Tests for Conversation steer revisions and generation fencing."""

from datetime import UTC, datetime

import pytest

from fogmoe_bot.domain.assistant.messages import text_message
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    InferenceActivityId,
    MessageSequence,
    TurnId,
    TurnRevision,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityDraft,
    InferenceActivityStatus,
)
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.steering import (
    STEER_INPUT_KIND,
    TurnSteer,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 确定性测试时刻 / Deterministic test instant."""


def _steer_message(
    *,
    turn_id: TurnId,
    conversation_id: ConversationId,
    update_id: UpdateId,
) -> ConversationMessage:
    """@brief 构造规范 steer 消息 / Build a canonical steer message.

    @param turn_id 目标 Turn / Target Turn.
    @param conversation_id 会话 / Conversation.
    @param update_id 来源 Update / Source Update.
    @return 已分配 sequence 的 steer 消息 / Sequenced steer message.
    """

    return ConversationMessage(
        MessageDraft(
            message_id=ConversationMessageId.for_turn(
                turn_id,
                f"steer.update.{update_id.value}",
            ),
            conversation_id=conversation_id,
            turn_id=turn_id,
            source_update_id=update_id,
            role=MessageRole.USER,
            content={
                "input_kind": STEER_INPUT_KIND,
                "input_revision": 1,
                "text": "focus on costs",
                "model_message": text_message(
                    MessageRole.USER,
                    "focus on costs",
                ).to_json(),
            },
            idempotency_key=f"turn:{turn_id}:steer:update:{update_id.value}",
            created_at=NOW,
        ),
        MessageSequence(8),
    )


def test_turn_revision_is_monotonic_and_cannot_be_negative() -> None:
    """@brief TurnRevision 只能从零单调递增 / TurnRevision is non-negative and advances monotonically."""

    initial = TurnRevision.initial()
    assert int(initial) == 0
    assert int(initial.next()) == 1
    with pytest.raises(ValueError, match="cannot be negative"):
        TurnRevision(-1)


def test_steer_pending_inference_is_claimable_at_its_new_revision() -> None:
    """@brief steer_pending activity 可领取且显式携带新 revision / A steer-pending activity is claimable and explicitly carries its new revision."""

    turn_id = TurnId.new()
    draft = InferenceActivityDraft(
        activity_id=InferenceActivityId.for_turn(turn_id),
        turn_id=turn_id,
        conversation_id=ConversationId("assistant-user:42"),
        request={"schema_version": 2},
        created_at=NOW,
    )
    activity = InferenceActivity(
        draft=draft,
        status=InferenceActivityStatus.STEER_PENDING,
        version=2,
        attempt_count=1,
        next_attempt_at=NOW,
        updated_at=NOW,
        input_revision=TurnRevision(1),
    )

    assert activity.status is InferenceActivityStatus.STEER_PENDING
    assert int(activity.input_revision) == 1


def test_turn_steer_binds_source_message_and_revision() -> None:
    """@brief steer 绑定同一 Turn、来源 Update 与严格正 revision / A steer binds one Turn, source Update, and a strictly positive revision."""

    turn_id = TurnId.new()
    conversation_id = ConversationId("assistant-user:42")
    update_id = UpdateId(101)
    message = _steer_message(
        turn_id=turn_id,
        conversation_id=conversation_id,
        update_id=update_id,
    )

    steer = TurnSteer(
        turn_id=turn_id,
        conversation_id=conversation_id,
        source_update_id=update_id,
        revision=TurnRevision(1),
        message=message,
        accepted_at=NOW,
    )

    assert steer.query_text == "focus on costs"
    assert steer.message.draft.content["input_kind"] == STEER_INPUT_KIND


def test_turn_steer_rejects_cross_turn_or_unmarked_messages() -> None:
    """@brief steer 拒绝跨 Turn 与未标记普通消息 / A steer rejects cross-Turn and unmarked ordinary messages."""

    turn_id = TurnId.new()
    conversation_id = ConversationId("assistant-user:42")
    update_id = UpdateId(101)
    message = _steer_message(
        turn_id=turn_id,
        conversation_id=conversation_id,
        update_id=update_id,
    )

    with pytest.raises(ValueError, match="positive revision"):
        TurnSteer(
            turn_id=turn_id,
            conversation_id=conversation_id,
            source_update_id=update_id,
            revision=TurnRevision.initial(),
            message=message,
            accepted_at=NOW,
        )

    unmarked = ConversationMessage(
        MessageDraft(
            message_id=message.draft.message_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            source_update_id=update_id,
            role=MessageRole.USER,
            content={
                "text": "ordinary",
                "model_message": text_message(
                    MessageRole.USER,
                    "ordinary",
                ).to_json(),
            },
            idempotency_key=message.draft.idempotency_key,
            created_at=NOW,
        ),
        message.sequence,
    )
    with pytest.raises(ValueError, match="input_kind"):
        TurnSteer(
            turn_id=turn_id,
            conversation_id=conversation_id,
            source_update_id=update_id,
            revision=TurnRevision(1),
            message=unmarked,
            accepted_at=NOW,
        )
