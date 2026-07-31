"""@brief Inference activity 类型化转换决定 / Typed inference-activity transition decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Self

from fogmoe_bot.domain.temporal import ensure_utc

from ._inference_activity import (
    InferenceActivity,
    InferenceActivityClaim,
    InferenceActivityLease,
)
from ._inference_state import (
    FailedInference,
    InferenceActivityStatus,
    InferenceFailure,
    WaitingInferenceRetry,
)


class _ClosedDecision:
    """@brief 禁止调用方伪造转换决定 / Prevent callers from forging transition decisions."""

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝公开构造 / Reject public construction.

        @param args 未使用位置参数 / Unused positional arguments.
        @param kwargs 未使用关键字参数 / Unused keyword arguments.
        @return 永不返回 / Never returns.
        """

        del args, kwargs
        raise TypeError("Inference decisions are created by aggregate transitions")


@dataclass(frozen=True, slots=True, init=False)
class InferenceFailureAttempt(_ClosedDecision):
    """@brief 已分类计费、尚未选择 retry/final 的失败 outcome / Budget-classified failure outcome awaiting a retry/final choice."""

    claim: InferenceActivityClaim
    failed_at: datetime
    failure: InferenceFailure
    retry_budget_used: int

    @classmethod
    def _create(
        cls,
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        failure: InferenceFailure,
        retry_budget_used: int,
    ) -> Self:
        """@brief 由聚合创建失败 outcome / Create a failure outcome from the aggregate.

        @return 封闭构造的 outcome / Closed-construction outcome.
        """

        if (
            retry_budget_used
            not in {
                claim.activity.retry_budget_used,
                claim.activity.retry_budget_used + 1,
            }
            or retry_budget_used > claim.activity.attempt_count
        ):
            raise ValueError("Inference failure may consume at most one budget unit")
        outcome = object.__new__(cls)
        object.__setattr__(outcome, "claim", claim)
        object.__setattr__(outcome, "failed_at", ensure_utc(failed_at))
        object.__setattr__(outcome, "failure", failure)
        object.__setattr__(outcome, "retry_budget_used", retry_budget_used)
        return outcome

    def schedule_retry(self, *, retry_at: datetime) -> InferenceRetryScheduled:
        """@brief 将 outcome 终结为可恢复重试 / Settle the outcome as a recoverable retry.

        @param retry_at 下次可领取时刻 / Next claim time.
        @return 类型化重试 settlement / Typed retry settlement.
        """

        retry_time = ensure_utc(retry_at)
        if retry_time <= self.failed_at:
            raise ValueError("Inference retry_at must follow failed_at")
        source = self.claim.activity
        target = source._evolve(
            state=WaitingInferenceRetry(retry_time, self.failure),
            version=source.version + 1,
            retry_budget_used=self.retry_budget_used,
            updated_at=self.failed_at,
        )
        return InferenceRetryScheduled._create(self.claim, target)

    def fail_final(self) -> InferenceFailedFinal:
        """@brief 将 outcome 终结为最终失败 / Settle the outcome as final failure.

        @return 类型化最终失败 settlement / Typed final-failure settlement.
        """

        source = self.claim.activity
        target = source._evolve(
            state=FailedInference(self.failure),
            version=source.version + 1,
            retry_budget_used=self.retry_budget_used,
            updated_at=self.failed_at,
        )
        return InferenceFailedFinal._create(self.claim, target)


@dataclass(frozen=True, slots=True, init=False)
class InferenceSucceeded(_ClosedDecision):
    """@brief 成功 generation settlement / Successful generation settlement."""

    claim: InferenceActivityClaim
    activity: InferenceActivity

    @classmethod
    def _create(
        cls,
        claim: InferenceActivityClaim,
        activity: InferenceActivity,
    ) -> Self:
        """@brief 由聚合创建成功 settlement / Create a successful settlement from the aggregate.

        @return 已验证 settlement / Validated settlement.
        """

        _validate_claim_settlement(claim, activity, InferenceActivityStatus.COMPLETED)
        if (
            activity.retry_budget_used != claim.activity.retry_budget_used
            or activity.completion_token != claim.token
        ):
            raise ValueError("Inference success must preserve budget and claim token")
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "activity", activity)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class InferenceRetryScheduled(_ClosedDecision):
    """@brief 可恢复重试 settlement / Recoverable retry settlement."""

    claim: InferenceActivityClaim
    activity: InferenceActivity

    @classmethod
    def _create(
        cls,
        claim: InferenceActivityClaim,
        activity: InferenceActivity,
    ) -> Self:
        """@brief 由聚合创建重试 settlement / Create a retry settlement from the aggregate.

        @return 已验证 settlement / Validated settlement.
        """

        _validate_claim_settlement(claim, activity, InferenceActivityStatus.RETRY)
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "activity", activity)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class InferenceFailedFinal(_ClosedDecision):
    """@brief 最终失败 settlement / Final-failure settlement."""

    claim: InferenceActivityClaim
    activity: InferenceActivity

    @classmethod
    def _create(
        cls,
        claim: InferenceActivityClaim,
        activity: InferenceActivity,
    ) -> Self:
        """@brief 由聚合创建最终失败 settlement / Create a final-failure settlement from the aggregate.

        @return 已验证 settlement / Validated settlement.
        """

        _validate_claim_settlement(claim, activity, InferenceActivityStatus.FAILED)
        decision = object.__new__(cls)
        object.__setattr__(decision, "claim", claim)
        object.__setattr__(decision, "activity", activity)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class InferenceLeaseRecovered(_ClosedDecision):
    """@brief 过期 lease 恢复决定 / Expired-lease recovery decision."""

    lease: InferenceActivityLease
    activity: InferenceActivity

    @classmethod
    def _create(
        cls,
        lease: InferenceActivityLease,
        activity: InferenceActivity,
    ) -> Self:
        """@brief 由聚合创建 lease-recovery 决定 / Create a lease-recovery decision from the aggregate.

        @return 已验证恢复决定 / Validated recovery decision.
        """

        if activity.status is not InferenceActivityStatus.RETRY:
            raise ValueError("Inference lease recovery requires a retry target")
        _validate_identity_and_version(lease.activity, activity)
        if (
            activity.attempt_count != lease.activity.attempt_count
            or activity.retry_budget_used != lease.activity.retry_budget_used
            or activity.input_revision != lease.activity.input_revision
        ):
            raise ValueError(
                "Inference lease recovery must preserve counters and revision"
            )
        decision = object.__new__(cls)
        object.__setattr__(decision, "lease", lease)
        object.__setattr__(decision, "activity", activity)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class InferenceSteered(_ClosedDecision):
    """@brief 同 Turn input revision 提升决定 / Same-Turn input-revision advancement decision."""

    previous: InferenceActivity
    activity: InferenceActivity

    @classmethod
    def _create(
        cls,
        previous: InferenceActivity,
        activity: InferenceActivity,
    ) -> Self:
        """@brief 由聚合创建 steer 决定 / Create a steer decision from the aggregate.

        @return 已验证 steer 决定 / Validated steer decision.
        """

        if (
            previous.status
            not in {
                InferenceActivityStatus.PROCESSING,
                InferenceActivityStatus.STEER_PENDING,
            }
            or activity.status is not InferenceActivityStatus.STEER_PENDING
        ):
            raise ValueError(
                "Inference steer requires an active source and steer target"
            )
        _validate_identity_and_version(previous, activity)
        if (
            activity.attempt_count != previous.attempt_count
            or activity.input_revision != previous.input_revision.next()
            or activity.retry_budget_used != 0
        ):
            raise ValueError(
                "Inference steer must preserve attempts, advance revision, and reset budget"
            )
        decision = object.__new__(cls)
        object.__setattr__(decision, "previous", previous)
        object.__setattr__(decision, "activity", activity)
        return decision


@dataclass(frozen=True, slots=True, init=False)
class InferenceCancelled(_ClosedDecision):
    """@brief 未终结推理活动的取消决定 / Cancellation decision for an unsettled inference activity."""

    previous: InferenceActivity
    activity: InferenceActivity

    @classmethod
    def _create(
        cls,
        previous: InferenceActivity,
        activity: InferenceActivity,
    ) -> Self:
        """@brief 由聚合创建取消决定 / Create a cancellation decision from the aggregate.

        @return 已验证取消决定 / Validated cancellation decision.
        """

        if (
            previous.status
            in {
                InferenceActivityStatus.COMPLETED,
                InferenceActivityStatus.FAILED,
                InferenceActivityStatus.CANCELLED,
            }
            or activity.status is not InferenceActivityStatus.CANCELLED
        ):
            raise ValueError("Inference cancellation requires an unsettled source")
        _validate_identity_and_version(previous, activity)
        if (
            activity.attempt_count != previous.attempt_count
            or activity.retry_budget_used != previous.retry_budget_used
            or activity.input_revision != previous.input_revision
        ):
            raise ValueError(
                "Inference cancellation must preserve counters and revision"
            )
        decision = object.__new__(cls)
        object.__setattr__(decision, "previous", previous)
        object.__setattr__(decision, "activity", activity)
        return decision


type InferenceClaimSettlement = (
    InferenceSucceeded | InferenceRetryScheduled | InferenceFailedFinal
)
"""@brief generation claim 的穷尽持久化 settlement / Exhaustive persisted settlements of a generation claim."""


def _validate_claim_settlement(
    claim: InferenceActivityClaim,
    activity: InferenceActivity,
    expected_status: InferenceActivityStatus,
) -> None:
    """@brief 验证 settlement 保持 identity/counters 并递增一个版本 / Validate identity and counters while advancing one version.

    @return None / None.
    """

    if activity.status is not expected_status:
        raise ValueError(
            f"Inference settlement requires {expected_status.value}, "
            f"found {activity.status.value}"
        )
    _validate_identity_and_version(claim.activity, activity)
    if (
        activity.attempt_count != claim.activity.attempt_count
        or activity.input_revision != claim.activity.input_revision
        or activity.retry_budget_used
        not in {
            claim.activity.retry_budget_used,
            claim.activity.retry_budget_used + 1,
        }
    ):
        raise ValueError("Inference settlement changed forbidden counters or revision")


def _validate_identity_and_version(
    previous: InferenceActivity,
    activity: InferenceActivity,
) -> None:
    """@brief 验证转换保持 intent 且版本严格加一 / Validate preserved intent and an exact one-version advance.

    @return None / None.
    """

    if (
        activity.activity_id != previous.activity_id
        or activity.turn_id != previous.turn_id
        or activity.conversation_id != previous.conversation_id
        or activity.request != previous.request
        or activity.created_at != previous.created_at
        or activity.trace_context != previous.trace_context
    ):
        raise ValueError("Inference transition cannot replace its durable intent")
    if activity.version != previous.version + 1:
        raise ValueError("Inference transition must advance exactly one version")


@dataclass(frozen=True, slots=True)
class InferenceActivityEnqueueResult:
    """@brief 幂等活动意图写入结果 / Idempotent inference-activity enqueue result.

    @param activity 数据库中的规范聚合 / Canonical stored aggregate.
    @param inserted 本次是否插入 / Whether this call inserted the row.
    """

    activity: InferenceActivity
    inserted: bool
