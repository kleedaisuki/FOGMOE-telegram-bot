"""跨 aggregate 原子工作流结果 / Cross-aggregate atomic workflow results."""

from collections.abc import Sequence
from dataclasses import dataclass

from .inference import (
    InferenceActivity,
    InferenceActivityEnqueueResult,
    InferenceActivityStatus,
)
from .message import MessageAppendResult
from .outbox import OutboundEnqueueResult
from .turn import (
    POST_ACCEPTANCE_TURN_STATES,
    POST_INFERENCE_COMPLETION_TURN_STATES,
    ConversationTurn,
)


@dataclass(frozen=True, slots=True)
class TurnAcceptanceResult:
    """@brief 原子接受回合的结果 / Result of atomically accepting a turn.

    @param turn 已进入 WAITING_INFERENCE 的回合 / Turn now in WAITING_INFERENCE.
    @param user_message 幂等追加的用户消息 / Idempotently appended user message.
    @param inference_activity 幂等写入的长推理意图 / Idempotently persisted long-inference intent.
    """

    turn: ConversationTurn
    user_message: MessageAppendResult
    inference_activity: InferenceActivityEnqueueResult

    def __post_init__(self) -> None:
        """@brief 校验接受结果状态 / Validate the acceptance result state.

        @return None / None.
        @raise ValueError 回合未进入 WAITING_INFERENCE 时抛出 / Raised when the turn is not in WAITING_INFERENCE.
        """

        if self.turn.state not in POST_ACCEPTANCE_TURN_STATES:
            raise ValueError("Acceptance result requires a post-acceptance turn state")
        if self.inference_activity.activity.turn_id != self.turn.turn_id:
            raise ValueError("Acceptance activity must belong to the accepted turn")


@dataclass(frozen=True, slots=True)
class InferenceCompletionResult:
    """@brief 原子完成推理并写 outbox 的结果 / Result of atomically completing inference and writing the outbox.

    @param turn 已进入 WAITING_DELIVERY 的回合 / Turn now in WAITING_DELIVERY.
    @param activity 已成功 fenced 完成的活动 / Successfully fenced completed activity.
    @param assistant_message 幂等追加的助手消息 / Idempotently appended assistant message.
    @param outbounds 幂等入队的有序投递副作用 / Idempotently enqueued ordered delivery effects.
    """

    turn: ConversationTurn
    activity: InferenceActivity
    assistant_message: MessageAppendResult
    outbounds: Sequence[OutboundEnqueueResult]

    def __post_init__(self) -> None:
        """@brief 校验推理完成结果状态 / Validate the inference-completion result state.

        @return None / None.
        @raise ValueError 回合未进入 WAITING_DELIVERY 时抛出 / Raised when the turn is not in WAITING_DELIVERY.
        """

        if self.turn.state not in POST_INFERENCE_COMPLETION_TURN_STATES:
            raise ValueError(
                "Inference-completion result requires a post-completion turn state"
            )
        if self.activity.status is not InferenceActivityStatus.COMPLETED:
            raise ValueError(
                "Inference-completion result requires a completed activity"
            )
        if self.activity.turn_id != self.turn.turn_id:
            raise ValueError("Completed activity must belong to the result turn")
        if not self.outbounds:
            raise ValueError(
                "Inference completion requires at least one outbound effect"
            )
