"""@brief Dreaming 聚合的纯领域状态机测试 / Pure domain-state-machine tests for the Dreaming aggregate."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from fogmoe_bot.domain.user_profile.dream import (
    CompletedDream,
    DreamActivity,
    DreamActivityDraft,
    DreamActivityStatus,
    DreamClaim,
    DreamCompletion,
    DreamCompletionEvaluated,
    DreamCompletionPrepared,
    DreamFailedFinalDecision,
    DreamFailure,
    DreamFailureAttempt,
    DreamLease,
    DreamLeaseRecoveryFailedFinal,
    DreamLeaseRecoveryRetry,
    DreamLeaseToken,
    DreamResult,
    DreamRetryScheduled,
    FailedDreamFinal,
    InvalidDreamTransition,
    PendingDream,
    ProcessingDream,
    ProfileBaseline,
    WaitingDreamRetry,
)
from fogmoe_bot.domain.user_profile.models import (
    DreamId,
    ProfileClaim,
    ProfileClaimKind,
    ProfileConfidence,
    ProfileDocument,
    ProfileEvidence,
    ProfileMetadata,
    ProfilePatch,
    UpsertProfileClaim,
)

CREATED_AT = datetime(2035, 4, 5, 6, 7, tzinfo=UTC)
"""@brief Dream 建立时间 / Dream creation time."""

CLAIMED_AT = CREATED_AT + timedelta(minutes=1)
"""@brief 首次领取时间 / First claim time."""

LEASE_EXPIRES_AT = CLAIMED_AT + timedelta(minutes=2)
"""@brief 首次租约截止时间 / First lease deadline."""

TOKEN_ONE = DreamLeaseToken.parse("00000000-0000-0000-0000-000000000101")
"""@brief 首次 claim token / First claim token."""

TOKEN_TWO = DreamLeaseToken.parse("00000000-0000-0000-0000-000000000102")
"""@brief 后续 claim token / Subsequent claim token."""


def _metadata() -> ProfileMetadata:
    """@brief 构造冻结元信息 / Build frozen metadata."""

    return ProfileMetadata("Klee", "klee", "CS researcher")


def _draft(
    *,
    revision: int = 0,
    watermark: int = 0,
    through_event_id: int | None = None,
    created_at: datetime = CREATED_AT,
) -> DreamActivityDraft:
    """@brief 构造合法 Dream 意图 / Build a valid Dream intent."""

    through = watermark + 1 if through_event_id is None else through_event_id
    return DreamActivityDraft(
        dream_id=DreamId(UUID("00000000-0000-0000-0000-000000000099")),
        owner_user_id=42,
        baseline=ProfileBaseline(
            revision=revision,
            observed_through_event_id=watermark,
        ),
        through_event_id=through,
        source_count=1,
        metadata=_metadata(),
        created_at=created_at,
    )


def _evidence(event_id: int) -> tuple[ProfileEvidence, ...]:
    """@brief 构造单条冻结 evidence / Build one frozen evidence item."""

    return (
        ProfileEvidence(
            event_id=event_id,
            source_turn_id=UUID(f"00000000-0000-0000-0000-{event_id:012d}"),
            owner_user_id=42,
            user_text="I prefer tea",
            assistant_text="Understood",
            occurred_at=CLAIMED_AT,
            metadata=_metadata(),
        ),
    )


def _existing_document() -> ProfileDocument:
    """@brief 构造已 materialize 的 Profile / Build a materialized Profile document."""

    return ProfileDocument(
        (
            ProfileClaim(
                key="drink.preference",
                kind=ProfileClaimKind.PREFERENCE,
                statement="偏好咖啡",
                confidence=ProfileConfidence.EXPLICIT,
                evidence_event_ids=(1,),
                observed_at=CREATED_AT,
            ),
        )
    )


def _claim(
    *,
    revision: int = 0,
    watermark: int = 0,
    current_document: ProfileDocument | None = None,
    token: DreamLeaseToken = TOKEN_ONE,
    claimed_at: datetime = CLAIMED_AT,
    lease_expires_at: datetime = LEASE_EXPIRES_AT,
) -> DreamClaim:
    """@brief 通过聚合转换签发合法 claim / Issue a valid claim through an aggregate transition."""

    pending = DreamActivity.enqueue(_draft(revision=revision, watermark=watermark))
    return pending.claim(
        token=token,
        claimed_at=claimed_at,
        lease_expires_at=lease_expires_at,
        current_document=current_document or ProfileDocument(),
        evidence=_evidence(watermark + 1),
    )


def _updated_result(event_id: int) -> DreamResult:
    """@brief 构造会改变 Profile 的结果 / Build a Profile-changing result."""

    return DreamResult(
        patch=ProfilePatch(
            (
                UpsertProfileClaim(
                    key="drink.preference",
                    kind=ProfileClaimKind.PREFERENCE,
                    statement="偏好茶",
                    confidence=ProfileConfidence.EXPLICIT,
                    evidence_event_ids=(event_id,),
                ),
            )
        ),
        route_key="test:profile-model",
        prompt_version=3,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param("owner_user_id", 42.0, id="floating-owner"),
        pytest.param("owner_user_id", True, id="boolean-owner"),
        pytest.param("through_event_id", 1.0, id="floating-watermark"),
        pytest.param("through_event_id", True, id="boolean-watermark"),
        pytest.param("source_count", 1.0, id="floating-source-count"),
        pytest.param("source_count", True, id="boolean-source-count"),
    ),
)
def test_draft_rejects_values_that_are_not_actual_integers(
    field: str,
    value: object,
) -> None:
    """@brief Draft 在运行时拒绝 bool 与 float 伪整数 / Draft rejects bool and float pseudo-integers at runtime.

    @param field 被破坏的整数域 / Integer field under test.
    @param value 非整数运行时值 / Non-integer runtime value.
    @return None / None.
    """

    values: dict[str, object] = {
        "dream_id": DreamId(UUID("00000000-0000-0000-0000-000000000099")),
        "owner_user_id": 42,
        "baseline": ProfileBaseline(revision=0, observed_through_event_id=0),
        "through_event_id": 1,
        "source_count": 1,
        "metadata": _metadata(),
        "created_at": CREATED_AT,
    }
    values[field] = value

    with pytest.raises(ValueError):
        DreamActivityDraft(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "constructor",
    (
        pytest.param(lambda: DreamActivity(), id="activity"),
        pytest.param(lambda: DreamLease(), id="lease"),
        pytest.param(lambda: DreamClaim(), id="claim"),
        pytest.param(
            lambda: DreamCompletionEvaluated(),
            id="completion-evaluated",
        ),
        pytest.param(
            lambda: DreamCompletionPrepared(),
            id="completion-prepared",
        ),
        pytest.param(lambda: DreamCompletion(), id="completion"),
        pytest.param(
            lambda: DreamFailureAttempt(),
            id="failure-attempt",
        ),
        pytest.param(
            lambda: DreamRetryScheduled(),
            id="retry-decision",
        ),
        pytest.param(
            lambda: DreamFailedFinalDecision(),
            id="final-decision",
        ),
        pytest.param(
            lambda: DreamLeaseRecoveryRetry(),
            id="recovery-retry-decision",
        ),
        pytest.param(
            lambda: DreamLeaseRecoveryFailedFinal(),
            id="recovery-final-decision",
        ),
    ),
)
def test_aggregate_capabilities_and_decisions_have_sealed_constructors(
    constructor: Callable[[], object],
) -> None:
    """@brief 聚合与 capability/decision 只能由领域转换创建 / Aggregates, capabilities, and decisions can only be created by domain transitions."""

    with pytest.raises(TypeError):
        constructor()


def test_completion_evaluation_cannot_inject_derived_document_or_changed() -> None:
    """@brief 空 patch 不能伪造任意 document/changed / An empty patch cannot forge an arbitrary document or changed flag.

    @return None / None.
    """

    current = _existing_document()
    claim = _claim(revision=2, watermark=5, current_document=current)
    result = DreamResult(ProfilePatch(), "test:no-op", 1)

    with pytest.raises(TypeError):
        DreamCompletionEvaluated._create(  # type: ignore[call-arg]
            claim,
            result=result,
            document=ProfileDocument(),
            changed=True,
        )
    with pytest.raises(TypeError, match="private to the domain module"):
        DreamCompletionEvaluated._create(object(), claim, result=result)

    evaluated = claim.evaluate_result(result)
    assert evaluated.document == current
    assert evaluated.changed is False


def test_claim_freezes_mutable_evidence_before_workload_validation() -> None:
    """@brief claim 在校验 workload 前 tuple 化可变 evidence / A claim tuple-freezes mutable evidence before validating its workload.

    @return None / None.
    """

    pending = DreamActivity.enqueue(_draft())
    evidence = list(_evidence(1))

    claim = pending.claim(
        token=TOKEN_ONE,
        claimed_at=CLAIMED_AT,
        lease_expires_at=LEASE_EXPIRES_AT,
        current_document=ProfileDocument(),
        evidence=evidence,
    )
    evidence.clear()

    assert isinstance(claim.evidence, tuple)
    assert tuple(item.event_id for item in claim.evidence) == (1,)


def test_claim_requires_latest_evidence_metadata_to_match_frozen_intent() -> None:
    """@brief claim 拒绝与最新 evidence metadata 不一致的冻结意图 / A claim rejects an intent inconsistent with the latest evidence metadata.

    @return None / None.
    """

    pending = DreamActivity.enqueue(_draft())
    mismatched = (
        ProfileEvidence(
            event_id=1,
            source_turn_id=UUID("00000000-0000-0000-0000-000000000001"),
            owner_user_id=42,
            user_text="I prefer tea",
            assistant_text="Understood",
            occurred_at=CLAIMED_AT,
            metadata=ProfileMetadata("Different", None, ""),
        ),
    )

    with pytest.raises(ValueError, match="frozen source range"):
        pending.claim(
            token=TOKEN_ONE,
            claimed_at=CLAIMED_AT,
            lease_expires_at=LEASE_EXPIRES_AT,
            current_document=ProfileDocument(),
            evidence=mismatched,
        )


def test_dream_result_canonicalizes_route_provenance() -> None:
    """@brief Dream result 在派生 snapshot 前规范 route provenance / A Dream result canonicalizes route provenance before snapshot derivation.

    @return None / None.
    """

    result = DreamResult(_updated_result(1).patch, "  test:model  ", 1)
    completion = _claim().prepare_completion(
        result,
        completed_at=CLAIMED_AT + timedelta(seconds=10),
    )
    snapshot = completion.plan_profile_commit(
        has_backlog=False,
        refresh_after=timedelta(hours=6),
    ).snapshot(profile_created_at=CREATED_AT)

    assert result.route_key == "test:model"
    assert snapshot is not None
    assert snapshot.route_key == "test:model"


def test_restore_recovers_each_of_the_five_persisted_states() -> None:
    """@brief restore 穷尽恢复五种持久化状态 / Restore exhaustively recovers all five persisted states."""

    draft = _draft()
    result = DreamResult(ProfilePatch(), "test:no-op", 1)
    retry_updated_at = CLAIMED_AT + timedelta(seconds=10)
    retry_at = CLAIMED_AT + timedelta(seconds=20)
    completed_at = CLAIMED_AT + timedelta(seconds=30)
    cases: tuple[
        tuple[
            DreamActivityStatus,
            int,
            datetime | None,
            DreamLeaseToken | None,
            datetime | None,
            DreamResult | None,
            str | None,
            datetime,
            datetime | None,
            type[object],
        ],
        ...,
    ] = (
        (
            DreamActivityStatus.PENDING,
            0,
            CREATED_AT,
            None,
            None,
            None,
            None,
            CREATED_AT,
            None,
            PendingDream,
        ),
        (
            DreamActivityStatus.PROCESSING,
            1,
            None,
            TOKEN_ONE,
            LEASE_EXPIRES_AT,
            None,
            None,
            CLAIMED_AT,
            None,
            ProcessingDream,
        ),
        (
            DreamActivityStatus.RETRY_WAIT,
            1,
            retry_at,
            None,
            None,
            None,
            "temporary provider failure",
            retry_updated_at,
            None,
            WaitingDreamRetry,
        ),
        (
            DreamActivityStatus.COMPLETED,
            1,
            None,
            None,
            None,
            result,
            None,
            completed_at,
            completed_at,
            CompletedDream,
        ),
        (
            DreamActivityStatus.FAILED_FINAL,
            1,
            None,
            None,
            None,
            None,
            "permanent provider failure",
            completed_at,
            completed_at,
            FailedDreamFinal,
        ),
    )

    for (
        status,
        version,
        next_attempt_at,
        claim_token,
        lease_expires_at,
        restored_result,
        last_error,
        updated_at,
        terminal_at,
        expected_state,
    ) in cases:
        activity = DreamActivity.restore(
            draft=draft,
            status=status,
            version=version,
            attempt_count=version,
            next_attempt_at=next_attempt_at,
            claim_token=claim_token,
            lease_expires_at=lease_expires_at,
            result=restored_result,
            last_error=last_error,
            updated_at=updated_at,
            completed_at=terminal_at,
        )

        assert activity.status is status
        assert isinstance(activity.state, expected_state)
        assert (activity.version, activity.attempt_count) == (version, version)
        assert activity.next_attempt_at == next_attempt_at
        assert activity.result == restored_result
        assert activity.last_error == last_error
        assert activity.completed_at == terminal_at


@pytest.mark.parametrize(
    "overrides",
    (
        pytest.param(
            {"claim_token": None},
            id="processing-without-token",
        ),
        pytest.param(
            {"lease_expires_at": None},
            id="processing-without-lease-deadline",
        ),
        pytest.param(
            {"next_attempt_at": CLAIMED_AT},
            id="processing-with-retry-time",
        ),
        pytest.param(
            {"version": 2},
            id="version-attempt-diverge",
        ),
    ),
)
def test_restore_rejects_inconsistent_processing_rows(
    overrides: dict[str, object],
) -> None:
    """@brief restore 拒绝不完整 ownership 与计数漂移 / Restore rejects incomplete ownership and counter drift."""

    values: dict[str, object] = {
        "draft": _draft(),
        "status": DreamActivityStatus.PROCESSING,
        "version": 1,
        "attempt_count": 1,
        "next_attempt_at": None,
        "claim_token": TOKEN_ONE,
        "lease_expires_at": LEASE_EXPIRES_AT,
        "result": None,
        "last_error": None,
        "updated_at": CLAIMED_AT,
        "completed_at": None,
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        DreamActivity.restore(**values)  # type: ignore[arg-type]


def test_only_claim_advances_version_and_attempt_count() -> None:
    """@brief 仅 claim 同步推进 version/attempt / Only a claim advances version and attempt together."""

    pending = DreamActivity.enqueue(_draft())
    assert (pending.version, pending.attempt_count) == (0, 0)

    first_claim = pending.claim(
        token=TOKEN_ONE,
        claimed_at=CLAIMED_AT,
        lease_expires_at=LEASE_EXPIRES_AT,
        current_document=ProfileDocument(),
        evidence=_evidence(1),
    )
    assert (first_claim.activity.version, first_claim.activity.attempt_count) == (
        1,
        1,
    )

    failure = first_claim.record_failure(
        failed_at=CLAIMED_AT + timedelta(seconds=10),
        failure=DreamFailure("temporary provider failure"),
    )
    retry = failure.schedule_retry(retry_at=CLAIMED_AT + timedelta(seconds=20))
    assert (retry.activity.version, retry.activity.attempt_count) == (1, 1)

    second_claim = retry.activity.claim(
        token=TOKEN_TWO,
        claimed_at=CLAIMED_AT + timedelta(seconds=20),
        lease_expires_at=LEASE_EXPIRES_AT + timedelta(minutes=1),
        current_document=ProfileDocument(),
        evidence=_evidence(1),
    )
    assert (second_claim.activity.version, second_claim.activity.attempt_count) == (
        2,
        2,
    )


def test_retry_final_completion_and_recovery_preserve_claim_version() -> None:
    """@brief 所有 settlement 与 recovery 均保持 claim version/attempt / Every settlement and recovery preserves the claim version and attempt."""

    claim = _claim()
    expected = (claim.activity.version, claim.activity.attempt_count)
    failure = claim.record_failure(
        failed_at=CLAIMED_AT + timedelta(seconds=10),
        failure=DreamFailure("provider failure"),
    )
    retry = failure.schedule_retry(retry_at=CLAIMED_AT + timedelta(seconds=20))
    final = failure.fail_final()
    completion = claim.prepare_completion(
        DreamResult(ProfilePatch(), "test:no-op", 1),
        completed_at=CLAIMED_AT + timedelta(seconds=10),
    )
    lease = DreamLease.restore(claim.activity)
    recovery = claim.activity.recover_expired_lease(
        lease,
        recovered_at=LEASE_EXPIRES_AT,
        failure=DreamFailure("recovered expired Dream lease"),
        max_attempts=5,
    )

    assert (retry.activity.version, retry.activity.attempt_count) == expected
    assert (final.activity.version, final.activity.attempt_count) == expected
    assert (completion.activity.version, completion.activity.attempt_count) == expected
    assert (recovery.activity.version, recovery.activity.attempt_count) == expected


def test_completion_distinguishes_updated_and_no_op_profile_commits() -> None:
    """@brief completion 显式区分 revision 更新与 no-op / Completion explicitly distinguishes revision updates from no-op."""

    updated_claim = _claim()
    updated = updated_claim.prepare_completion(
        _updated_result(1),
        completed_at=CLAIMED_AT + timedelta(seconds=10),
    )
    updated_plan = updated.plan_profile_commit(
        has_backlog=False,
        refresh_after=timedelta(hours=6),
    )

    assert updated.changed is True
    assert updated.activity.status is DreamActivityStatus.COMPLETED
    assert updated.document.claims[0].statement == "偏好茶"
    assert updated_plan.profile_revision == 1
    assert updated_plan.observed_through_event_id == 1
    snapshot = updated_plan.snapshot(profile_created_at=CREATED_AT)
    assert snapshot is not None
    assert snapshot.revision == 1
    assert snapshot.document == updated.document

    current = _existing_document()
    no_op_claim = _claim(revision=2, watermark=5, current_document=current)
    no_op = no_op_claim.prepare_completion(
        DreamResult(ProfilePatch(), "test:no-op", 2),
        completed_at=CLAIMED_AT + timedelta(seconds=20),
    )
    no_op_plan = no_op.plan_profile_commit(
        has_backlog=False,
        refresh_after=timedelta(hours=6),
    )

    assert no_op.changed is False
    assert no_op.document == current
    assert no_op_plan.profile_revision == 2
    assert no_op_plan.observed_through_event_id == 6
    assert no_op_plan.snapshot(profile_created_at=CREATED_AT) is None


@pytest.mark.parametrize(
    ("has_backlog", "expected_delay"),
    (
        pytest.param(True, timedelta(), id="backlog-is-immediate"),
        pytest.param(False, timedelta(hours=6), id="idle-uses-refresh-delay"),
    ),
)
def test_completion_plans_eligibility_from_backlog(
    has_backlog: bool,
    expected_delay: timedelta,
) -> None:
    """@brief backlog 立即再调度，否则应用 refresh delay / Backlog reschedules immediately; otherwise the refresh delay applies."""

    completed_at = CLAIMED_AT + timedelta(seconds=10)
    completion = _claim().prepare_completion(
        DreamResult(ProfilePatch(), "test:no-op", 1),
        completed_at=completed_at,
    )

    plan = completion.plan_profile_commit(
        has_backlog=has_backlog,
        refresh_after=timedelta(hours=6),
    )

    assert plan.next_eligible_at == completed_at + expected_delay


def test_expired_lease_recovery_fails_final_when_attempt_budget_is_exhausted() -> None:
    """@brief max_attempts=1 时首次 crash 直接终败 / With max_attempts=1, the first crashed claim fails finally.

    @return None / None.
    """

    claim = _claim()
    recovered_at = LEASE_EXPIRES_AT
    decision = claim.activity.recover_expired_lease(
        DreamLease.restore(claim.activity),
        recovered_at=recovered_at,
        failure=DreamFailure("worker crashed"),
        max_attempts=1,
    )

    assert isinstance(decision, DreamLeaseRecoveryFailedFinal)
    assert decision.activity.status is DreamActivityStatus.FAILED_FINAL
    assert decision.activity.completed_at == recovered_at
    assert decision.activity.next_attempt_at is None
    assert (decision.activity.version, decision.activity.attempt_count) == (1, 1)


def test_expired_lease_recovery_retries_while_attempt_budget_remains() -> None:
    """@brief 未耗尽尝试预算时 crash 进入 retry-wait / A crash enters retry-wait while attempt budget remains.

    @return None / None.
    """

    claim = _claim()
    recovered_at = LEASE_EXPIRES_AT
    decision = claim.activity.recover_expired_lease(
        DreamLease.restore(claim.activity),
        recovered_at=recovered_at,
        failure=DreamFailure("worker crashed"),
        max_attempts=2,
    )

    assert isinstance(decision, DreamLeaseRecoveryRetry)
    assert decision.activity.status is DreamActivityStatus.RETRY_WAIT
    assert decision.activity.next_attempt_at == recovered_at
    assert decision.activity.completed_at is None
    assert (decision.activity.version, decision.activity.attempt_count) == (1, 1)


def test_recovery_rejects_live_and_stale_leases() -> None:
    """@brief recovery 拒绝未到期与旧 ownership lease / Recovery rejects live and stale ownership leases."""

    first_claim = _claim()
    first_lease = DreamLease.restore(first_claim.activity)
    with pytest.raises(InvalidDreamTransition, match="before expiry"):
        first_claim.activity.recover_expired_lease(
            first_lease,
            recovered_at=LEASE_EXPIRES_AT - timedelta(microseconds=1),
            failure=DreamFailure("too early"),
            max_attempts=5,
        )

    wrong_token_activity = DreamActivity.restore(
        draft=_draft(),
        status=DreamActivityStatus.PROCESSING,
        version=1,
        attempt_count=1,
        next_attempt_at=None,
        claim_token=TOKEN_TWO,
        lease_expires_at=LEASE_EXPIRES_AT,
        result=None,
        last_error=None,
        updated_at=CLAIMED_AT,
        completed_at=None,
    )
    with pytest.raises(InvalidDreamTransition, match="does not own"):
        wrong_token_activity.recover_expired_lease(
            first_lease,
            recovered_at=LEASE_EXPIRES_AT,
            failure=DreamFailure("wrong token"),
            max_attempts=5,
        )

    recovered = first_claim.activity.recover_expired_lease(
        first_lease,
        recovered_at=LEASE_EXPIRES_AT,
        failure=DreamFailure("expired"),
        max_attempts=5,
    )
    second_claim = recovered.activity.claim(
        token=TOKEN_TWO,
        claimed_at=LEASE_EXPIRES_AT,
        lease_expires_at=LEASE_EXPIRES_AT + timedelta(minutes=2),
        current_document=ProfileDocument(),
        evidence=_evidence(1),
    )

    with pytest.raises(InvalidDreamTransition, match="does not own"):
        second_claim.activity.recover_expired_lease(
            first_lease,
            recovered_at=LEASE_EXPIRES_AT + timedelta(minutes=2),
            failure=DreamFailure("stale owner"),
            max_attempts=5,
        )
