"""@brief Admin durable recipient 领域生命周期测试 / Admin durable-recipient domain lifecycle tests."""

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fogmoe_bot.domain.admin.announcement import (
    AnnouncementDeliveryCounts,
    AnnouncementDispatchContent,
    AnnouncementId,
)
from fogmoe_bot.domain.admin.recipient import (
    AnnouncementClaimCapability,
    AnnouncementClaimToken,
    AnnouncementCompletionReleased,
    AnnouncementFailureCategory,
    AnnouncementRecipient,
    AnnouncementRecipientClaim,
    AnnouncementRecipientDeadLettered,
    AnnouncementRecipientExpanded,
    AnnouncementRecipientKind,
    AnnouncementRecipientLeaseRecovered,
    AnnouncementRecipientRetryScheduled,
    AnnouncementRecipientStatus,
    BlockedAnnouncementRecipient,
    ExpandedAnnouncementRecipient,
    FailedAnnouncementRecipient,
    PendingAnnouncementRecipient,
    ProcessingAnnouncementRecipient,
    RetryWaitingAnnouncementRecipient,
)
from fogmoe_bot.domain.conversation.identity import OutboundMessageId

NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定领域测试时刻 / Fixed domain-test instant."""

TOKEN = AnnouncementClaimToken(UUID("00000000-0000-0000-0000-000000000111"))
"""@brief 固定 fencing token / Fixed fencing token."""


def _restore(
    *,
    kind: AnnouncementRecipientKind = AnnouncementRecipientKind.USER,
    status: AnnouncementRecipientStatus = AnnouncementRecipientStatus.PENDING,
    attempt_count: int = 0,
    next_attempt_at: datetime | None = NOW,
    claim_token: AnnouncementClaimToken | None = None,
    lease_expires_at: datetime | None = None,
    outbound_message_id: OutboundMessageId | None = None,
    last_error: str | None = None,
    updated_at: datetime = NOW,
    expanded_at: datetime | None = None,
    terminal_at: datetime | None = None,
) -> AnnouncementRecipient:
    """@brief 通过唯一 restore 入口构造测试聚合 / Build a test aggregate through the sole restore entrypoint.

    @return 已验证 recipient / Validated recipient.
    """

    return AnnouncementRecipient.restore(
        announcement_id=AnnouncementId.for_idempotency_key("admin:test"),
        recipient_kind=kind,
        chat_id=42,
        message_thread_id=7 if kind is AnnouncementRecipientKind.COMPLETION else None,
        reply_to_message_id=9 if kind is AnnouncementRecipientKind.COMPLETION else None,
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=next_attempt_at,
        claim_token=claim_token,
        lease_expires_at=lease_expires_at,
        outbound_message_id=outbound_message_id,
        last_error=last_error,
        created_at=NOW,
        updated_at=updated_at,
        expanded_at=expanded_at,
        terminal_at=terminal_at,
    )


def _content() -> AnnouncementDispatchContent:
    """@brief 构造不可变公告内容 / Build immutable announcement content.

    @return 已验证内容 / Validated content.
    """

    return AnnouncementDispatchContent(
        body="hello",
        counts=AnnouncementDeliveryCounts(recipients=4, delivered=3, failed=1),
        announcement_created_at=NOW,
    )


def _claim(*, attempt_count: int = 0) -> AnnouncementRecipientClaim:
    """@brief 从 pending/retry 聚合领取测试能力 / Claim a test capability from a pending or retry aggregate.

    @param attempt_count 领取前尝试数 / Pre-claim attempt count.
    @return processing claim 能力 / Processing claim capability.
    """

    recipient = (
        _restore()
        if attempt_count == 0
        else _restore(
            status=AnnouncementRecipientStatus.RETRY_WAIT,
            attempt_count=attempt_count,
            last_error="previous_error",
        )
    )
    return recipient.claim(
        token=TOKEN,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(minutes=1),
        content=_content(),
    )


def test_blocked_is_a_real_completion_only_state() -> None:
    """@brief blocked 被完整建模且仅 completion 可用 / blocked is fully modeled and completion-only."""

    recipient = _restore(
        kind=AnnouncementRecipientKind.COMPLETION,
        status=AnnouncementRecipientStatus.BLOCKED,
        next_attempt_at=None,
    )

    assert isinstance(recipient.state, BlockedAnnouncementRecipient)
    assert recipient.attempt_count == 0
    with pytest.raises(ValueError, match="Only completion"):
        _restore(
            status=AnnouncementRecipientStatus.BLOCKED,
            next_attempt_at=None,
        )


def test_claim_is_a_token_lease_capability_and_increments_once() -> None:
    """@brief claim 封装 token/lease 且只增一次 attempt / A claim encapsulates token/lease and increments attempt exactly once."""

    claim = _claim()

    assert isinstance(claim.recipient.state, ProcessingAnnouncementRecipient)
    assert claim.capability.token == TOKEN
    assert claim.capability.claimed_at == NOW
    assert claim.capability.lease_expires_at == NOW + timedelta(minutes=1)
    assert claim.recipient.attempt_count == 1
    assert claim.content.body == "hello"
    assert (
        claim.content.counts.recipients,
        claim.content.counts.delivered,
        claim.content.counts.failed,
    ) == (
        4,
        3,
        1,
    )
    assert not hasattr(claim.recipient, "version")
    assert not hasattr(claim, "claim_token")
    assert tuple(field.name for field in fields(AnnouncementRecipientClaim)) == (
        "recipient",
        "content",
        "capability",
    )
    with pytest.raises(TypeError):
        AnnouncementRecipientClaim()


def test_claim_rejects_not_due_and_non_claimable_states() -> None:
    """@brief 尚未到期或非等待态不能领取 / Not-due and non-waiting recipients cannot be claimed."""

    future = NOW + timedelta(minutes=1)
    recipient = _restore(next_attempt_at=future, updated_at=future)
    with pytest.raises(ValueError, match="not due"):
        recipient.claim(
            token=TOKEN,
            claimed_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=2),
            content=_content(),
        )

    processing = _claim().recipient
    with pytest.raises(ValueError, match="not claimable"):
        processing.claim(
            token=TOKEN,
            claimed_at=NOW,
            lease_expires_at=NOW + timedelta(minutes=2),
            content=_content(),
        )


def test_claim_produces_typed_expanded_retry_and_dead_letter_decisions() -> None:
    """@brief 三种结算由 claim 产生穷尽类型化后态 / Claim produces the three typed settlement post-states."""

    claim = _claim(attempt_count=2)
    completed_at = NOW + timedelta(seconds=2)
    outbound_id = OutboundMessageId.parse(UUID("00000000-0000-0000-0000-000000000222"))

    expanded = claim.expand(
        outbound_message_id=outbound_id,
        completed_at=completed_at,
    )
    retry = claim.retry(
        retry_at=completed_at,
        failure=AnnouncementFailureCategory(" retryable "),
    )
    dead = claim.dead_letter(
        failed_at=completed_at,
        failure=AnnouncementFailureCategory("permanent"),
    )

    assert expanded.claim.capability.token == TOKEN
    assert isinstance(expanded.recipient.state, ExpandedAnnouncementRecipient)
    assert expanded.recipient.state.outbound_message_id == outbound_id
    assert isinstance(retry.recipient.state, RetryWaitingAnnouncementRecipient)
    assert retry.recipient.state.failure.value == "retryable"
    assert isinstance(dead.recipient.state, FailedAnnouncementRecipient)
    assert dead.recipient.state.failure.value == "permanent"
    assert {
        expanded.recipient.attempt_count,
        retry.recipient.attempt_count,
        dead.recipient.attempt_count,
    } == {3}


def test_error_category_is_normalized_truncated_and_never_empty() -> None:
    """@brief 错误类别在领域中去空白、截断并提供 unknown / Error category is stripped, truncated, and never empty in the domain."""

    assert AnnouncementFailureCategory("  ").value == "unknown"
    assert AnnouncementFailureCategory("x" * 101).value == "x" * 100
    assert AnnouncementFailureCategory.from_exception(RuntimeError()).value == (
        "RuntimeError"
    )


def test_restore_rejects_cross_state_columns_and_invalid_counts() -> None:
    """@brief restore 拒绝跨状态残留列和越界计数 / Restore rejects cross-state residue and excessive delivery counts."""

    with pytest.raises(ValueError, match="persistence shape"):
        _restore(claim_token=TOKEN)
    with pytest.raises(ValueError, match="failure category"):
        _restore(
            status=AnnouncementRecipientStatus.RETRY_WAIT,
            attempt_count=1,
            last_error=None,
        )
    with pytest.raises(ValueError, match="exceed"):
        AnnouncementDeliveryCounts(recipients=1, delivered=1, failed=1)


def test_completion_release_is_domain_owned_and_rejects_other_states() -> None:
    """@brief 仅 blocked completion 可由领域释放 / Only a blocked completion can be released by the domain."""

    blocked = _restore(
        kind=AnnouncementRecipientKind.COMPLETION,
        status=AnnouncementRecipientStatus.BLOCKED,
        next_attempt_at=None,
    )
    released_at = NOW + timedelta(seconds=1)
    decision = blocked.release_completion(released_at=released_at)

    assert decision.blocked_recipient is blocked
    assert decision.recipient.status is AnnouncementRecipientStatus.PENDING
    assert isinstance(decision.recipient.state, PendingAnnouncementRecipient)
    assert decision.recipient.state.next_attempt_at == released_at
    assert decision.recipient.attempt_count == 0
    with pytest.raises(ValueError, match="blocked completion"):
        _restore().release_completion(released_at=released_at)


def test_recovery_validates_lease_preserves_attempt_and_carries_capability() -> None:
    """@brief recovery 校验到期边界并保留 attempt/capability / Recovery validates expiry and preserves attempt and capability."""

    processing = _claim(attempt_count=2).recipient
    state = processing.state
    assert isinstance(state, ProcessingAnnouncementRecipient)
    with pytest.raises(ValueError, match="has not expired"):
        processing.recover_expired(
            recovered_at=state.capability.lease_expires_at
            - timedelta(microseconds=1)
        )

    decision = processing.recover_expired(
        recovered_at=state.capability.lease_expires_at
    )
    recovered = decision.recipient
    assert decision.processing_recipient is processing
    assert decision.capability == state.capability
    assert recovered.attempt_count == processing.attempt_count == 3
    assert isinstance(recovered.state, RetryWaitingAnnouncementRecipient)
    assert recovered.state.failure.value == "lease_expired"


def test_tokens_and_persistence_decisions_cannot_be_forged_publicly() -> None:
    """@brief nil token 与公开 decision 构造均被拒绝 / Nil tokens and public decision construction are rejected."""

    with pytest.raises(ValueError, match="nil UUID"):
        AnnouncementClaimToken(UUID(int=0))
    capability = AnnouncementClaimCapability(
        TOKEN,
        NOW,
        NOW + timedelta(minutes=1),
    )
    recipient = _claim().recipient
    for decision_type in (
        AnnouncementCompletionReleased,
        AnnouncementRecipientLeaseRecovered,
        AnnouncementRecipientExpanded,
        AnnouncementRecipientRetryScheduled,
        AnnouncementRecipientDeadLettered,
    ):
        with pytest.raises(TypeError):
            decision_type(  # type: ignore[call-arg]
                capability=capability,
                recipient=recipient,
            )
