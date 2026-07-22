"""@brief Durable Conversation acceptance 工作流 / Durable Conversation acceptance workflow.

本模块只把入口命令规范化成稳定 identity，并以一个短事务提交用户消息和
provider-neutral 推理活动意图。推理 I/O 与完成提交由独立 activity worker 负责。/
This module only normalizes ingress commands into stable identities and commits the user
message plus provider-neutral inference intent in one short transaction. A separate activity
worker owns inference I/O and completion commits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Mapping
from typing import Protocol

from fogmoe_bot.domain.assistant.messages import (
    CanonicalMessage,
    CanonicalMessageError,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    InferenceActivityId,
    TurnId,
    TurnSource,
)
from fogmoe_bot.domain.conversation.inference import InferenceActivityDraft
from fogmoe_bot.domain.conversation.message import (
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.turn import ConversationTurn
from fogmoe_bot.domain.conversation.workflow_results import TurnAcceptanceResult
from fogmoe_bot.domain.observability.trace import TraceContext
from fogmoe_bot.domain.temporal import ensure_utc


class TurnWorkflowPersistence(Protocol):
    """@brief acceptance 所需最小持久化端口 / Minimal persistence port required by acceptance."""

    async def create_and_accept_turn(
        self,
        turn: ConversationTurn,
        *,
        message: MessageDraft,
        activity: InferenceActivityDraft,
        accepted_at: datetime,
    ) -> TurnAcceptanceResult:
        """@brief 原子创建并接受回合、消息与推理意图 / Atomically create and accept a turn, message, and inference intent.

        @param turn 初始 RECEIVED 回合 / Initial RECEIVED turn.
        @param message 确定性用户消息 / Deterministic user message.
        @param activity 确定性 primary inference intent / Deterministic primary inference intent.
        @param accepted_at 接受时间 / Acceptance time.
        @return acceptance receipt / Acceptance receipt.
        """

        ...


@dataclass(frozen=True, slots=True)
class AcceptConversationTurn:
    """@brief 将 durable 来源接受为 Conversation Turn / Accept a durable source as a Conversation Turn.

    @param source Telegram、调度或其他 durable 来源 / Telegram, scheduling, or another durable source.
    @param conversation_id 长期会话 identity / Long-lived conversation identity.
    @param user_content 含 canonical V2 ``model_message`` 的入口用户内容 /
        Ingress user content carrying a canonical V2 ``model_message``.
    @param inference_request provider-neutral 推理请求 / Provider-neutral inference request.
    @param received_at listener 首次观察时间 / Time first observed by the listener.
    @param accepted_at 应用接受时间 / Application acceptance time.
    """

    source: TurnSource
    conversation_id: ConversationId
    user_content: JsonObject
    inference_request: JsonObject
    received_at: datetime
    accepted_at: datetime
    trace_context: TraceContext = field(default_factory=TraceContext.new_root)

    def __post_init__(self) -> None:
        """@brief 校验命令时间并隔离可变 JSON / Validate command timing and isolate mutable JSON.

        @return None / None.
        @raise ValueError 接受时间早于接收时间时抛出 / Raised when acceptance precedes receipt.
        """

        if not isinstance(self.source, TurnSource):
            raise TypeError("Conversation acceptance source must be a TurnSource")
        received_at = ensure_utc(self.received_at)
        accepted_at = ensure_utc(self.accepted_at)
        if accepted_at < received_at:
            raise ValueError("accepted_at cannot precede received_at")
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "accepted_at", accepted_at)
        object.__setattr__(
            self,
            "user_content",
            normalize_user_content_model_message(self.user_content),
        )
        object.__setattr__(self, "inference_request", dict(self.inference_request))
        if not isinstance(self.trace_context, TraceContext):
            raise TypeError("Conversation acceptance requires a TraceContext")


def normalize_user_content_model_message(content: JsonObject) -> JsonObject:
    """@brief 严格验证并规范化入口的 canonical user message / Strictly validate and normalize an ingress canonical user message.

    @param content 将被持久化的用户内容 envelope / User-content envelope to be persisted.
    @return 含 canonical V2 ``model_message`` 的独立 envelope / Independent envelope with a canonical V2 ``model_message``.
    @raise ValueError 模型消息缺失、不是 V2，或角色不是 user 时抛出 /
        Raised when the model message is missing, not V2, or not a user role.
    @note 此边界故意不升级旧 OpenAI wire JSON；历史数据只能经显式数据库迁移进入 V2。/
        This boundary intentionally does not upgrade legacy OpenAI wire JSON; historical data
        can enter V2 only through the explicit database migration.
    """

    raw_message = content.get("model_message")
    if not isinstance(raw_message, Mapping):
        raise ValueError("Conversation ingress requires a canonical V2 model_message")
    try:
        message = CanonicalMessage.from_json(raw_message)
    except CanonicalMessageError as error:
        raise ValueError("Conversation ingress model_message must be canonical V2") from error
    if message.role is not MessageRole.USER:
        raise ValueError("Conversation ingress model_message must have the user role")
    normalized = dict(content)
    normalized["model_message"] = message.to_json()
    return normalized


@dataclass(frozen=True, slots=True)
class PreparedTurnAcceptance:
    """@brief 已确定 identity、可原子提交的 acceptance / Acceptance with deterministic identities ready for atomic commit.

    @param turn 初始 RECEIVED 回合 / Initial RECEIVED turn.
    @param message 确定性用户消息 / Deterministic user message.
    @param activity 确定性 primary inference intent / Deterministic primary inference intent.
    @param accepted_at 接受时间 / Acceptance time.
    """

    turn: ConversationTurn
    message: MessageDraft
    activity: InferenceActivityDraft
    accepted_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验 prepared acceptance 的同一 Turn 边界 / Validate the shared Turn boundary of a prepared acceptance.

        @return None / None.
        @raise ValueError 组成部分不属于同一 Turn/会话时抛出 / Raised when components do not belong to one Turn and conversation.
        """

        accepted_at = ensure_utc(self.accepted_at)
        if self.message.turn_id != self.turn.turn_id:
            raise ValueError("Prepared message must belong to the prepared Turn")
        if self.activity.turn_id != self.turn.turn_id:
            raise ValueError("Prepared activity must belong to the prepared Turn")
        if (
            self.message.conversation_id != self.turn.conversation_id
            or self.activity.conversation_id != self.turn.conversation_id
        ):
            raise ValueError("Prepared effects must belong to the Turn conversation")
        object.__setattr__(self, "accepted_at", accepted_at)


class ConversationWorkflow:
    """@brief 构造稳定 identity 并调用 acceptance UoW / Build stable identities and invoke the acceptance unit of work."""

    def __init__(self, persistence: TurnWorkflowPersistence) -> None:
        """@brief 创建工作流 / Create the workflow.

        @param persistence 原子 acceptance persistence / Atomic acceptance persistence.
        """

        self._persistence = persistence

    async def accept(self, command: AcceptConversationTurn) -> TurnAcceptanceResult:
        """@brief 幂等创建并接受一个回合 / Idempotently create and accept one turn.

        @param command durable ingress 命令 / Durable-ingress command.
        @return acceptance receipt / Acceptance receipt.
        @note Turn、用户消息与推理意图共享一个短事务；失败不会遗留孤立 RECEIVED Turn。/
        The Turn, user message, and inference intent share one short transaction; failure cannot
        leave an orphan RECEIVED Turn.
        """

        prepared = self.prepare(command)
        return await self._persistence.create_and_accept_turn(
            prepared.turn,
            message=prepared.message,
            activity=prepared.activity,
            accepted_at=prepared.accepted_at,
        )

    @staticmethod
    def prepare(command: AcceptConversationTurn) -> PreparedTurnAcceptance:
        """@brief 纯构造确定性 acceptance 组成部分 / Purely build deterministic acceptance components.

        @param command durable ingress 命令 / Durable-ingress command.
        @return 可交给普通或跨聚合事务端口的 prepared acceptance /
        Prepared acceptance usable by ordinary or cross-aggregate transaction ports.
        @note 此方法不执行 I/O，billing coordinator 可安全复用而无需复制 identity 规则。/
        This method performs no I/O, allowing billing coordinators to reuse it without copying identity rules.
        """

        turn_id = TurnId.for_source(command.source)
        initial = ConversationTurn.received(
            turn_id=turn_id,
            conversation_id=command.conversation_id,
            source=command.source,
            received_at=command.received_at,
        )
        message = MessageDraft(
            message_id=ConversationMessageId.for_turn(turn_id, "user.input"),
            conversation_id=command.conversation_id,
            turn_id=turn_id,
            source_update_id=command.source.update_id,
            role=MessageRole.USER,
            content=command.user_content,
            idempotency_key=f"turn:{turn_id}:user:0",
            created_at=command.received_at,
        )
        activity = InferenceActivityDraft(
            activity_id=InferenceActivityId.for_turn(turn_id),
            turn_id=turn_id,
            conversation_id=command.conversation_id,
            request=command.inference_request,
            created_at=command.accepted_at,
            trace_context=command.trace_context,
        )
        return PreparedTurnAcceptance(
            turn=initial,
            message=message,
            activity=activity,
            accepted_at=command.accepted_at,
        )


__all__ = [
    "AcceptConversationTurn",
    "ConversationWorkflow",
    "PreparedTurnAcceptance",
    "TurnWorkflowPersistence",
    "normalize_user_content_model_message",
]
