"""@brief Passage 向量任务领域状态矩阵测试 / Passage-vector job domain state-matrix tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fogmoe_bot.domain.retrieval import (
    AwaitingPassageVector,
    CompletedPassageVector,
    FailedPassageVector,
    InvalidPassageVectorTransition,
    PassageVectorClaim,
    PassageVectorFailure,
    PassageVectorJob,
    PassageVectorJobKey,
    PassageVectorStatus,
    ProcessingPassageVector,
    RetrievalPassage,
    RetrievalScope,
    WaitingPassageVectorRetry,
)
from fogmoe_bot.domain.retrieval.models import EmbeddingSpace, EmbeddingVector

NOW = datetime(2035, 1, 2, 3, 4, 5, tzinfo=UTC)
"""@brief 确定性领域测试时刻 / Deterministic domain-test instant."""

TOKEN = UUID("00000000-0000-0000-0000-000000000041")
"""@brief 确定性 fencing token / Deterministic fencing token."""

SPACE = EmbeddingSpace(
    space_id="retrieval.test",
    model="test-model",
    dimensions=2,
    query_instruction="Represent the query",
    passage_format_version=1,
)
"""@brief 两维测试 embedding space / Two-dimensional test embedding space."""

PASSAGE = RetrievalPassage.create(
    corpus_id="conversation.episodic",
    scope=RetrievalScope("personal", 42),
    source_kind="conversation.turn",
    source_id=UUID("00000000-0000-0000-0000-000000000042"),
    ordinal=0,
    format_version=1,
    text="User: remember the vector lifecycle",
    occurred_at=NOW - timedelta(minutes=1),
)
"""@brief 与测试 space 兼容的 Passage / Passage compatible with the test space."""


def _pending() -> PassageVectorJob:
    """@brief 创建规范 pending 聚合 / Create a canonical pending aggregate.

    @return 初始向量任务 / Initial vector job.
    """

    return PassageVectorJob.create_pending(
        PassageVectorJobKey(PASSAGE.passage_id, SPACE.space_id),
        created_at=NOW,
    )


def _claim(*, claimed_at: datetime = NOW) -> PassageVectorClaim:
    """@brief 通过领域转换创建 sealed claim / Create a sealed claim through a domain transition.

    @param claimed_at 领取时刻 / Claim instant.
    @return Processing capability / Processing capability.
    """

    return (
        _pending()
        .claim(
            passage=PASSAGE,
            space=SPACE,
            claim_token=TOKEN,
            claimed_at=claimed_at,
            lease_for=timedelta(seconds=30),
        )
        .claim
    )


def test_create_claim_and_complete_own_the_counter_transitions() -> None:
    """@brief 聚合拥有 claim 与 completion 的版本和 attempt 变化 / Aggregate owns claim and completion counter changes."""

    pending = _pending()
    assert isinstance(pending.state, AwaitingPassageVector)
    assert pending.version == pending.attempt_count == 0
    claimed = pending.claim(
        passage=PASSAGE,
        space=SPACE,
        claim_token=TOKEN,
        claimed_at=NOW,
        lease_for=timedelta(seconds=30),
    )
    assert claimed.previous == pending
    assert isinstance(claimed.job.state, ProcessingPassageVector)
    assert claimed.job.version == claimed.job.attempt_count == 1
    assert claimed.job.state.lease_expires_at == NOW + timedelta(seconds=30)

    completed_at = NOW + timedelta(seconds=31)
    completed = claimed.job.complete(
        claimed.claim,
        EmbeddingVector((0.25, 0.75)),
        completed_at=completed_at,
    )
    assert isinstance(completed.job.state, CompletedPassageVector)
    assert completed.job.version == 2
    assert completed.job.attempt_count == 1
    assert completed.job.updated_at == completed_at
    assert completed.job.state.completed_at == completed_at


def test_completion_remains_valid_after_deadline_until_recovery_commits() -> None:
    """@brief 仅时钟经过不会撤销尚未恢复的 token / Passage of time alone does not revoke an unrecovered token."""

    claim = _claim()
    completed = claim.job.complete(
        claim,
        EmbeddingVector((1.0, 0.5)),
        completed_at=NOW + timedelta(minutes=5),
    )
    assert isinstance(completed.job.state, CompletedPassageVector)


def test_settlement_time_cannot_precede_the_claimed_version() -> None:
    """@brief 完成、重试和终止都不能让聚合时间倒退 / Completion, retry, and failure cannot regress aggregate time."""

    claim = _claim()
    before_claim = NOW - timedelta(microseconds=1)
    with pytest.raises(ValueError, match="cannot precede"):
        claim.job.complete(
            claim,
            EmbeddingVector((0.25, 0.75)),
            completed_at=before_claim,
        )
    with pytest.raises(ValueError, match="cannot precede"):
        claim.job.schedule_retry(
            claim,
            retry_at=NOW + timedelta(seconds=1),
            failure=PassageVectorFailure("retry"),
            failed_at=before_claim,
        )
    with pytest.raises(ValueError, match="cannot precede"):
        claim.job.fail(
            claim,
            failure=PassageVectorFailure("final"),
            failed_at=before_claim,
        )


def test_retry_fail_and_recovery_preserve_started_attempts() -> None:
    """@brief 结算与 crash recovery 不会消耗新的 attempt / Settlement and crash recovery do not consume a new attempt."""

    claim = _claim()
    failure = PassageVectorFailure("  provider timeout  ")
    retry = claim.job.schedule_retry(
        claim,
        retry_at=NOW + timedelta(seconds=6),
        failure=failure,
        failed_at=NOW + timedelta(seconds=5),
    )
    assert isinstance(retry.job.state, WaitingPassageVectorRetry)
    assert retry.job.state.failure.summary == "provider timeout"
    assert retry.job.version == 2 and retry.job.attempt_count == 1

    second = retry.job.claim(
        passage=PASSAGE,
        space=SPACE,
        claim_token=UUID("00000000-0000-0000-0000-000000000043"),
        claimed_at=retry.job.state.next_attempt_at,
        lease_for=timedelta(seconds=10),
    )
    failed = second.job.fail(
        second.claim,
        failure=PassageVectorFailure("permanent"),
        failed_at=NOW + timedelta(seconds=7),
    )
    assert isinstance(failed.job.state, FailedPassageVector)
    assert failed.job.version == 4 and failed.job.attempt_count == 2

    recovered = claim.job.recover_expired(recovered_at=NOW + timedelta(seconds=30))
    assert isinstance(recovered.job.state, WaitingPassageVectorRetry)
    assert recovered.job.state.next_attempt_at == recovered.job.updated_at
    assert recovered.job.version == 2 and recovered.job.attempt_count == 1
    assert recovered.previous == claim.job


def test_due_and_expired_boundaries_are_inclusive_but_live_recovery_is_rejected() -> (
    None
):
    """@brief Claim 与 recovery 都在等号边界生效，活租约被拒绝 / Claim and recovery activate at equality while a live lease is rejected."""

    pending = _pending()
    with pytest.raises(InvalidPassageVectorTransition, match="not due"):
        pending.claim(
            passage=PASSAGE,
            space=SPACE,
            claim_token=TOKEN,
            claimed_at=NOW - timedelta(microseconds=1),
            lease_for=timedelta(seconds=30),
        )
    claim = _claim()
    with pytest.raises(InvalidPassageVectorTransition, match="Live"):
        claim.job.recover_expired(
            recovered_at=NOW + timedelta(seconds=30, microseconds=-1)
        )
    claim.job.recover_expired(recovered_at=NOW + timedelta(seconds=30))


def test_restore_is_exhaustive_and_accepts_non_formula_counters() -> None:
    """@brief Restore 校验五态形状但不猜测 2n 公式 / Restore validates five state shapes without guessing a 2n formula."""

    key = PassageVectorJobKey(PASSAGE.passage_id, SPACE.space_id)
    processing = PassageVectorJob.restore(
        key=key,
        status=PassageVectorStatus.PROCESSING,
        version=17,
        attempt_count=3,
        next_attempt_at=None,
        claim_token=TOKEN,
        lease_expires_at=NOW + timedelta(seconds=2),
        vector=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
        completed_at=None,
    )
    assert processing.version == 17 and processing.attempt_count == 3

    retry = PassageVectorJob.restore(
        key=key,
        status=PassageVectorStatus.RETRY_WAIT,
        version=18,
        attempt_count=3,
        next_attempt_at=NOW + timedelta(seconds=3),
        claim_token=None,
        lease_expires_at=None,
        vector=None,
        last_error="retry",
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=3),
        completed_at=None,
    )
    assert isinstance(retry.state, WaitingPassageVectorRetry)

    completed = PassageVectorJob.restore(
        key=key,
        status=PassageVectorStatus.COMPLETED,
        version=23,
        attempt_count=3,
        next_attempt_at=None,
        claim_token=None,
        lease_expires_at=None,
        vector=EmbeddingVector((0.2, 0.8)),
        last_error=None,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=4),
        completed_at=NOW + timedelta(seconds=4),
    )
    assert isinstance(completed.state, CompletedPassageVector)

    failed = PassageVectorJob.restore(
        key=key,
        status=PassageVectorStatus.FAILED_FINAL,
        version=24,
        attempt_count=3,
        next_attempt_at=None,
        claim_token=None,
        lease_expires_at=None,
        vector=None,
        last_error="final",
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=5),
        completed_at=None,
    )
    assert isinstance(failed.state, FailedPassageVector)


@pytest.mark.parametrize(
    ("status", "fields"),
    (
        (
            PassageVectorStatus.PENDING,
            {"next_attempt_at": NOW, "last_error": "unexpected"},
        ),
        (
            PassageVectorStatus.RETRY_WAIT,
            {"next_attempt_at": NOW, "last_error": None},
        ),
        (
            PassageVectorStatus.PROCESSING,
            {"claim_token": TOKEN, "lease_expires_at": None},
        ),
        (
            PassageVectorStatus.COMPLETED,
            {"vector": EmbeddingVector((1.0, 0.5)), "completed_at": None},
        ),
        (
            PassageVectorStatus.FAILED_FINAL,
            {"last_error": "failure", "claim_token": TOKEN},
        ),
    ),
)
def test_restore_rejects_every_partial_or_cross_state_shape(
    status: PassageVectorStatus,
    fields: dict[str, object],
) -> None:
    """@brief Restore 拒绝半 lease、半 result 与跨状态字段 / Restore rejects partial leases, partial results, and cross-state fields.

    @param status 待恢复状态 / Status being restored.
    @param fields 覆盖 nullable 列 / Nullable-column overrides.
    """

    values: dict[str, object] = {
        "next_attempt_at": None,
        "claim_token": None,
        "lease_expires_at": None,
        "vector": None,
        "last_error": None,
        "completed_at": None,
    }
    values.update(fields)
    with pytest.raises(ValueError, match="inconsistent"):
        PassageVectorJob.restore(
            key=PassageVectorJobKey(PASSAGE.passage_id, SPACE.space_id),
            status=status,
            version=1,
            attempt_count=0 if status is PassageVectorStatus.PENDING else 1,
            next_attempt_at=values["next_attempt_at"],  # type: ignore[arg-type]
            claim_token=values["claim_token"],  # type: ignore[arg-type]
            lease_expires_at=values["lease_expires_at"],  # type: ignore[arg-type]
            vector=values["vector"],  # type: ignore[arg-type]
            last_error=values["last_error"],  # type: ignore[arg-type]
            created_at=NOW,
            updated_at=NOW,
            completed_at=values["completed_at"],  # type: ignore[arg-type]
        )


def test_state_specific_counter_and_timestamp_invariants_are_strict() -> None:
    """@brief Counter 与状态时间关系由一个不变量门约束 / One invariant gate enforces counters and state timestamps."""

    key = PassageVectorJobKey(PASSAGE.passage_id, SPACE.space_id)
    with pytest.raises(ValueError, match="zero attempts"):
        PassageVectorJob.restore(
            key=key,
            status=PassageVectorStatus.PENDING,
            version=1,
            attempt_count=1,
            next_attempt_at=NOW,
            claim_token=None,
            lease_expires_at=None,
            vector=None,
            last_error=None,
            created_at=NOW,
            updated_at=NOW,
            completed_at=None,
        )
    with pytest.raises(ValueError, match="positive attempt"):
        PassageVectorJob.restore(
            key=key,
            status=PassageVectorStatus.FAILED_FINAL,
            version=1,
            attempt_count=0,
            next_attempt_at=None,
            claim_token=None,
            lease_expires_at=None,
            vector=None,
            last_error="failure",
            created_at=NOW,
            updated_at=NOW,
            completed_at=None,
        )
    with pytest.raises(ValueError, match="trail attempt_count"):
        PassageVectorJob.restore(
            key=key,
            status=PassageVectorStatus.FAILED_FINAL,
            version=1,
            attempt_count=2,
            next_attempt_at=None,
            claim_token=None,
            lease_expires_at=None,
            vector=None,
            last_error="failure",
            created_at=NOW,
            updated_at=NOW,
            completed_at=None,
        )
    with pytest.raises(ValueError, match="timestamps must equal"):
        PassageVectorJob.restore(
            key=key,
            status=PassageVectorStatus.PENDING,
            version=0,
            attempt_count=0,
            next_attempt_at=NOW + timedelta(microseconds=1),
            claim_token=None,
            lease_expires_at=None,
            vector=None,
            last_error=None,
            created_at=NOW,
            updated_at=NOW,
            completed_at=None,
        )
    with pytest.raises(ValueError, match="lease must follow"):
        PassageVectorJob.restore(
            key=key,
            status=PassageVectorStatus.PROCESSING,
            version=1,
            attempt_count=1,
            next_attempt_at=None,
            claim_token=TOKEN,
            lease_expires_at=NOW,
            vector=None,
            last_error=None,
            created_at=NOW,
            updated_at=NOW,
            completed_at=None,
        )
    with pytest.raises(ValueError, match="retry cannot precede"):
        PassageVectorJob.restore(
            key=key,
            status=PassageVectorStatus.RETRY_WAIT,
            version=2,
            attempt_count=1,
            next_attempt_at=NOW,
            claim_token=None,
            lease_expires_at=None,
            vector=None,
            last_error="retry",
            created_at=NOW,
            updated_at=NOW + timedelta(microseconds=1),
            completed_at=None,
        )
    with pytest.raises(ValueError, match="timestamp must equal"):
        PassageVectorJob.restore(
            key=key,
            status=PassageVectorStatus.COMPLETED,
            version=2,
            attempt_count=1,
            next_attempt_at=None,
            claim_token=None,
            lease_expires_at=None,
            vector=EmbeddingVector((0.5, 0.5)),
            last_error=None,
            created_at=NOW,
            updated_at=NOW + timedelta(seconds=1),
            completed_at=NOW + timedelta(seconds=2),
        )
    with pytest.raises(ValueError, match="cannot precede created_at"):
        PassageVectorJob.restore(
            key=key,
            status=PassageVectorStatus.FAILED_FINAL,
            version=2,
            attempt_count=1,
            next_attempt_at=None,
            claim_token=None,
            lease_expires_at=None,
            vector=None,
            last_error="failure",
            created_at=NOW + timedelta(seconds=1),
            updated_at=NOW,
            completed_at=None,
        )


def test_claim_binds_work_and_public_models_are_immutable() -> None:
    """@brief Sealed claim 绑定 Passage/space，聚合字段公开但不可修改 / Sealed claim binds Passage/space while aggregate fields remain public and immutable."""

    claim = _claim()
    assert claim.passage is PASSAGE
    assert claim.space is SPACE
    with pytest.raises(FrozenInstanceError):
        claim.job.version = 99  # type: ignore[misc]
    with pytest.raises(TypeError):
        PassageVectorClaim()  # type: ignore[call-arg]
    mismatched = EmbeddingSpace(
        space_id="retrieval.other",
        model=SPACE.model,
        dimensions=SPACE.dimensions,
        query_instruction=SPACE.query_instruction,
        passage_format_version=SPACE.passage_format_version,
    )
    with pytest.raises(ValueError, match="space"):
        _pending().claim(
            passage=PASSAGE,
            space=mismatched,
            claim_token=TOKEN,
            claimed_at=NOW,
            lease_for=timedelta(seconds=1),
        )


def test_failure_is_trimmed_bounded_and_nonblank() -> None:
    """@brief Failure 摘要保持原有 strip、1000 字符上限与非空语义 / Failure preserves strip, 1000-character bound, and nonblank semantics."""

    assert PassageVectorFailure("  " + "x" * 1_100).summary == "x" * 1_000
    with pytest.raises(ValueError, match="blank"):
        PassageVectorFailure("   ")
