"""@brief Transactional outbox 领域状态机测试 / Transactional-outbox domain state-machine tests."""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    LeaseToken,
    MessageSequence,
    OutboundMessageId,
    TurnId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    CancelledOutboundMessage,
    DeadLetteredOutboundMessage,
    DeliveredOutboundMessage,
    InvalidOutboundTransition,
    OutboundClaim,
    OutboundDraft,
    OutboundFailure,
    OutboundMessage,
    OutboundStatus,
    ProcessingOutboundDelivery,
    WaitingOutboundRetry,
)

NOW = datetime(2026, 7, 31, 10, tzinfo=timezone.utc)
"""@brief 状态机测试基准时间 / State-machine test reference time."""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


def _draft() -> OutboundDraft:
    """@brief 构造规范出站意图 / Build a canonical outbound intent.

    @return 测试出站意图 / Test outbound intent.
    """

    return OutboundDraft(
        message_id=OutboundMessageId.new(),
        conversation_id=ConversationId("telegram:chat:7:user:42"),
        turn_id=TurnId.new(),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:7"),
        kind=SEND_TELEGRAM_MESSAGE,
        payload={"chat_id": 7, "text": "hello", "metadata": {"reply": True}},
        idempotency_key="turn:answer:0",
        created_at=NOW,
    )


def _processing_claim() -> tuple[OutboundMessage, OutboundClaim]:
    """@brief 领取初始消息 / Claim an initial message.

    @return processing 聚合与 claim / Processing aggregate and claim.
    """

    queued = OutboundMessage.enqueue(_draft(), stream_sequence=MessageSequence(1))
    claim = queued.claim(
        token=LeaseToken.new(),
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    return claim.message, claim


def test_aggregate_and_claim_cannot_be_forged_through_public_construction() -> None:
    """@brief 聚合和 capability 必须由领域流程签发 / Aggregate and capability must be issued by domain flows."""

    with pytest.raises(TypeError, match="OutboundMessage.enqueue"):
        OutboundMessage()
    with pytest.raises(TypeError, match="issued by OutboundMessage.claim"):
        OutboundClaim()


def test_outbound_aggregate_owns_retry_reclaim_and_success_lifecycle() -> None:
    """@brief 聚合拥有 claim→retry→claim→success 全部转换 / The aggregate owns claim-to-retry-to-reclaim-to-success transitions."""

    queued = OutboundMessage.enqueue(_draft(), stream_sequence=MessageSequence(3))
    assert queued.status is OutboundStatus.PENDING
    assert queued.version == 0
    assert queued.attempt_count == 0

    first_claim = queued.claim(
        token=LeaseToken.new(),
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    assert isinstance(first_claim.message.state, ProcessingOutboundDelivery)
    assert first_claim.expected_version == 1
    assert first_claim.message.attempt_count == 1

    retry = first_claim.message.retry(
        first_claim,
        failed_at=NOW + timedelta(seconds=2),
        retry_at=NOW + timedelta(seconds=7),
        failure=OutboundFailure("  network reset  "),
    )
    assert isinstance(retry.message.state, WaitingOutboundRetry)
    assert retry.message.version == 2
    assert retry.message.attempt_count == 1
    assert retry.message.last_error == "network reset"

    second_claim = retry.message.claim(
        token=LeaseToken.new(),
        claimed_at=NOW + timedelta(seconds=7),
        lease_expires_at=NOW + timedelta(minutes=2),
    )
    assert second_claim.expected_version == 3
    assert second_claim.message.attempt_count == 2
    delivered = second_claim.message.succeed(
        second_claim,
        delivered_at=NOW + timedelta(seconds=8),
        external_message_id="telegram:99",
    )

    assert isinstance(delivered.message.state, DeliveredOutboundMessage)
    assert delivered.message.status is OutboundStatus.DELIVERED
    assert delivered.message.version == 4
    assert delivered.message.attempt_count == 2
    assert delivered.message.external_message_id == "telegram:99"


def test_outbound_failure_and_plan_cancellation_are_explicit_terminal_states() -> None:
    """@brief 最终失败与计划取消是不同终态 / Final failure and plan cancellation are distinct terminal states."""

    queued = OutboundMessage.enqueue(_draft(), stream_sequence=MessageSequence(1))
    cancellation = queued.cancel(
        cancelled_at=NOW + timedelta(seconds=1),
        reason=OutboundFailure("delivery plan cancelled"),
    )
    assert isinstance(cancellation.message.state, CancelledOutboundMessage)
    assert cancellation.message.status is OutboundStatus.CANCELLED
    assert cancellation.message.attempt_count == 0

    claim = OutboundMessage.enqueue(
        _draft(),
        stream_sequence=MessageSequence(2),
    ).claim(
        token=LeaseToken.new(),
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    dead_letter = claim.message.dead_letter(
        claim,
        failed_at=NOW + timedelta(seconds=2),
        failure=OutboundFailure("permission denied"),
    )
    assert isinstance(dead_letter.message.state, DeadLetteredOutboundMessage)
    assert dead_letter.message.status is OutboundStatus.FAILED_FINAL
    assert dead_letter.message.last_error == "permission denied"


def test_expired_lease_recovery_preserves_attempt_and_requires_expiry() -> None:
    """@brief lease recovery 只推进版本且必须等待到期 / Lease recovery advances only the version and must wait for expiry."""

    message, claim = _processing_claim()
    with pytest.raises(InvalidOutboundTransition, match="before it expires"):
        message.recover_expired_lease(
            claim,
            recovered_at=claim.lease_expires_at - timedelta(microseconds=1),
            retry_at=claim.lease_expires_at + timedelta(microseconds=1),
            failure=OutboundFailure("recovered expired worker lease"),
        )

    recovered_at = claim.lease_expires_at
    recovery = message.recover_expired_lease(
        claim,
        recovered_at=recovered_at,
        retry_at=recovered_at + timedelta(microseconds=1),
        failure=OutboundFailure("recovered expired worker lease"),
    )

    assert recovery.message.status is OutboundStatus.RETRY_WAIT
    assert recovery.message.version == claim.expected_version + 1
    assert recovery.message.attempt_count == claim.message.attempt_count
    assert recovery.message.updated_at == recovered_at
    assert recovery.message.next_attempt_at == recovered_at + timedelta(microseconds=1)


def test_stale_or_foreign_claim_cannot_settle_processing_version() -> None:
    """@brief 其他消息的 capability 不能终结当前 processing 版本 / Another message's capability cannot settle the current processing version."""

    current_message, current_claim = _processing_claim()
    foreign_message, foreign_claim = _processing_claim()
    del foreign_message

    with pytest.raises(InvalidOutboundTransition, match="does not own"):
        current_message.succeed(
            foreign_claim,
            delivered_at=NOW + timedelta(seconds=2),
            external_message_id=None,
        )

    settled = current_message.succeed(
        current_claim,
        delivered_at=NOW + timedelta(seconds=2),
        external_message_id=None,
    )
    with pytest.raises(InvalidOutboundTransition, match="cannot be settled"):
        settled.message.dead_letter(
            current_claim,
            failed_at=NOW + timedelta(seconds=3),
            failure=OutboundFailure("late failure"),
        )


@pytest.mark.parametrize(
    ("status", "next_attempt_at", "delivered_at", "last_error"),
    [
        (OutboundStatus.PENDING, None, None, None),
        (OutboundStatus.PROCESSING, NOW, None, None),
        (OutboundStatus.RETRY_WAIT, NOW, None, None),
        (OutboundStatus.DELIVERED, None, None, None),
        (OutboundStatus.FAILED_FINAL, None, None, None),
    ],
)
def test_restore_rejects_impossible_nullable_field_combinations(
    status: OutboundStatus,
    next_attempt_at: datetime | None,
    delivered_at: datetime | None,
    last_error: str | None,
) -> None:
    """@brief hydration 拒绝隐式且不完整的状态组合 / Hydration rejects implicit and incomplete state combinations.

    @param status 持久化状态 / Persisted status.
    @param next_attempt_at 可选领取时刻 / Optional claim time.
    @param delivered_at 可选成功时刻 / Optional success time.
    @param last_error 可选失败 / Optional failure.
    @return None / None.
    """

    with pytest.raises(ValueError, match="inconsistent persistence fields"):
        OutboundMessage.restore(
            draft=_draft(),
            stream_sequence=MessageSequence(1),
            status=status,
            version=1,
            attempt_count=1,
            next_attempt_at=next_attempt_at,
            updated_at=NOW,
            delivered_at=delivered_at,
            external_message_id=None,
            last_error=last_error,
        )


def test_message_payload_projection_cannot_mutate_the_owned_intent() -> None:
    """@brief payload projection 不允许调用方修改聚合意图 / Payload projection cannot mutate the aggregate's intent."""

    message = OutboundMessage.enqueue(_draft(), stream_sequence=MessageSequence(1))
    projected = message.draft.payload
    projected["text"] = "mutated"
    nested = projected["metadata"]
    assert isinstance(nested, dict)
    nested["reply"] = False

    assert message.draft.payload["text"] == "hello"
    assert message.draft.payload["metadata"] == {"reply": True}


def test_outbox_worker_persists_domain_settlements_instead_of_setter_calls() -> None:
    """@brief 静态门禁止 worker 重获 setter 式生命周期 / Static gate prevents setter-style lifecycle from returning to the worker."""

    domain_path = (
        PROJECT_ROOT / "src/fogmoe_bot/domain/conversation/outbox.py"
    )
    worker_path = (
        PROJECT_ROOT / "src/fogmoe_bot/application/conversation/outbox_worker.py"
    )
    tree = ast.parse(domain_path.read_text(encoding="utf-8"), filename=str(domain_path))
    classes = {
        node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
    }
    aggregate_methods = {
        node.name
        for node in classes["OutboundMessage"].body
        if isinstance(node, ast.FunctionDef)
    }
    assert {
        "enqueue",
        "restore",
        "claim",
        "succeed",
        "retry",
        "dead_letter",
        "cancel",
    } <= aggregate_methods

    worker_source = worker_path.read_text(encoding="utf-8")
    for setter_style_operation in (
        "mark_outbound_delivered",
        "retry_outbound",
        "fail_outbound",
    ):
        assert setter_style_operation not in worker_source
