"""@brief Inference activity 状态与值对象 / Inference-activity states and values."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from fogmoe_bot.domain.temporal import ensure_utc

from .identity import LeaseToken


class InferenceActivityStatus(StrEnum):
    """@brief 可恢复推理活动的稳定持久化状态 / Stable persisted status of a recoverable inference activity."""

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


class InferenceRetryBudgetCharge(StrEnum):
    """@brief 失败 outcome 对普通重试预算的影响 / Effect of a failure outcome on the ordinary retry budget."""

    PRESERVE = "preserve"
    CONSUME = "consume"


INFERENCE_ACTIVITY_CLAIMABLE_STATES = frozenset(
    {
        InferenceActivityStatus.PENDING,
        InferenceActivityStatus.STEER_PENDING,
        InferenceActivityStatus.RETRY,
    }
)
"""@brief worker 可领取的持久化状态 / Persisted states claimable by workers."""


@dataclass(frozen=True, slots=True)
class InferenceFailure:
    """@brief 可安全持久化的推理失败摘要 / Safely persistable inference-failure summary.

    @param summary 去除首尾空白且有界的摘要 / Trimmed and bounded summary.
    """

    summary: str

    def __post_init__(self) -> None:
        """@brief 规范化失败摘要 / Normalize the failure summary.

        @return None / None.
        """

        if not isinstance(self.summary, str):
            raise TypeError("Inference failure summary must be a string")
        normalized = self.summary.strip()
        if not normalized:
            raise ValueError("Inference failure cannot be empty")
        object.__setattr__(self, "summary", normalized[:4000])


@dataclass(frozen=True, slots=True)
class AwaitingInitialInference:
    """@brief 等待首次 generation 领取 / Awaiting the initial generation claim.

    @param claimable_at 最早可领取时刻 / Earliest claim time.
    """

    claimable_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范化领取时刻 / Normalize the claim time.

        @return None / None.
        """

        object.__setattr__(self, "claimable_at", ensure_utc(self.claimable_at))


@dataclass(frozen=True, slots=True)
class ProcessingInference:
    """@brief 已被带 fencing capability 的 worker 领取 / Claimed by a worker carrying a fencing capability."""


@dataclass(frozen=True, slots=True)
class AwaitingSteeredInference:
    """@brief 等待已提升 input revision 的 generation / Awaiting a generation for an advanced input revision.

    @param claimable_at 最早可领取时刻 / Earliest claim time.
    """

    claimable_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范化领取时刻 / Normalize the claim time.

        @return None / None.
        """

        object.__setattr__(self, "claimable_at", ensure_utc(self.claimable_at))


@dataclass(frozen=True, slots=True)
class WaitingInferenceRetry:
    """@brief 持久失败 outcome 后等待重试 / Waiting for retry after a durable failure outcome.

    @param claimable_at 下次可领取时刻 / Next claim time.
    @param failure 最近失败 / Latest failure.
    """

    claimable_at: datetime
    failure: InferenceFailure

    def __post_init__(self) -> None:
        """@brief 校验重试等待状态 / Validate the retry-wait state.

        @return None / None.
        """

        object.__setattr__(self, "claimable_at", ensure_utc(self.claimable_at))
        if not isinstance(self.failure, InferenceFailure):
            raise TypeError("Inference retry requires an InferenceFailure")


@dataclass(frozen=True, slots=True)
class CompletedInference:
    """@brief 结果已与历史/outbox 原子提交 / Result atomically committed with history and outbox.

    @param completed_at 提交时刻 / Commit time.
    @param completion_token 成功 claim 的 fencing 回执 / Fencing receipt of the successful claim.
    """

    completed_at: datetime
    completion_token: LeaseToken

    def __post_init__(self) -> None:
        """@brief 校验完成回执 / Validate the completion receipt.

        @return None / None.
        """

        object.__setattr__(self, "completed_at", ensure_utc(self.completed_at))
        if not isinstance(self.completion_token, LeaseToken):
            raise TypeError("Completed inference requires a LeaseToken receipt")


@dataclass(frozen=True, slots=True)
class FailedInference:
    """@brief 不再自动重试的推理终态 / Inference terminal state excluded from automatic retries.

    @param failure 最终失败 / Final failure.
    """

    failure: InferenceFailure

    def __post_init__(self) -> None:
        """@brief 校验最终失败 / Validate the final failure.

        @return None / None.
        """

        if not isinstance(self.failure, InferenceFailure):
            raise TypeError("Failed inference requires an InferenceFailure")


@dataclass(frozen=True, slots=True)
class CancelledInference:
    """@brief 被 Turn 或 Conversation reset 围栏取消 / Fenced and cancelled by its Turn or a Conversation reset.

    @param last_failure 取消前可能已持久化的失败 / Optional failure persisted before cancellation.
    """

    last_failure: InferenceFailure | None = None

    def __post_init__(self) -> None:
        """@brief 校验取消终态 / Validate the cancelled terminal state.

        @return None / None.
        """

        if self.last_failure is not None and not isinstance(
            self.last_failure,
            InferenceFailure,
        ):
            raise TypeError("Cancelled inference failure must be InferenceFailure")


type InferenceActivityState = (
    AwaitingInitialInference
    | ProcessingInference
    | AwaitingSteeredInference
    | WaitingInferenceRetry
    | CompletedInference
    | FailedInference
    | CancelledInference
)
"""@brief 推理活动生命周期的穷尽状态和 / Exhaustive sum of inference-activity lifecycle states."""


class InvalidInferenceTransition(RuntimeError):
    """@brief 聚合拒绝了非法生命周期转换 / Aggregate rejected an illegal lifecycle transition."""
