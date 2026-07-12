"""Durable inference activity 模型 / Durable inference-activity models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from .identity import ConversationId, InferenceActivityId, LeaseToken, TurnId
from .payloads import JsonObject
from .temporal import ensure_utc


class InferenceActivityStatus(StrEnum):
    """@brief 可恢复推理活动状态 / Recoverable inference-activity status."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY = "retry"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


INFERENCE_ACTIVITY_CLAIMABLE_STATES = frozenset(
    {InferenceActivityStatus.PENDING, InferenceActivityStatus.RETRY}
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

    def __post_init__(self) -> None:
        """@brief 校验活动草稿并隔离可变 JSON / Validate the activity draft and isolate mutable JSON.

        @return None / None.
        """

        object.__setattr__(self, "request", dict(self.request))
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


@dataclass(frozen=True, slots=True)
class InferenceActivity:
    """@brief 可版本化、可租约恢复的推理活动快照 / Versioned, lease-recoverable inference-activity snapshot.

    @param draft 不可变活动意图 / Immutable activity intent.
    @param status 当前状态 / Current status.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param attempt_count 已领取次数 / Number of claims made.
    @param next_attempt_at 下一次可领取时间 / Next claimable time.
    @param updated_at 最近状态更新时间 / Most recent state-update time.
    @param completed_at 成功提交时间 / Successful commit time.
    @param completion_token 成功 claim 的持久化 fencing 回执 / Persisted fencing receipt for the successful claim.
    @param last_error 最近错误摘要 / Most recent error summary.
    """

    draft: InferenceActivityDraft
    status: InferenceActivityStatus
    version: int
    attempt_count: int
    next_attempt_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None = None
    completion_token: LeaseToken | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        """@brief 校验状态、调度与完成回执不变量 / Validate status, scheduling, and completion-receipt invariants.

        @return None / None.
        @raise ValueError 版本、计数或状态相关字段不一致时抛出 / Raised for invalid versions, counts, or state-dependent fields.
        """

        if self.version < 0 or self.attempt_count < 0:
            raise ValueError(
                "Inference activity version and attempts cannot be negative"
            )
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
    """

    activity: InferenceActivity
    token: LeaseToken
    lease_expires_at: datetime

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
        object.__setattr__(self, "lease_expires_at", lease_expires_at)


@dataclass(frozen=True, slots=True)
class InferenceActivityEnqueueResult:
    """@brief 幂等活动意图写入结果 / Idempotent inference-activity enqueue result.

    @param activity 数据库中的规范活动 / Canonical stored activity.
    @param inserted 本次是否插入 / Whether this call inserted the row.
    """

    activity: InferenceActivity
    inserted: bool
