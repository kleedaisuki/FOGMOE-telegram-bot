"""@brief Admin 主公告聚合领域测试 / Domain tests for the main Admin announcement aggregate."""

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fogmoe_bot.domain.admin.announcement import (
    Announcement,
    AnnouncementAudienceProgress,
    AnnouncementAudienceSnapshot,
    AnnouncementAudienceSnapshotted,
    AnnouncementCompletionAddress,
    AnnouncementDeliveryCompleted,
    AnnouncementDeliveryCounts,
    AnnouncementDeliveryStarted,
    AnnouncementDispatchContent,
    AnnouncementId,
    AnnouncementIntent,
    AnnouncementIntentMismatch,
    AnnouncementStatus,
    CompletedAnnouncement,
    DeliveringAnnouncement,
    ExpandingAnnouncement,
)
from fogmoe_bot.domain.admin.recipient import (
    AnnouncementRecipient,
    AnnouncementRecipientKind,
    AnnouncementRecipientStatus,
    PendingAnnouncementRecipient,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定领域测试时刻 / Fixed domain-test instant."""


def _intent(*, suffix: str = "base") -> AnnouncementIntent:
    """@brief 构造完整公告意图 / Build a complete announcement intent.

    @param suffix 幂等键后缀 / Idempotency-key suffix.
    @return 已验证意图 / Validated intent.
    """

    return AnnouncementIntent(
        idempotency_key=f" admin:test:{suffix} ",
        requested_by=7,
        source_update_id=11,
        body="hello",
        completion_address=AnnouncementCompletionAddress(
            chat_id=42,
            message_thread_id=5,
            reply_to_message_id=9,
        ),
        requested_at=NOW,
    )


def _completion_recipient(
    announcement_id: AnnouncementId,
    *,
    status: AnnouncementRecipientStatus = AnnouncementRecipientStatus.BLOCKED,
    chat_id: int = 42,
    message_thread_id: int | None = 5,
    reply_to_message_id: int = 9,
) -> AnnouncementRecipient:
    """@brief 构造公告最终报告 recipient / Build an announcement final-report recipient.

    @param announcement_id 所属公告 / Owning announcement.
    @param status 测试状态 / Test state.
    @param chat_id completion 会话 ID / Completion chat ID.
    @param message_thread_id completion 话题 ID / Completion thread ID.
    @param reply_to_message_id completion 回复消息 ID / Completion reply-message ID.
    @return 已验证 completion recipient / Validated completion recipient.
    """

    pending = status is AnnouncementRecipientStatus.PENDING
    return AnnouncementRecipient.restore(
        announcement_id=announcement_id,
        recipient_kind=AnnouncementRecipientKind.COMPLETION,
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        reply_to_message_id=reply_to_message_id,
        status=status,
        attempt_count=0,
        next_attempt_at=NOW if pending else None,
        claim_token=None,
        lease_expires_at=None,
        outbound_message_id=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
        expanded_at=None,
        terminal_at=None,
    )


def _delivering(*, recipient_count: int = 2) -> Announcement:
    """@brief 构造已完成受众扩展的 delivering 聚合 / Build a delivering aggregate whose audience expansion is complete.

    @param recipient_count 受众规模 / Audience size.
    @return delivering 聚合 / Delivering aggregate.
    """

    snapshotted = (
        Announcement.start(_intent())
        .record_audience_snapshot(
            AnnouncementAudienceSnapshot(recipient_count),
            recorded_at=NOW,
        )
        .announcement
    )
    if recipient_count == 0:
        return snapshotted
    return snapshotted.finish_audience_expansion(
        AnnouncementAudienceProgress(recipient_count, recipient_count),
        finished_at=NOW + timedelta(seconds=1),
    ).announcement


def test_intent_and_start_make_identity_and_initial_state_explicit() -> None:
    """@brief intent 规范语义且 start 创建封闭初态 / Intent normalizes semantics and start creates a sealed initial state."""

    intent = _intent()
    announcement = Announcement.start(intent)

    assert intent.idempotency_key == "admin:test:base"
    assert announcement.announcement_id == AnnouncementId.for_idempotency_key(
        intent.idempotency_key
    )
    assert announcement.intent is intent
    assert announcement.recipient_count == 0
    assert isinstance(announcement.state, ExpandingAnnouncement)
    assert announcement.status is AnnouncementStatus.EXPANDING
    assert tuple(field.name for field in fields(Announcement)) == (
        "announcement_id",
        "intent",
        "recipient_count",
        "state",
        "updated_at",
    )
    with pytest.raises(TypeError):
        Announcement()


def test_dispatch_body_bound_uses_original_normalized_length() -> None:
    """@brief dispatch 正文上限按原字符串长度执行 / The dispatch body limit is enforced on the original normalized string length."""

    content = AnnouncementDispatchContent(
        body="x" * 3500,
        counts=AnnouncementDeliveryCounts(0, 0, 0),
        announcement_created_at=NOW,
    )

    assert len(content.body) == 3500
    with pytest.raises(ValueError, match="1-3500"):
        AnnouncementDispatchContent(
            body=f" {'x' * 3500}",
            counts=AnnouncementDeliveryCounts(0, 0, 0),
            announcement_created_at=NOW,
        )


def test_snapshot_keeps_positive_audience_expanding_and_zero_goes_delivering() -> None:
    """@brief 正受众继续 expanding，零受众立即 delivering / Positive audiences remain expanding while zero audience enters delivering immediately."""

    positive = Announcement.start(_intent(suffix="positive"))
    positive_decision = positive.record_audience_snapshot(
        AnnouncementAudienceSnapshot(3),
        recorded_at=NOW,
    )
    zero = Announcement.start(_intent(suffix="zero"))
    zero_decision = zero.record_audience_snapshot(
        AnnouncementAudienceSnapshot(0),
        recorded_at=NOW,
    )

    assert positive_decision.expanding_announcement is positive
    assert positive_decision.announcement.recipient_count == 3
    assert isinstance(positive_decision.announcement.state, ExpandingAnnouncement)
    assert zero_decision.announcement.recipient_count == 0
    assert isinstance(zero_decision.announcement.state, DeliveringAnnouncement)
    with pytest.raises(ValueError, match="already recorded"):
        positive_decision.announcement.record_audience_snapshot(
            AnnouncementAudienceSnapshot(3),
            recorded_at=NOW,
        )


def test_delivery_starts_only_with_exact_terminal_audience_evidence() -> None:
    """@brief 只有全部受众终结且计数一致才能进入 delivering / Delivery starts only when exact audience evidence is fully terminal."""

    expanding = (
        Announcement.start(_intent())
        .record_audience_snapshot(
            AnnouncementAudienceSnapshot(3),
            recorded_at=NOW,
        )
        .announcement
    )
    with pytest.raises(ValueError, match="not complete"):
        expanding.finish_audience_expansion(
            AnnouncementAudienceProgress(3, 2),
            finished_at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="not complete"):
        expanding.finish_audience_expansion(
            AnnouncementAudienceProgress(4, 3),
            finished_at=NOW + timedelta(seconds=1),
        )

    decision = expanding.finish_audience_expansion(
        AnnouncementAudienceProgress(3, 3),
        finished_at=NOW + timedelta(seconds=1),
    )

    assert decision.expanding_announcement is expanding
    assert isinstance(decision.announcement.state, DeliveringAnnouncement)
    assert decision.announcement.updated_at == NOW + timedelta(seconds=1)


def test_delivery_completion_composes_blocked_recipient_release() -> None:
    """@brief completed 决策原生组合 blocked completion 释放 / The completed decision natively composes the blocked-completion release."""

    delivering = _delivering()
    completion = _completion_recipient(delivering.announcement_id)
    completed_at = NOW + timedelta(seconds=2)
    decision = delivering.complete_delivery(
        AnnouncementDeliveryCounts(recipients=2, delivered=1, failed=1),
        completion_recipient=completion,
        completed_at=completed_at,
    )

    assert decision.delivering_announcement is delivering
    assert isinstance(decision.announcement.state, CompletedAnnouncement)
    assert decision.announcement.state.completed_at == completed_at
    assert decision.announcement.updated_at == completed_at
    assert decision.completion_release.blocked_recipient is completion
    released_state = decision.completion_release.recipient.state
    assert isinstance(released_state, PendingAnnouncementRecipient)
    assert released_state.next_attempt_at == completed_at
    assert decision.completion_release.recipient.updated_at == completed_at


def test_delivery_completion_rejects_partial_or_wrong_completion_state() -> None:
    """@brief 部分投递或非 blocked completion 不能完成公告 / Partial delivery or a non-blocked completion receipt cannot complete an announcement."""

    delivering = _delivering()
    completion = _completion_recipient(delivering.announcement_id)
    with pytest.raises(ValueError, match="not terminal"):
        delivering.complete_delivery(
            AnnouncementDeliveryCounts(recipients=2, delivered=1, failed=0),
            completion_recipient=completion,
            completed_at=NOW + timedelta(seconds=2),
        )
    with pytest.raises(ValueError, match="blocked completion"):
        delivering.complete_delivery(
            AnnouncementDeliveryCounts(recipients=2, delivered=2, failed=0),
            completion_recipient=_completion_recipient(
                delivering.announcement_id,
                status=AnnouncementRecipientStatus.PENDING,
            ),
            completed_at=NOW + timedelta(seconds=2),
        )


@pytest.mark.parametrize(
    ("chat_id", "message_thread_id", "reply_to_message_id"),
    (
        (43, 5, 9),
        (42, 6, 9),
        (42, 5, 10),
    ),
)
def test_delivery_completion_requires_exact_intent_address(
    chat_id: int,
    message_thread_id: int | None,
    reply_to_message_id: int,
) -> None:
    """@brief completion recipient 的 chat/thread/reply 必须逐项等于 intent / Completion chat, thread, and reply IDs must each equal the intent."""

    delivering = _delivering()

    with pytest.raises(ValueError, match="disagrees with its intent"):
        delivering.complete_delivery(
            AnnouncementDeliveryCounts(recipients=2, delivered=2, failed=0),
            completion_recipient=_completion_recipient(
                delivering.announcement_id,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
            ),
            completed_at=NOW + timedelta(seconds=2),
        )


def test_restore_validates_full_persistence_matrix() -> None:
    """@brief restore 拒绝 ID、状态和 completed_at 矩阵不一致 / Restore rejects inconsistent identity, state, and completed-at matrices."""

    intent = _intent()
    completed_at = NOW + timedelta(seconds=3)
    restored = Announcement.restore(
        announcement_id=AnnouncementId.for_idempotency_key(intent.idempotency_key),
        idempotency_key=intent.idempotency_key,
        requested_by=intent.requested_by,
        source_update_id=intent.source_update_id,
        body=intent.body,
        completion_chat_id=intent.completion_address.chat_id,
        completion_message_thread_id=intent.completion_address.message_thread_id,
        completion_reply_to_message_id=(intent.completion_address.reply_to_message_id),
        recipient_count=2,
        status=AnnouncementStatus.COMPLETED,
        created_at=intent.requested_at,
        updated_at=completed_at,
        completed_at=completed_at,
    )

    assert isinstance(restored.state, CompletedAnnouncement)
    with pytest.raises(ValueError, match="ID disagrees"):
        Announcement.restore(
            announcement_id=AnnouncementId(
                UUID("00000000-0000-0000-0000-000000000999")
            ),
            idempotency_key=intent.idempotency_key,
            requested_by=intent.requested_by,
            source_update_id=intent.source_update_id,
            body=intent.body,
            completion_chat_id=intent.completion_address.chat_id,
            completion_message_thread_id=None,
            completion_reply_to_message_id=9,
            recipient_count=2,
            status=AnnouncementStatus.DELIVERING,
            created_at=NOW,
            updated_at=NOW,
            completed_at=None,
        )
    with pytest.raises(ValueError, match="must equal updated_at"):
        Announcement.restore(
            announcement_id=AnnouncementId.for_idempotency_key(intent.idempotency_key),
            idempotency_key=intent.idempotency_key,
            requested_by=intent.requested_by,
            source_update_id=intent.source_update_id,
            body=intent.body,
            completion_chat_id=intent.completion_address.chat_id,
            completion_message_thread_id=intent.completion_address.message_thread_id,
            completion_reply_to_message_id=9,
            recipient_count=2,
            status=AnnouncementStatus.COMPLETED,
            created_at=NOW,
            updated_at=completed_at,
            completed_at=completed_at + timedelta(seconds=1),
        )


def test_replay_comparison_is_domain_semantic_and_decisions_are_sealed() -> None:
    """@brief replay 比较完整领域意图且决策不能公开伪造 / Replay compares complete domain intent and decisions cannot be forged publicly."""

    announcement = Announcement.start(_intent())
    announcement.require_same_intent(_intent())
    with pytest.raises(AnnouncementIntentMismatch):
        announcement.require_same_intent(replace(_intent(), body="different body"))
    with pytest.raises(AnnouncementIntentMismatch):
        announcement.require_same_intent(
            replace(
                _intent(),
                completion_address=AnnouncementCompletionAddress(43, 5, 9),
            )
        )

    for decision_type in (
        AnnouncementAudienceSnapshotted,
        AnnouncementDeliveryStarted,
        AnnouncementDeliveryCompleted,
    ):
        with pytest.raises(TypeError):
            decision_type()
