"""@brief Durable inbox 聚合状态机测试 / Durable-inbox aggregate state-machine tests."""

from datetime import datetime, timedelta, timezone

import pytest

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    LeaseToken,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import (
    AwaitingInboxClaim,
    DeadLetteredInboxItem,
    InboxClaim,
    InboxFailure,
    InboxItem,
    InboxStatus,
    InboundUpdate,
    InvalidInboxTransition,
    ProcessedInboxItem,
    ProcessingInboxItem,
    WaitingInboxRetry,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
"""@brief 测试基准时刻 / Test reference time."""


def _update(*, payload: JsonObject | None = None) -> InboundUpdate:
    """@brief 创建测试入口事实 / Build a test inbound fact.

    @param payload 可选 payload / Optional payload.
    @return 入口事实 / Inbound fact.
    """

    return InboundUpdate.pending(
        update_id=UpdateId(42),
        conversation_id=ConversationId("assistant-user:7"),
        payload=payload or {"update_id": 42, "message": {"text": "hello"}},
        received_at=NOW,
    )


def _claim() -> InboxClaim:
    """@brief 创建第一次 processing claim / Build a first processing claim.

    @return 测试 claim / Test claim.
    """

    return InboxItem.receive(_update()).claim(
        token=LeaseToken.new(),
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(minutes=1),
    )


def test_inbound_update_is_a_fact_not_a_job_snapshot() -> None:
    """@brief 路由事实不暴露持久化 job 状态 / The routed fact exposes no persistence-job state."""

    update = _update()

    assert update.received_at == NOW
    for leaked_field in (
        "status",
        "version",
        "attempt_count",
        "next_attempt_at",
        "processed_at",
        "last_error",
    ):
        assert not hasattr(update, leaked_field)


def test_inbound_payload_is_deeply_isolated_from_callers() -> None:
    """@brief 嵌套 payload 不能绕过 frozen 外壳修改入口事实 / Nested payloads cannot bypass the frozen shell to mutate the inbound fact."""

    source: JsonObject = {"items": ["first"]}
    update = _update(payload=source)
    source_items = source["items"]
    assert isinstance(source_items, list)
    source_items.append("source mutation")

    exposed = update.payload
    exposed_items = exposed["items"]
    assert isinstance(exposed_items, list)
    exposed_items.append("consumer mutation")

    assert update.payload == {"items": ["first"]}


def test_receive_and_claim_are_explicit_versioned_transitions() -> None:
    """@brief receive/claim 精确推进版本与 attempt / Receive and claim advance version and attempt exactly."""

    pending = InboxItem.receive(_update())

    assert pending.status is InboxStatus.PENDING
    assert isinstance(pending.state, AwaitingInboxClaim)
    assert pending.version == 0
    assert pending.attempt_count == 0
    assert pending.next_attempt_at == NOW

    claim = pending.claim(
        token=LeaseToken.new(),
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(minutes=1),
    )

    assert isinstance(claim.item.state, ProcessingInboxItem)
    assert claim.item.status is InboxStatus.PROCESSING
    assert claim.expected_version == 1
    assert claim.item.attempt_count == 1
    assert claim.item.next_attempt_at is None
    with pytest.raises(InvalidInboxTransition, match="cannot be claimed"):
        claim.item.claim(
            token=LeaseToken.new(),
            claimed_at=NOW + timedelta(seconds=2),
            lease_expires_at=NOW + timedelta(minutes=2),
        )


def test_retry_and_reclaim_preserve_failure_then_clear_it() -> None:
    """@brief retry 保存失败，下一次 claim 显式清除失败 / Retry retains failure and the next claim explicitly clears it."""

    claim = _claim()
    failed_at = NOW + timedelta(seconds=2)
    retry_at = NOW + timedelta(seconds=10)
    decision = claim.item.retry(
        claim,
        failed_at=failed_at,
        retry_at=retry_at,
        failure=InboxFailure(" provider timeout "),
    )

    waiting = decision.item
    assert isinstance(waiting.state, WaitingInboxRetry)
    assert waiting.status is InboxStatus.RETRY_WAIT
    assert waiting.version == 2
    assert waiting.attempt_count == 1
    assert waiting.next_attempt_at == retry_at
    assert waiting.last_error == "provider timeout"

    reclaimed = waiting.claim(
        token=LeaseToken.new(),
        claimed_at=retry_at,
        lease_expires_at=retry_at + timedelta(minutes=1),
    )

    assert reclaimed.item.status is InboxStatus.PROCESSING
    assert reclaimed.item.version == 3
    assert reclaimed.item.attempt_count == 2
    assert reclaimed.item.last_error is None


def test_success_and_dead_letter_are_distinct_terminal_decisions() -> None:
    """@brief 成功与 dead-letter 是不同终态且不能再次 settlement / Success and dead letter are distinct terminal states rejecting further settlement."""

    success_claim = _claim()
    success = success_claim.item.succeed(
        success_claim,
        processed_at=NOW + timedelta(seconds=2),
    )
    assert isinstance(success.item.state, ProcessedInboxItem)
    assert success.item.status is InboxStatus.PROCESSED
    assert success.item.processed_at == NOW + timedelta(seconds=2)
    assert success.item.version == 2

    failure_claim = _claim()
    dead_letter = failure_claim.item.dead_letter(
        failure_claim,
        failed_at=NOW + timedelta(seconds=2),
        failure=InboxFailure("invalid payload"),
    )
    assert isinstance(dead_letter.item.state, DeadLetteredInboxItem)
    assert dead_letter.item.status is InboxStatus.FAILED_FINAL
    assert dead_letter.item.last_error == "invalid payload"
    assert dead_letter.item.next_attempt_at is None

    with pytest.raises(InvalidInboxTransition, match="cannot be settled"):
        success.item.succeed(
            success_claim,
            processed_at=NOW + timedelta(seconds=3),
        )


def test_transition_time_failure_and_lease_invariants_are_owned_by_domain() -> None:
    """@brief 时间、失败与 lease 约束在 domain 拒绝 / Time, failure, and lease constraints are rejected in the domain."""

    pending = InboxItem.receive(_update())
    with pytest.raises(ValueError, match="current version"):
        pending.claim(
            token=LeaseToken.new(),
            claimed_at=NOW - timedelta(seconds=1),
            lease_expires_at=NOW + timedelta(minutes=1),
        )
    waiting = InboxItem.restore(
        update=_update(),
        status=InboxStatus.RETRY_WAIT,
        version=2,
        attempt_count=1,
        next_attempt_at=NOW + timedelta(seconds=5),
        updated_at=NOW,
        processed_at=None,
        last_error="timeout",
    )
    with pytest.raises(ValueError, match="before next_attempt_at"):
        waiting.claim(
            token=LeaseToken.new(),
            claimed_at=NOW + timedelta(seconds=1),
            lease_expires_at=NOW + timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="expire after claim time"):
        pending.claim(
            token=LeaseToken.new(),
            claimed_at=NOW,
            lease_expires_at=NOW,
        )

    claim = _claim()
    with pytest.raises(ValueError, match="later than failed_at"):
        claim.item.retry(
            claim,
            failed_at=NOW + timedelta(seconds=2),
            retry_at=NOW + timedelta(seconds=2),
            failure=InboxFailure("timeout"),
        )
    with pytest.raises(ValueError, match="cannot be empty"):
        InboxFailure("  ")
    with pytest.raises(TypeError, match="must be a string"):
        InboxFailure(42)  # type: ignore[arg-type]


def test_restore_rejects_nullable_field_combinations_that_schema_cannot_represent() -> None:
    """@brief restore 拒绝 enum 加可空字段形成的非法组合 / Restore rejects illegal enum-plus-nullable-field combinations."""

    with pytest.raises(ValueError, match="Processing inbox state"):
        InboxItem.restore(
            update=_update(),
            status=InboxStatus.PROCESSING,
            version=1,
            attempt_count=1,
            next_attempt_at=NOW,
            updated_at=NOW,
            processed_at=None,
            last_error=None,
        )
    with pytest.raises(ValueError, match="Retrying inbox state"):
        InboxItem.restore(
            update=_update(),
            status=InboxStatus.RETRY_WAIT,
            version=2,
            attempt_count=1,
            next_attempt_at=NOW,
            updated_at=NOW,
            processed_at=None,
            last_error=None,
        )
    with pytest.raises(ValueError, match="unclaimed initial version"):
        InboxItem.restore(
            update=_update(),
            status=InboxStatus.PENDING,
            version=1,
            attempt_count=0,
            next_attempt_at=NOW,
            updated_at=NOW,
            processed_at=None,
            last_error=None,
        )


def test_lease_recovery_row_matches_retry_state_and_preserves_attempt_count() -> None:
    """@brief set-based lease recovery 的行形状符合领域 retry 不变量 / Set-based lease recovery row shape satisfies domain retry invariants."""

    claim = _claim()
    recovered_at = claim.lease_expires_at
    recovered = InboxItem.restore(
        update=claim.update,
        status=InboxStatus.RETRY_WAIT,
        version=claim.expected_version + 1,
        attempt_count=claim.item.attempt_count,
        next_attempt_at=recovered_at,
        updated_at=recovered_at,
        processed_at=None,
        last_error="recovered expired worker lease",
    )

    assert recovered.status is InboxStatus.RETRY_WAIT
    assert recovered.next_attempt_at == recovered.updated_at
    assert recovered.attempt_count == claim.item.attempt_count
    assert recovered.version == claim.item.version + 1


def test_replay_semantics_ignore_observation_metadata_but_not_payload() -> None:
    """@brief 幂等重放忽略接收观测但拒绝 payload 替换 / Idempotent replay ignores receipt observations but rejects payload replacement."""

    first = _update()
    replay = InboundUpdate.pending(
        update_id=first.update_id,
        conversation_id=first.conversation_id,
        payload=first.payload,
        received_at=NOW + timedelta(seconds=5),
    )
    conflict = _update(payload={"update_id": 42, "message": {"text": "changed"}})

    assert first.is_replay_of(replay)
    assert not first.is_replay_of(conflict)
