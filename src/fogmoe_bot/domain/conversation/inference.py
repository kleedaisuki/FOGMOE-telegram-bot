"""Durable inference activity 模型 / Durable inference-activity models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Self

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


class InferenceActivityStatus(StrEnum):
    """@brief 可恢复推理活动状态 / Recoverable inference-activity status."""

    PENDING = "pending"
    PROCESSING = "processing"
    STEER_PENDING = "steer_pending"
    RETRY = "retry"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InferenceGenerationCause(StrEnum):
    """@brief processing generation 的启动原因 / Cause that started a processing generation."""

    INITIAL = "initial"
    STEER = "steer"
    RETRY = "retry"


INFERENCE_ACTIVITY_CLAIMABLE_STATES = frozenset(
    {
        InferenceActivityStatus.PENDING,
        InferenceActivityStatus.STEER_PENDING,
        InferenceActivityStatus.RETRY,
    }
)
"""@brief 可由 worker 领取的推理活动状态 / Inference-activity states claimable by workers."""


@dataclass(frozen=True, slots=True)
class InferenceActivityDraft:
    """@brief acceptance 原子写入的推理活动意图 / Inference-activity intent atomically written by acceptance.

    @param activity_id 活动 ID / Activity identifier.
    @param turn_id 所属回合 / Owning turn.
    @param conversation_id 所属长期会话 / Owning long-lived conversation.
    @param request provider-neutral 结构请求 / Provider-neutral structured request.
    @param created_at 意图创建时间 / Intent creation time.
    """

    activity_id: InferenceActivityId
    turn_id: TurnId
    conversation_id: ConversationId
    request: JsonObject
    created_at: datetime
    trace_context: TraceContext = field(default_factory=TraceContext.new_root)

    def __post_init__(self) -> None:
        """@brief 校验活动草稿并隔离可变 JSON / Validate the activity draft and isolate mutable JSON.

        @return None / None.
        """

        object.__setattr__(self, "request", dict(self.request))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        if not isinstance(self.trace_context, TraceContext):
            raise TypeError("Inference activity requires a TraceContext")


@dataclass(frozen=True, slots=True)
class InferenceActivity:
    """@brief 可版本化、可租约恢复的推理活动快照 / Versioned, lease-recoverable inference-activity snapshot.

    @param draft 不可变活动意图 / Immutable activity intent.
    @param status 当前状态 / Current status.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param attempt_count 已领取次数 / Number of claims made.
    @param retry_budget_used 当前 input revision 内已持久化且消耗普通重试预算的失败
        outcome 数；steer 开启新 revision 时归零 / Number of durably persisted failure
        outcomes in the current input revision that consume the ordinary retry budget; reset
        when steering starts a new revision.
    @param next_attempt_at 下一次可领取时间 / Next claimable time.
    @param updated_at 最近状态更新时间 / Most recent state-update time.
    @param completed_at 成功提交时间 / Successful commit time.
    @param completion_token 成功 claim 的持久化 fencing 回执 / Persisted fencing receipt for the successful claim.
    @param last_error 最近错误摘要 / Most recent error summary.
    @param input_revision 当前用户输入 revision；零表示初始消息 /
        Current user-input revision; zero denotes the initial message.
    """

    draft: InferenceActivityDraft
    status: InferenceActivityStatus
    version: int
    attempt_count: int
    next_attempt_at: datetime | None
    updated_at: datetime
    retry_budget_used: int = 0
    completed_at: datetime | None = None
    completion_token: LeaseToken | None = None
    last_error: str | None = None
    input_revision: TurnRevision = field(default_factory=TurnRevision.initial)

    def __post_init__(self) -> None:
        """@brief 校验状态、调度与完成回执不变量 / Validate status, scheduling, and completion-receipt invariants.

        @return None / None.
        @raise ValueError 版本、计数或状态相关字段不一致时抛出 / Raised for invalid versions, counts, or state-dependent fields.
        """

        if self.version < 0 or self.attempt_count < 0:
            raise ValueError(
                "Inference activity version and attempts cannot be negative"
            )
        if (
            isinstance(self.retry_budget_used, bool)
            or not isinstance(self.retry_budget_used, int)
            or not 0 <= self.retry_budget_used <= self.attempt_count
        ):
            raise ValueError(
                "Inference retry budget usage must be an integer between zero "
                "and the claim-attempt count"
            )
        if (
            self.status is InferenceActivityStatus.PROCESSING
            and self.retry_budget_used >= self.attempt_count
        ):
            raise ValueError(
                "A processing inference activity requires an unfinalized claim "
                "beyond the consumed retry budget"
            )
        if not isinstance(self.input_revision, TurnRevision):
            raise TypeError("Inference activity input_revision must be TurnRevision")
        updated_at = ensure_utc(self.updated_at)
        if updated_at < self.draft.created_at:
            raise ValueError("Inference activity updated_at cannot precede created_at")
        next_attempt_at = (
            ensure_utc(self.next_attempt_at) if self.next_attempt_at else None
        )
        completed_at = ensure_utc(self.completed_at) if self.completed_at else None
        if (self.status in INFERENCE_ACTIVITY_CLAIMABLE_STATES) != (
            next_attempt_at is not None
        ):
            raise ValueError(
                "Only claimable inference activities require next_attempt_at"
            )
        completion_fields_present = (
            completed_at is not None and self.completion_token is not None
        )
        completion_fields_absent = (
            completed_at is None and self.completion_token is None
        )
        if (
            self.status is InferenceActivityStatus.COMPLETED
            and not completion_fields_present
        ) or (
            self.status is not InferenceActivityStatus.COMPLETED
            and not completion_fields_absent
        ):
            raise ValueError(
                "Completed inference activities require time and fencing receipt"
            )
        if completed_at is not None and completed_at < self.draft.created_at:
            raise ValueError("Inference completion cannot precede activity creation")
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "next_attempt_at", next_attempt_at)
        object.__setattr__(self, "completed_at", completed_at)

    @classmethod
    def pending(cls, draft: InferenceActivityDraft) -> Self:
        """@brief 从 durable intent 创建待领取活动 / Create a pending activity from a durable intent.

        @param draft 不可变活动意图 / Immutable activity intent.
        @return 初始待领取活动 / Initial pending activity.
        """

        return cls(
            draft=draft,
            status=InferenceActivityStatus.PENDING,
            version=0,
            attempt_count=0,
            next_attempt_at=draft.created_at,
            updated_at=draft.created_at,
        )

    @property
    def activity_id(self) -> InferenceActivityId:
        """@brief 返回活动 ID / Return the activity identifier.

        @return 活动 ID / Activity identifier.
        """

        return self.draft.activity_id

    @property
    def turn_id(self) -> TurnId:
        """@brief 返回所属回合 / Return the owning turn.

        @return 回合 ID / Turn identifier.
        """

        return self.draft.turn_id

    @property
    def conversation_id(self) -> ConversationId:
        """@brief 返回所属会话 / Return the owning conversation.

        @return 会话 ID / Conversation identifier.
        """

        return self.draft.conversation_id

    @property
    def request(self) -> JsonObject:
        """@brief 返回 provider-neutral 请求 / Return the provider-neutral request.

        @return 结构化请求 / Structured request.
        """

        return self.draft.request


@dataclass(frozen=True, slots=True)
class InferenceActivityClaim:
    """@brief 带 fencing token 的推理活动领取凭证 / Inference-activity claim carrying a fencing token.

    @param activity 已进入 processing 的活动 / Activity now in processing state.
    @param token 本次领取 fencing token / Fencing token for this claim.
    @param lease_expires_at 租约截止时间 / Lease expiration time.
    @param cause 本次 generation 的 claim 原因 / Claim cause of this generation.
    """

    activity: InferenceActivity
    token: LeaseToken
    lease_expires_at: datetime
    cause: InferenceGenerationCause = InferenceGenerationCause.INITIAL

    def __post_init__(self) -> None:
        """@brief 校验 claim 状态与租约 / Validate claim state and lease.

        @return None / None.
        @raise ValueError 活动不在 processing 或租约无效时抛出 / Raised when the activity is not processing or its lease is invalid.
        """

        lease_expires_at = ensure_utc(self.lease_expires_at)
        if self.activity.status is not InferenceActivityStatus.PROCESSING:
            raise ValueError("Inference activity claims require processing status")
        if lease_expires_at <= self.activity.updated_at:
            raise ValueError("Inference activity lease must expire after claim time")
        if (
            self.cause is InferenceGenerationCause.STEER
            and int(self.activity.input_revision) < 1
        ):
            raise ValueError("A steer generation requires a positive input revision")
        object.__setattr__(self, "lease_expires_at", lease_expires_at)

    @property
    def generation_fence(self) -> InferenceGenerationFence:
        """@brief 投影本次 claim 的强类型 generation fence / Project the typed generation fence for this claim.

        @return activity、claim token、attempt 与 input revision 的不可分值 /
            Indivisible activity, claim-token, attempt, and input-revision value.
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
    """@brief 一次 processing generation 的跨进程 fencing 身份 / Cross-process fencing identity for one processing generation.

    @param activity_id durable inference activity / Durable inference activity.
    @param turn_id 所属 Turn / Owning Turn.
    @param claim_token 本次 lease token / Lease token for this claim.
    @param attempt provider generation 序号 / Provider-generation ordinal.
    @param input_revision 同 Turn steer revision / Same-Turn steer revision.
    @param cause generation 启动原因 / Cause that started the generation.
    """

    activity_id: InferenceActivityId
    turn_id: TurnId
    claim_token: LeaseToken
    attempt: int
    input_revision: TurnRevision
    cause: InferenceGenerationCause

    def __post_init__(self) -> None:
        """@brief 校验 generation 身份 / Validate generation identity.

        @return None / None.
        """

        if isinstance(self.attempt, bool) or self.attempt < 1:
            raise ValueError("Inference generation attempt must be positive")
        if not isinstance(self.input_revision, TurnRevision):
            raise TypeError("Inference generation input_revision must be TurnRevision")


@dataclass(frozen=True, slots=True)
class InferenceActivityEnqueueResult:
    """@brief 幂等活动意图写入结果 / Idempotent inference-activity enqueue result.

    @param activity 数据库中的规范活动 / Canonical stored activity.
    @param inserted 本次是否插入 / Whether this call inserted the row.
    """

    activity: InferenceActivity
    inserted: bool
