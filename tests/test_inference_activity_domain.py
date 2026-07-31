"""@brief 推理活动富领域模型状态机测试 / Rich inference-activity domain-state-machine tests."""

import ast
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from conversation_workflow_testkit import NOW, _activity_draft

from fogmoe_bot.domain.conversation.identity import LeaseToken, TurnRevision
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityClaim,
    InferenceActivityLease,
    InferenceActivityStatus,
    InferenceFailedFinal,
    InferenceFailure,
    InferenceFailureAttempt,
    InferenceGenerationCause,
    InferenceRetryBudgetCharge,
    InferenceSucceeded,
    InvalidInferenceTransition,
)

TOKEN_ONE = LeaseToken.parse("11111111-aaaa-4aaa-8aaa-111111111111")
"""@brief 第一代测试 lease token / First-generation test lease token."""

TOKEN_TWO = LeaseToken.parse("22222222-bbbb-4bbb-8bbb-222222222222")
"""@brief 第二代测试 lease token / Second-generation test lease token."""

TOKEN_THREE = LeaseToken.parse("33333333-cccc-4ccc-8ccc-333333333333")
"""@brief 第三代测试 lease token / Third-generation test lease token."""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 仓库根目录 / Repository root."""


def _initial_claim() -> InferenceActivityClaim:
    """@brief 生成严格的首次 processing capability / Produce a strict initial-processing capability.

    @return 首次 generation claim / Initial-generation claim.
    """

    pending = InferenceActivity.enqueue(_activity_draft())
    return pending.claim(
        token=TOKEN_ONE,
        claimed_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=30),
    )


def _processing_after_retry() -> InferenceActivityClaim:
    """@brief 生成已消耗一次普通预算的 retry generation / Produce a retry generation after one ordinary budget charge.

    @return attempt=2、budget=1 的 claim / Claim with attempt two and budget one.
    """

    first = _initial_claim()
    failure = first.activity.record_failure(
        first,
        failed_at=NOW + timedelta(seconds=1),
        failure=InferenceFailure("ordinary provider failure"),
        budget_charge=InferenceRetryBudgetCharge.CONSUME,
    )
    waiting = failure.schedule_retry(retry_at=NOW + timedelta(seconds=2)).activity
    return waiting.claim(
        token=TOKEN_TWO,
        claimed_at=NOW + timedelta(seconds=2),
        lease_expires_at=NOW + timedelta(seconds=10),
    )


def _restore(
    status: InferenceActivityStatus,
    **overrides: object,
) -> InferenceActivity:
    """@brief 用默认合法标量恢复指定状态 / Restore one state from default valid persistence scalars.

    @param status 目标持久化状态 / Target persisted status.
    @param overrides 覆盖默认标量 / Scalar overrides.
    @return 严格恢复的聚合 / Strictly restored aggregate.
    """

    defaults: dict[str, object] = {
        "draft": _activity_draft(),
        "status": status,
        "version": 1,
        "attempt_count": 1,
        "retry_budget_used": 0,
        "next_attempt_at": None,
        "updated_at": NOW,
        "completed_at": None,
        "completion_token": None,
        "last_error": None,
        "input_revision": TurnRevision.initial(),
    }
    if status is InferenceActivityStatus.PENDING:
        defaults.update(
            version=0,
            attempt_count=0,
            next_attempt_at=NOW,
        )
    elif status is InferenceActivityStatus.STEER_PENDING:
        defaults.update(
            version=2,
            next_attempt_at=NOW,
            input_revision=TurnRevision(1),
        )
    elif status is InferenceActivityStatus.RETRY:
        defaults.update(
            version=2,
            next_attempt_at=NOW + timedelta(seconds=1),
            last_error="temporary failure",
        )
    elif status is InferenceActivityStatus.COMPLETED:
        defaults.update(
            version=2,
            completed_at=NOW,
            completion_token=TOKEN_ONE,
        )
    elif status is InferenceActivityStatus.FAILED:
        defaults.update(version=2, last_error="final failure")
    elif status is InferenceActivityStatus.CANCELLED:
        defaults.update(attempt_count=0)
    defaults.update(overrides)
    return InferenceActivity.restore(**defaults)  # type: ignore[arg-type]


def test_domain_capabilities_and_decisions_cannot_be_forged() -> None:
    """@brief 封闭构造器阻止伪造聚合、lease、claim 与 settlement / Closed constructors prevent forged aggregates, leases, claims, and settlements."""

    with pytest.raises(TypeError, match="enqueue"):
        InferenceActivity()
    with pytest.raises(TypeError, match="restore"):
        InferenceActivityLease()
    with pytest.raises(TypeError, match="claim"):
        InferenceActivityClaim()
    with pytest.raises(TypeError, match="aggregate transitions"):
        InferenceFailureAttempt()
    with pytest.raises(TypeError, match="aggregate transitions"):
        InferenceSucceeded()
    with pytest.raises(TypeError, match="aggregate transitions"):
        InferenceFailedFinal()


def test_inference_facade_is_the_only_external_domain_entrypoint() -> None:
    """@brief internal 生命周期模块不泄漏给应用与适配器 / Internal lifecycle modules do not leak into applications or adapters."""

    source_root = PROJECT_ROOT / "src" / "fogmoe_bot"
    conversation_root = source_root / "domain" / "conversation"
    facade_path = conversation_root / "inference.py"
    facade_tree = ast.parse(
        facade_path.read_text(encoding="utf-8"),
        filename=str(facade_path),
    )
    facade_imports = {
        node.module
        for node in facade_tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 1
    }
    assert facade_imports == {
        "_inference_activity",
        "_inference_decision",
        "_inference_state",
    }
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in facade_tree.body
    )

    internal_modules = {
        "fogmoe_bot.domain.conversation._inference_activity",
        "fogmoe_bot.domain.conversation._inference_decision",
        "fogmoe_bot.domain.conversation._inference_state",
    }
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        if path.parent == conversation_root:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in internal_modules:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
            elif isinstance(node, ast.Import):
                offenders.extend(
                    str(path.relative_to(PROJECT_ROOT))
                    for alias in node.names
                    if alias.name in internal_modules
                )
    assert offenders == []


def test_activity_is_read_only_and_defensively_owns_its_request() -> None:
    """@brief 聚合公开状态不可变且 request 不泄漏可变引用 / Aggregate state is immutable and its request leaks no mutable reference."""

    activity = InferenceActivity.enqueue(_activity_draft())
    request = activity.request
    request["prompt"] = "mutated outside aggregate"

    assert activity.request == {"prompt": "hello"}
    with pytest.raises(FrozenInstanceError):
        activity.version = 99  # type: ignore[misc]


def test_failure_budget_is_distinct_from_generation_attempts() -> None:
    """@brief dependency 不计预算而 retry generation 仍增加 attempt / Dependency preserves budget while a retry generation still advances attempts."""

    initial = _initial_claim()
    assert initial.cause is InferenceGenerationCause.INITIAL
    assert initial.expected_version == 1
    assert initial.activity.attempt_count == 1
    assert initial.activity.retry_budget_used == 0

    dependency = initial.activity.record_failure(
        initial,
        failed_at=NOW + timedelta(seconds=1),
        failure=InferenceFailure("attachment receipt pending"),
        budget_charge=InferenceRetryBudgetCharge.PRESERVE,
    )
    waiting = dependency.schedule_retry(retry_at=NOW + timedelta(seconds=2)).activity
    assert waiting.status is InferenceActivityStatus.RETRY
    assert waiting.attempt_count == 1
    assert waiting.retry_budget_used == 0

    retry = waiting.claim(
        token=TOKEN_TWO,
        claimed_at=NOW + timedelta(seconds=2),
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    assert retry.cause is InferenceGenerationCause.RETRY
    assert retry.activity.attempt_count == 2
    assert retry.activity.retry_budget_used == 0

    ordinary = retry.activity.record_failure(
        retry,
        failed_at=NOW + timedelta(seconds=3),
        failure=InferenceFailure("provider unavailable"),
        budget_charge=InferenceRetryBudgetCharge.CONSUME,
    )
    failed = ordinary.fail_final().activity
    assert failed.status is InferenceActivityStatus.FAILED
    assert failed.attempt_count == 2
    assert failed.retry_budget_used == 1
    assert failed.version == retry.expected_version + 1


def test_steer_advances_revision_resets_budget_and_changes_generation_cause() -> None:
    """@brief steer 保留 attempt、重置预算、提升 revision 并产生 steer generation / Steer preserves attempts, resets budget, advances revision, and creates a steer generation."""

    retry = _processing_after_retry()
    assert retry.cause is InferenceGenerationCause.RETRY
    assert retry.activity.retry_budget_used == 1

    first_steer = retry.activity.steer(accepted_at=NOW + timedelta(seconds=3)).activity
    assert first_steer.status is InferenceActivityStatus.STEER_PENDING
    assert first_steer.attempt_count == retry.activity.attempt_count
    assert first_steer.retry_budget_used == 0
    assert first_steer.input_revision == TurnRevision(1)

    second_steer = first_steer.steer(accepted_at=NOW + timedelta(seconds=4)).activity
    assert second_steer.input_revision == TurnRevision(2)
    assert second_steer.version == first_steer.version + 1

    claim = second_steer.claim(
        token=TOKEN_THREE,
        claimed_at=NOW + timedelta(seconds=4),
        lease_expires_at=NOW + timedelta(seconds=40),
    )
    assert claim.cause is InferenceGenerationCause.STEER
    assert claim.generation_fence.input_revision == TurnRevision(2)
    assert claim.generation_fence.attempt == retry.activity.attempt_count + 1

    succeeded = claim.activity.succeed(
        claim,
        completed_at=NOW + timedelta(seconds=5),
    ).activity
    assert succeeded.status is InferenceActivityStatus.COMPLETED
    assert succeeded.completion_token == TOKEN_THREE
    assert succeeded.completed_at == NOW + timedelta(seconds=5)


def test_lease_recovery_preserves_counters_and_requires_actual_expiry() -> None:
    """@brief lease recovery 只重排队，不伪增 attempt 或预算 / Lease recovery only reschedules and does not invent attempts or budget charges."""

    claim = _processing_after_retry()
    lease = InferenceActivityLease.restore(
        claim.activity,
        token=claim.token,
        lease_expires_at=claim.lease_expires_at,
    )
    failure = InferenceFailure("worker lease expired")

    with pytest.raises(InvalidInferenceTransition, match="before it expires"):
        claim.activity.recover_expired_lease(
            lease,
            recovered_at=claim.lease_expires_at - timedelta.resolution,
            retry_at=claim.lease_expires_at + timedelta.resolution,
            failure=failure,
        )

    recovery = claim.activity.recover_expired_lease(
        lease,
        recovered_at=claim.lease_expires_at,
        retry_at=claim.lease_expires_at + timedelta.resolution,
        failure=failure,
    )
    recovered = recovery.activity
    assert recovered.status is InferenceActivityStatus.RETRY
    assert recovered.version == claim.expected_version + 1
    assert recovered.attempt_count == claim.activity.attempt_count
    assert recovered.retry_budget_used == claim.activity.retry_budget_used
    assert recovered.input_revision == claim.activity.input_revision
    assert recovered.last_error == "worker lease expired"


def test_stale_capability_and_regressing_time_are_rejected() -> None:
    """@brief settlement 拒绝旧 capability 与倒退时钟 / Settlement rejects stale capabilities and regressing clocks."""

    initial = _initial_claim()
    steered = initial.activity.steer(accepted_at=NOW + timedelta(seconds=1)).activity
    current = steered.claim(
        token=TOKEN_TWO,
        claimed_at=NOW + timedelta(seconds=1),
        lease_expires_at=NOW + timedelta(seconds=30),
    )

    with pytest.raises(InvalidInferenceTransition, match="does not own"):
        current.activity.succeed(
            initial,
            completed_at=NOW + timedelta(seconds=2),
        )
    with pytest.raises(ValueError, match="cannot precede"):
        initial.activity.succeed(
            initial,
            completed_at=NOW - timedelta.resolution,
        )


def test_every_unsettled_state_can_be_fenced_by_domain_cancellation() -> None:
    """@brief 四种未终结状态都由同一取消规则围栏 / One cancellation rule fences all four unsettled states."""

    initial = _initial_claim()
    retry_failure = initial.activity.record_failure(
        initial,
        failed_at=NOW + timedelta(seconds=1),
        failure=InferenceFailure("retry later"),
        budget_charge=InferenceRetryBudgetCharge.CONSUME,
    )
    retry = retry_failure.schedule_retry(retry_at=NOW + timedelta(seconds=2)).activity
    sources = (
        InferenceActivity.enqueue(_activity_draft()),
        initial.activity,
        initial.activity.steer(accepted_at=NOW + timedelta(seconds=1)).activity,
        retry,
    )

    for source in sources:
        cancelled = source.cancel(
            cancelled_at=max(source.updated_at, NOW + timedelta(seconds=3))
        ).activity
        assert cancelled.status is InferenceActivityStatus.CANCELLED
        assert cancelled.version == source.version + 1
        assert cancelled.attempt_count == source.attempt_count
        assert cancelled.retry_budget_used == source.retry_budget_used
        assert cancelled.input_revision == source.input_revision
        with pytest.raises(InvalidInferenceTransition, match="cannot be cancelled"):
            cancelled.cancel(cancelled_at=cancelled.updated_at)


@pytest.mark.parametrize("status", tuple(InferenceActivityStatus))
def test_restore_accepts_each_exact_persisted_state(
    status: InferenceActivityStatus,
) -> None:
    """@brief hydration 覆盖状态和的每个合法分支 / Hydration covers every valid branch of the state sum.

    @param status 被恢复的持久化状态 / Persisted status being restored.
    @return None / None.
    """

    assert _restore(status).status is status


@pytest.mark.parametrize(
    ("status", "overrides", "message"),
    (
        (InferenceActivityStatus.PENDING, {"version": 1}, "unclaimed initial"),
        (
            InferenceActivityStatus.PENDING,
            {"attempt_count": 1},
            "cannot trail",
        ),
        (
            InferenceActivityStatus.PROCESSING,
            {"retry_budget_used": 1},
            "unfinalized claim",
        ),
        (
            InferenceActivityStatus.PROCESSING,
            {"next_attempt_at": NOW},
            "inconsistent persistence",
        ),
        (
            InferenceActivityStatus.STEER_PENDING,
            {"input_revision": TurnRevision.initial()},
            "positive revision",
        ),
        (
            InferenceActivityStatus.STEER_PENDING,
            {"retry_budget_used": 1},
            "reset budget",
        ),
        (
            InferenceActivityStatus.RETRY,
            {"next_attempt_at": NOW},
            "future schedule",
        ),
        (
            InferenceActivityStatus.RETRY,
            {"last_error": None},
            "inconsistent persistence",
        ),
        (
            InferenceActivityStatus.COMPLETED,
            {"completion_token": None},
            "inconsistent persistence",
        ),
        (
            InferenceActivityStatus.COMPLETED,
            {"completed_at": NOW + timedelta(seconds=1)},
            "matching commit time",
        ),
        (
            InferenceActivityStatus.COMPLETED,
            {"retry_budget_used": 1},
            "unconsumed current claim",
        ),
        (
            InferenceActivityStatus.FAILED,
            {"last_error": None},
            "inconsistent persistence",
        ),
        (InferenceActivityStatus.CANCELLED, {"version": 0}, "positive version"),
        (
            InferenceActivityStatus.CANCELLED,
            {"completed_at": NOW, "completion_token": TOKEN_ONE},
            "inconsistent persistence",
        ),
        *(
            (
                status,
                {"input_revision": TurnRevision(1)},
                "steered generation requires a second claim",
            )
            for status in (
                InferenceActivityStatus.PROCESSING,
                InferenceActivityStatus.RETRY,
                InferenceActivityStatus.COMPLETED,
                InferenceActivityStatus.FAILED,
            )
        ),
    ),
)
def test_restore_rejects_invalid_persisted_state_combinations(
    status: InferenceActivityStatus,
    overrides: dict[str, Any],
    message: str,
) -> None:
    """@brief hydration fail-closed 拒绝不可能组合 / Hydration fails closed on impossible persistence combinations.

    @param status 被伪造的状态 / Forged status.
    @param overrides 破坏不变量的字段 / Fields violating invariants.
    @param message 预期诊断片段 / Expected diagnostic fragment.
    @return None / None.
    """

    with pytest.raises(ValueError, match=message):
        _restore(status, **overrides)
