"""@brief Inference activity 聚合与 fencing capability / Inference-activity aggregate and fencing capabilities."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Self

from fogmoe_bot.domain.observability.trace import TraceContext
from fogmoe_bot.domain.temporal import ensure_utc

from .identity import (
    ConversationId,
    InferenceActivityId,
    LeaseToken,
    TurnId,
    TurnRevision,
)
from .payloads import JsonObject
from ._inference_state import (
    AwaitingInitialInference,
    AwaitingSteeredInference,
    CancelledInference,
    CompletedInference,
    FailedInference,
    InferenceActivityState,
    InferenceActivityStatus,
    InferenceFailure,
    InferenceGenerationCause,
    InferenceRetryBudgetCharge,
    InvalidInferenceTransition,
    ProcessingInference,
    WaitingInferenceRetry,
)

if TYPE_CHECKING:
    from ._inference_decision import (
        InferenceCancelled,
        InferenceFailureAttempt,
        InferenceLeaseRecovered,
        InferenceSteered,
        InferenceSucceeded,
    )


@dataclass(frozen=True, slots=True, init=False)
class InferenceActivityDraft:
    """@brief acceptance 原子写入的不可变推理意图 / Immutable inference intent atomically written by acceptance.

    @param activity_id 活动 ID / Activity identifier.
    @param turn_id 所属 Turn / Owning Turn.
    @param conversation_id 所属 Conversation / Owning Conversation.
    @param request provider-neutral 结构请求 / Provider-neutral structured request.
    @param created_at 意图创建时间 / Intent creation time.
    @param trace_context 可持久传播 trace / Persistable trace context.
    """

    activity_id: InferenceActivityId
    turn_id: TurnId
    conversation_id: ConversationId
    _request: JsonObject
    created_at: datetime
    trace_context: TraceContext

    def __init__(
        self,
        *,
        activity_id: InferenceActivityId,
        turn_id: TurnId,
        conversation_id: ConversationId,
        request: JsonObject,
        created_at: datetime,
        trace_context: TraceContext | None = None,
    ) -> None:
        """@brief 创建并校验推理意图 / Create and validate an inference intent.

        @param activity_id 活动 ID / Activity identifier.
        @param turn_id 所属 Turn / Owning Turn.
        @param conversation_id 所属 Conversation / Owning Conversation.
        @param request provider-neutral 请求 / Provider-neutral request.
        @param created_at 创建时间 / Creation time.
        @param trace_context 可选 trace / Optional trace context.
        @return None / None.
        """

        context = trace_context or TraceContext.new_root()
        if not isinstance(activity_id, InferenceActivityId):
            raise TypeError("Inference draft requires an InferenceActivityId")
        if not isinstance(turn_id, TurnId):
            raise TypeError("Inference draft requires a TurnId")
        if not isinstance(conversation_id, ConversationId):
            raise TypeError("Inference draft requires a ConversationId")
        if not isinstance(context, TraceContext):
            raise TypeError("Inference draft requires a TraceContext")
        object.__setattr__(self, "activity_id", activity_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "conversation_id", conversation_id)
        object.__setattr__(self, "_request", deepcopy(request))
        object.__setattr__(self, "created_at", ensure_utc(created_at))
        object.__setattr__(self, "trace_context", context)

    @property
    def request(self) -> JsonObject:
        """@brief 返回隔离的结构请求 / Return an isolated structured request.

        @return 深拷贝 JSON 对象 / Deep-copied JSON object.
        """

        return deepcopy(self._request)


@dataclass(frozen=True, slots=True, init=False)
class InferenceActivity:
    """@brief 拥有 revision、retry budget、lease 与终态的推理聚合根 / Aggregate root owning revision, retry budget, leases, and terminal states.

    @note 新活动必须经 ``enqueue``，数据库 hydration 必须经 ``restore``；
        公开构造器被封闭。/ New activities go through ``enqueue`` and database
        hydration goes through ``restore``; the public constructor is closed.
    """

    activity_id: InferenceActivityId
    turn_id: TurnId
    conversation_id: ConversationId
    _request: JsonObject
    created_at: datetime
    trace_context: TraceContext
    state: InferenceActivityState
    version: int
    attempt_count: int
    retry_budget_used: int
    updated_at: datetime
    input_revision: TurnRevision

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝绕过领域工厂的公开构造 / Reject public construction that bypasses domain factories.

        @param args 未使用位置参数 / Unused positional arguments.
        @param kwargs 未使用关键字参数 / Unused keyword arguments.
        @return 永不返回 / Never returns.
        """

        del args, kwargs
        raise TypeError("Use InferenceActivity.enqueue() or restore()")

    @classmethod
    def _create(
        cls,
        *,
        draft: InferenceActivityDraft,
        state: InferenceActivityState,
        version: int,
        attempt_count: int,
        retry_budget_used: int,
        updated_at: datetime,
        input_revision: TurnRevision,
    ) -> Self:
        """@brief 经唯一不变量入口创建聚合 / Create an aggregate through the single invariant gate.

        @return 已验证聚合 / Validated aggregate.
        """

        if not isinstance(draft, InferenceActivityDraft):
            raise TypeError("Inference activity requires an InferenceActivityDraft")
        if not isinstance(
            state,
            (
                AwaitingInitialInference,
                ProcessingInference,
                AwaitingSteeredInference,
                WaitingInferenceRetry,
                CompletedInference,
                FailedInference,
                CancelledInference,
            ),
        ):
            raise TypeError("Inference activity requires a lifecycle state")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("Inference version must be a non-negative integer")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise ValueError("Inference attempt count must be a non-negative integer")
        if (
            isinstance(retry_budget_used, bool)
            or not isinstance(retry_budget_used, int)
            or not 0 <= retry_budget_used <= attempt_count
        ):
            raise ValueError(
                "Inference retry budget must be an integer between zero and attempts"
            )
        if version < attempt_count:
            raise ValueError("Inference version cannot trail its claim-attempt count")
        if not isinstance(input_revision, TurnRevision):
            raise TypeError("Inference input_revision must be TurnRevision")
        timestamp = ensure_utc(updated_at)
        if timestamp < draft.created_at:
            raise ValueError("Inference updated_at cannot precede created_at")

        if isinstance(state, AwaitingInitialInference):
            if (
                version != 0
                or attempt_count != 0
                or retry_budget_used != 0
                or input_revision != TurnRevision.initial()
                or timestamp != draft.created_at
                or state.claimable_at != timestamp
            ):
                raise ValueError(
                    "Pending inference must be the unclaimed initial revision"
                )
        elif isinstance(state, AwaitingSteeredInference):
            if (
                attempt_count < 1
                or int(input_revision) < 1
                or retry_budget_used != 0
                or state.claimable_at != timestamp
            ):
                raise ValueError(
                    "Steer-pending inference requires a prior claim, positive revision, "
                    "reset budget, and immediate schedule"
                )
        elif isinstance(state, ProcessingInference):
            if attempt_count < 1 or retry_budget_used >= attempt_count:
                raise ValueError(
                    "Processing inference requires an unfinalized claim beyond its budget"
                )
        elif isinstance(state, WaitingInferenceRetry):
            if attempt_count < 1 or state.claimable_at <= timestamp:
                raise ValueError(
                    "Retrying inference requires a prior claim and future schedule"
                )
        elif isinstance(state, CompletedInference):
            if (
                attempt_count < 1
                or retry_budget_used >= attempt_count
                or state.completed_at != timestamp
            ):
                raise ValueError(
                    "Completed inference requires an unconsumed current claim and matching commit time"
                )
        elif isinstance(state, FailedInference):
            if attempt_count < 1:
                raise ValueError("Failed inference requires a prior claim")
        elif version < 1:
            raise ValueError("Cancelled inference requires a positive version")
        if int(input_revision) > 0 and attempt_count < 1:
            raise ValueError("A positive inference revision requires a prior claim")
        if (
            int(input_revision) > 0
            and isinstance(
                state,
                ProcessingInference
                | WaitingInferenceRetry
                | CompletedInference
                | FailedInference,
            )
            and attempt_count < 2
        ):
            raise ValueError(
                "A settled or processing steered generation requires a second claim"
            )

        activity = object.__new__(cls)
        object.__setattr__(activity, "activity_id", draft.activity_id)
        object.__setattr__(activity, "turn_id", draft.turn_id)
        object.__setattr__(activity, "conversation_id", draft.conversation_id)
        object.__setattr__(activity, "_request", deepcopy(draft.request))
        object.__setattr__(activity, "created_at", draft.created_at)
        object.__setattr__(activity, "trace_context", draft.trace_context)
        object.__setattr__(activity, "state", state)
        object.__setattr__(activity, "version", version)
        object.__setattr__(activity, "attempt_count", attempt_count)
        object.__setattr__(activity, "retry_budget_used", retry_budget_used)
        object.__setattr__(activity, "updated_at", timestamp)
        object.__setattr__(activity, "input_revision", input_revision)
        return activity

    @classmethod
    def enqueue(cls, draft: InferenceActivityDraft) -> Self:
        """@brief 从 durable intent 创建待领取聚合 / Create a pending aggregate from a durable intent.

        @param draft 不可变推理意图 / Immutable inference intent.
        @return 初始待领取聚合 / Initial pending aggregate.
        """

        return cls._create(
            draft=draft,
            state=AwaitingInitialInference(draft.created_at),
            version=0,
            attempt_count=0,
            retry_budget_used=0,
            updated_at=draft.created_at,
            input_revision=TurnRevision.initial(),
        )

    @classmethod
    def restore(
        cls,
        *,
        draft: InferenceActivityDraft,
        status: InferenceActivityStatus,
        version: int,
        attempt_count: int,
        retry_budget_used: int,
        next_attempt_at: datetime | None,
        updated_at: datetime,
        completed_at: datetime | None,
        completion_token: LeaseToken | None,
        last_error: str | None,
        input_revision: TurnRevision,
    ) -> Self:
        """@brief 从持久化标量严格恢复聚合 / Strictly restore an aggregate from persistence scalars.

        @return 严格验证的聚合 / Strictly validated aggregate.
        @raise ValueError 字段不符合精确状态矩阵时抛出 / Raised when fields violate the exact state matrix.
        """

        if not isinstance(status, InferenceActivityStatus):
            raise TypeError("Inference restore requires an InferenceActivityStatus")
        next_time = ensure_utc(next_attempt_at) if next_attempt_at else None
        completion_time = ensure_utc(completed_at) if completed_at else None
        failure = InferenceFailure(last_error) if last_error is not None else None
        empty_completion = completion_time is None and completion_token is None

        if status is InferenceActivityStatus.PENDING:
            if next_time is None or not empty_completion or failure is not None:
                raise ValueError(
                    "Pending inference has inconsistent persistence fields"
                )
            state: InferenceActivityState = AwaitingInitialInference(next_time)
        elif status is InferenceActivityStatus.PROCESSING:
            if next_time is not None or not empty_completion or failure is not None:
                raise ValueError(
                    "Processing inference has inconsistent persistence fields"
                )
            state = ProcessingInference()
        elif status is InferenceActivityStatus.STEER_PENDING:
            if next_time is None or not empty_completion or failure is not None:
                raise ValueError(
                    "Steer-pending inference has inconsistent persistence fields"
                )
            state = AwaitingSteeredInference(next_time)
        elif status is InferenceActivityStatus.RETRY:
            if next_time is None or not empty_completion or failure is None:
                raise ValueError(
                    "Retrying inference has inconsistent persistence fields"
                )
            state = WaitingInferenceRetry(next_time, failure)
        elif status is InferenceActivityStatus.COMPLETED:
            if (
                next_time is not None
                or completion_time is None
                or completion_token is None
                or failure is not None
            ):
                raise ValueError(
                    "Completed inference has inconsistent persistence fields"
                )
            state = CompletedInference(completion_time, completion_token)
        elif status is InferenceActivityStatus.FAILED:
            if next_time is not None or not empty_completion or failure is None:
                raise ValueError("Failed inference has inconsistent persistence fields")
            state = FailedInference(failure)
        else:
            if next_time is not None or not empty_completion:
                raise ValueError(
                    "Cancelled inference has inconsistent persistence fields"
                )
            state = CancelledInference(failure)
        return cls._create(
            draft=draft,
            state=state,
            version=version,
            attempt_count=attempt_count,
            retry_budget_used=retry_budget_used,
            updated_at=updated_at,
            input_revision=input_revision,
        )

    @property
    def request(self) -> JsonObject:
        """@brief 返回隔离的 provider-neutral 请求 / Return the isolated provider-neutral request.

        @return 深拷贝结构请求 / Deep-copied structured request.
        """

        return deepcopy(self._request)

    @property
    def status(self) -> InferenceActivityStatus:
        """@brief 投影稳定持久化状态 / Project the stable persisted status.

        @return 持久化状态 / Persisted status.
        """

        if isinstance(self.state, AwaitingInitialInference):
            return InferenceActivityStatus.PENDING
        if isinstance(self.state, ProcessingInference):
            return InferenceActivityStatus.PROCESSING
        if isinstance(self.state, AwaitingSteeredInference):
            return InferenceActivityStatus.STEER_PENDING
        if isinstance(self.state, WaitingInferenceRetry):
            return InferenceActivityStatus.RETRY
        if isinstance(self.state, CompletedInference):
            return InferenceActivityStatus.COMPLETED
        if isinstance(self.state, FailedInference):
            return InferenceActivityStatus.FAILED
        return InferenceActivityStatus.CANCELLED

    @property
    def next_attempt_at(self) -> datetime | None:
        """@brief 投影可选下次领取时刻 / Project the optional next claim time.

        @return 待领取时刻或 None / Claim time or None.
        """

        if isinstance(
            self.state,
            AwaitingInitialInference | AwaitingSteeredInference | WaitingInferenceRetry,
        ):
            return self.state.claimable_at
        return None

    @property
    def completed_at(self) -> datetime | None:
        """@brief 投影可选完成时刻 / Project the optional completion time.

        @return 完成时刻或 None / Completion time or None.
        """

        return (
            self.state.completed_at
            if isinstance(self.state, CompletedInference)
            else None
        )

    @property
    def completion_token(self) -> LeaseToken | None:
        """@brief 投影可选完成 fencing 回执 / Project the optional completion fencing receipt.

        @return token 或 None / Token or None.
        """

        return (
            self.state.completion_token
            if isinstance(self.state, CompletedInference)
            else None
        )

    @property
    def last_error(self) -> str | None:
        """@brief 投影可选最近失败 / Project the optional latest failure.

        @return 失败摘要或 None / Failure summary or None.
        """

        if isinstance(self.state, WaitingInferenceRetry | FailedInference):
            return self.state.failure.summary
        if isinstance(self.state, CancelledInference) and self.state.last_failure:
            return self.state.last_failure.summary
        return None

    def claim(
        self,
        *,
        token: LeaseToken,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> InferenceActivityClaim:
        """@brief 领取到期 generation 并签发 fencing capability / Claim a due generation and issue a fencing capability.

        @return 带 cause/version/token/lease 的 capability / Capability carrying cause, version, token, and lease.
        """

        if not isinstance(
            self.state,
            AwaitingInitialInference | AwaitingSteeredInference | WaitingInferenceRetry,
        ):
            raise InvalidInferenceTransition(
                f"Inference state {self.status.value} cannot be claimed"
            )
        timestamp = ensure_utc(claimed_at)
        lease_end = ensure_utc(lease_expires_at)
        if timestamp < self.updated_at or timestamp < self.state.claimable_at:
            raise ValueError("Inference activity is not claimable at claimed_at")
        if lease_end <= timestamp:
            raise ValueError("Inference lease must expire after claim time")
        cause = (
            InferenceGenerationCause.INITIAL
            if isinstance(self.state, AwaitingInitialInference)
            else InferenceGenerationCause.STEER
            if isinstance(self.state, AwaitingSteeredInference)
            else InferenceGenerationCause.RETRY
        )
        processing = self._evolve(
            state=ProcessingInference(),
            version=self.version + 1,
            attempt_count=self.attempt_count + 1,
            updated_at=timestamp,
        )
        return InferenceActivityClaim.from_processing(
            processing,
            token=token,
            lease_expires_at=lease_end,
            cause=cause,
        )

    def steer(self, *, accepted_at: datetime) -> InferenceSteered:
        """@brief 用新用户输入取代当前 generation / Supersede the current generation with new user input.

        @param accepted_at steer 接受时刻 / Steer-acceptance time.
        @return 类型化 steer 决定 / Typed steer decision.
        """

        from ._inference_decision import InferenceSteered

        if not isinstance(self.state, ProcessingInference | AwaitingSteeredInference):
            raise InvalidInferenceTransition(
                f"Inference state {self.status.value} cannot accept a steer"
            )
        timestamp = self._transition_time(accepted_at)
        target = self._evolve(
            state=AwaitingSteeredInference(timestamp),
            version=self.version + 1,
            retry_budget_used=0,
            updated_at=timestamp,
            input_revision=self.input_revision.next(),
        )
        return InferenceSteered._create(self, target)

    def succeed(
        self,
        claim: InferenceActivityClaim,
        *,
        completed_at: datetime,
    ) -> InferenceSucceeded:
        """@brief 成功终结当前 generation claim / Successfully settle the current generation claim.

        @return 类型化成功 settlement / Typed successful settlement.
        """

        from ._inference_decision import InferenceSucceeded

        self._require_claim(claim)
        timestamp = self._transition_time(completed_at)
        target = self._evolve(
            state=CompletedInference(timestamp, claim.token),
            version=self.version + 1,
            updated_at=timestamp,
        )
        return InferenceSucceeded._create(claim, target)

    def record_failure(
        self,
        claim: InferenceActivityClaim,
        *,
        failed_at: datetime,
        failure: InferenceFailure,
        budget_charge: InferenceRetryBudgetCharge,
    ) -> InferenceFailureAttempt:
        """@brief 记录尚未选择 retry/final 的失败 outcome / Record a failure outcome before choosing retry or final failure.

        @return 带预算目标的失败 outcome / Failure outcome carrying the budget target.
        """

        from ._inference_decision import InferenceFailureAttempt

        self._require_claim(claim)
        if not isinstance(failure, InferenceFailure):
            raise TypeError("Inference failure outcome requires InferenceFailure")
        if not isinstance(budget_charge, InferenceRetryBudgetCharge):
            raise TypeError("Inference failure requires InferenceRetryBudgetCharge")
        budget_target = self.retry_budget_used + (
            budget_charge is InferenceRetryBudgetCharge.CONSUME
        )
        return InferenceFailureAttempt._create(
            claim,
            failed_at=self._transition_time(failed_at),
            failure=failure,
            retry_budget_used=int(budget_target),
        )

    def recover_expired_lease(
        self,
        lease: InferenceActivityLease,
        *,
        recovered_at: datetime,
        retry_at: datetime,
        failure: InferenceFailure,
    ) -> InferenceLeaseRecovered:
        """@brief 恢复过期 lease 且不改变 attempt/budget / Recover an expired lease without changing attempts or budget.

        @return 类型化 lease-recovery 决定 / Typed lease-recovery decision.
        """

        from ._inference_decision import InferenceLeaseRecovered

        self._require_lease(lease)
        recovery_time = self._transition_time(recovered_at)
        retry_time = ensure_utc(retry_at)
        if recovery_time < lease.lease_expires_at:
            raise InvalidInferenceTransition(
                "Inference lease cannot be recovered before it expires"
            )
        if retry_time <= recovery_time:
            raise ValueError("Inference recovery retry_at must follow recovered_at")
        target = self._evolve(
            state=WaitingInferenceRetry(retry_time, failure),
            version=self.version + 1,
            updated_at=recovery_time,
        )
        return InferenceLeaseRecovered._create(lease, target)

    def cancel(self, *, cancelled_at: datetime) -> InferenceCancelled:
        """@brief 围栏并取消尚未终结的推理活动 / Fence and cancel an unsettled inference activity.

        @return 类型化取消决定 / Typed cancellation decision.
        """

        from ._inference_decision import InferenceCancelled

        if isinstance(
            self.state, CompletedInference | FailedInference | CancelledInference
        ):
            raise InvalidInferenceTransition(
                f"Inference state {self.status.value} cannot be cancelled"
            )
        last_failure = (
            self.state.failure
            if isinstance(self.state, WaitingInferenceRetry)
            else None
        )
        target = self._evolve(
            state=CancelledInference(last_failure),
            version=self.version + 1,
            updated_at=self._transition_time(cancelled_at),
        )
        return InferenceCancelled._create(self, target)

    def _evolve(
        self,
        *,
        state: InferenceActivityState,
        version: int,
        attempt_count: int | None = None,
        retry_budget_used: int | None = None,
        updated_at: datetime,
        input_revision: TurnRevision | None = None,
    ) -> Self:
        """@brief 保留不可变意图并构造下一版本 / Preserve immutable intent and construct the next version.

        @return 已验证的新聚合 / Validated new aggregate.
        """

        return type(self)._create(
            draft=InferenceActivityDraft(
                activity_id=self.activity_id,
                turn_id=self.turn_id,
                conversation_id=self.conversation_id,
                request=self.request,
                created_at=self.created_at,
                trace_context=self.trace_context,
            ),
            state=state,
            version=version,
            attempt_count=(
                self.attempt_count if attempt_count is None else attempt_count
            ),
            retry_budget_used=(
                self.retry_budget_used
                if retry_budget_used is None
                else retry_budget_used
            ),
            updated_at=updated_at,
            input_revision=(
                self.input_revision if input_revision is None else input_revision
            ),
        )

    def _require_claim(self, claim: InferenceActivityClaim) -> None:
        """@brief 验证 claim 拥有当前 processing 版本 / Verify that a claim owns the current processing version.

        @return None / None.
        """

        if not isinstance(self.state, ProcessingInference):
            raise InvalidInferenceTransition(
                f"Inference state {self.status.value} cannot be settled"
            )
        if not isinstance(claim, InferenceActivityClaim):
            raise TypeError("Inference settlement requires InferenceActivityClaim")
        if claim.activity != self or claim.expected_version != self.version:
            raise InvalidInferenceTransition(
                "Inference claim does not own this activity version"
            )

    def _require_lease(self, lease: InferenceActivityLease) -> None:
        """@brief 验证 lease 拥有当前 processing 版本 / Verify that a lease owns the current processing version.

        @return None / None.
        """

        if not isinstance(self.state, ProcessingInference):
            raise InvalidInferenceTransition(
                f"Inference state {self.status.value} has no recoverable lease"
            )
        if lease.activity != self or lease.expected_version != self.version:
            raise InvalidInferenceTransition(
                "Inference lease does not own this activity version"
            )

    def _transition_time(self, occurred_at: datetime) -> datetime:
        """@brief 校验转换时间不倒退 / Validate that a transition time does not regress.

        @return 规范 UTC 时刻 / Normalized UTC time.
        """

        timestamp = ensure_utc(occurred_at)
        if timestamp < self.updated_at:
            raise ValueError("Inference transition time cannot precede current version")
        return timestamp


@dataclass(frozen=True, slots=True, init=False)
class InferenceActivityLease:
    """@brief version/token/lease ownership capability，不伪造未持久化 cause / Version/token/lease ownership capability that does not invent an unpersisted cause."""

    activity: InferenceActivity
    expected_version: int
    token: LeaseToken
    lease_expires_at: datetime

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝伪造 lease capability / Reject forged lease capabilities.

        @return 永不返回 / Never returns.
        """

        del args, kwargs
        raise TypeError("Use InferenceActivityLease.restore()")

    @classmethod
    def restore(
        cls,
        activity: InferenceActivity,
        *,
        token: LeaseToken,
        lease_expires_at: datetime,
    ) -> Self:
        """@brief 从 processing 聚合与持久化租约严格恢复 capability / Strictly restore a capability from a processing aggregate and persisted lease.

        @return 已验证 capability / Validated capability.
        """

        if activity.status is not InferenceActivityStatus.PROCESSING:
            raise ValueError("Inference lease requires a processing activity")
        if not isinstance(token, LeaseToken):
            raise TypeError("Inference lease requires a LeaseToken")
        lease_end = ensure_utc(lease_expires_at)
        if lease_end <= activity.updated_at:
            raise ValueError("Inference lease must expire after claim time")
        capability = object.__new__(cls)
        object.__setattr__(capability, "activity", activity)
        object.__setattr__(capability, "expected_version", activity.version)
        object.__setattr__(capability, "token", token)
        object.__setattr__(capability, "lease_expires_at", lease_end)
        return capability


@dataclass(frozen=True, slots=True, init=False)
class InferenceActivityClaim:
    """@brief 携带 cause 与 version/token/lease ownership 的 generation capability / Generation capability carrying cause and version/token/lease ownership."""

    activity: InferenceActivity
    expected_version: int
    token: LeaseToken
    lease_expires_at: datetime
    cause: InferenceGenerationCause

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝伪造 generation claim / Reject forged generation claims.

        @return 永不返回 / Never returns.
        """

        del args, kwargs
        raise TypeError("Use InferenceActivity.claim() or from_processing()")

    @classmethod
    def from_processing(
        cls,
        activity: InferenceActivity,
        *,
        token: LeaseToken,
        lease_expires_at: datetime,
        cause: InferenceGenerationCause,
    ) -> Self:
        """@brief 从已知 cause 的 processing 聚合严格签发 claim / Strictly issue a claim for a processing aggregate with a known cause.

        @return 已验证 claim / Validated claim.
        """

        lease = InferenceActivityLease.restore(
            activity,
            token=token,
            lease_expires_at=lease_expires_at,
        )
        if not isinstance(cause, InferenceGenerationCause):
            raise TypeError("Inference claim requires InferenceGenerationCause")
        if cause is InferenceGenerationCause.INITIAL and (
            int(activity.input_revision) != 0 or activity.attempt_count != 1
        ):
            raise ValueError(
                "Initial generation requires revision zero and first claim"
            )
        if cause is InferenceGenerationCause.STEER and (
            int(activity.input_revision) < 1 or activity.attempt_count < 2
        ):
            raise ValueError(
                "Steer generation requires a positive revision and prior claim"
            )
        if cause is InferenceGenerationCause.RETRY and activity.attempt_count < 2:
            raise ValueError("Retry generation requires a prior claim")
        claim = object.__new__(cls)
        object.__setattr__(claim, "activity", activity)
        object.__setattr__(claim, "expected_version", lease.expected_version)
        object.__setattr__(claim, "token", token)
        object.__setattr__(claim, "lease_expires_at", lease.lease_expires_at)
        object.__setattr__(claim, "cause", cause)
        return claim

    @property
    def generation_fence(self) -> InferenceGenerationFence:
        """@brief 投影强类型 generation fence / Project the strongly typed generation fence.

        @return 不可分 generation identity / Indivisible generation identity.
        """

        return InferenceGenerationFence(
            activity_id=self.activity.activity_id,
            turn_id=self.activity.turn_id,
            claim_token=self.token,
            attempt=self.activity.attempt_count,
            input_revision=self.activity.input_revision,
            cause=self.cause,
        )


@dataclass(frozen=True, slots=True)
class InferenceGenerationFence:
    """@brief processing generation 的跨进程 fencing identity / Cross-process fencing identity of a processing generation."""

    activity_id: InferenceActivityId
    turn_id: TurnId
    claim_token: LeaseToken
    attempt: int
    input_revision: TurnRevision
    cause: InferenceGenerationCause

    def __post_init__(self) -> None:
        """@brief 校验 generation identity / Validate the generation identity.

        @return None / None.
        """

        if isinstance(self.attempt, bool) or self.attempt < 1:
            raise ValueError("Inference generation attempt must be positive")
        if not isinstance(self.input_revision, TurnRevision):
            raise TypeError("Inference generation input_revision must be TurnRevision")
        if not isinstance(self.cause, InferenceGenerationCause):
            raise TypeError(
                "Inference generation cause must be InferenceGenerationCause"
            )
