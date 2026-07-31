"""@brief Conversation workflow adapter 测试构造器 / Test builders for Conversation workflow adapters."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID

from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    ConversationMessageId,
    DeliveryStreamId,
    InferenceActivityId,
    LeaseToken,
    MessageSequence,
    OutboundMessageId,
    TurnId,
    TurnRevision,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inference import (
    InferenceActivity,
    InferenceActivityDraft,
    InferenceActivityStatus,
)
from fogmoe_bot.domain.conversation.message import (
    ConversationMessage,
    MessageAppendResult,
    MessageDraft,
    MessageRole,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundDraft,
)
from fogmoe_bot.domain.conversation.turn import ConversationTurn
from fogmoe_bot.infrastructure.database.conversation_workflow import turn_uow

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
"""@brief 测试用确定性 UTC 时间 / Deterministic UTC timestamp for tests."""

TURN_UUID = UUID("11111111-1111-4111-8111-111111111111")
"""@brief 测试回合 UUID / Test turn UUID."""

OUTBOUND_UUID = UUID("22222222-2222-4222-8222-222222222222")
"""@brief 测试出站 UUID / Test outbound UUID."""

MESSAGE_UUID = UUID("33333333-3333-4333-8333-333333333333")
"""@brief 测试会话消息 UUID / Test conversation-message UUID."""

TRACEPARENT = "00-11111111111141118111111111111111-2222222222224222-01"
"""@brief 测试用 durable trace carrier / Durable trace carrier used in tests."""

LEASE_UUID = UUID("44444444-4444-4444-8444-444444444444")
"""@brief 测试 fencing token / Test fencing token."""

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


class _TransactionContext:
    """@brief 可控异步事务上下文 / Controllable asynchronous transaction context."""

    def __init__(self, connection: object) -> None:
        """@brief 保存模拟连接 / Store the fake connection.

        @param connection 模拟数据库连接 / Fake database connection.
        """

        self.connection = connection
        self.exception: object | None = None

    async def __aenter__(self) -> object:
        """@brief 进入事务 / Enter the transaction.

        @return 模拟连接 / Fake connection.
        """

        return self.connection

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        """@brief 退出事务 / Exit the transaction.

        @param exc_type 异常类型 / Exception type.
        @param exc 异常 / Exception.
        @param traceback 回溯 / Traceback.
        @return None / None.
        """

        self.exception = exc
        return None


def _outbound_row(
    *,
    status: str = "processing",
    turn_id: UUID | None = TURN_UUID,
) -> tuple[object, ...]:
    """@brief 构造 outbox 数据库行 / Build an outbox database row.

    @param status 持久化状态 / Persisted status.
    @param turn_id 可选来源 Turn / Optional source Turn.
    @return 与仓储 SELECT 一致的行 / Row matching the repository SELECT shape.
    """

    next_attempt_at = NOW if status in {"pending", "retry_wait"} else None
    delivered_at = NOW if status == "delivered" else None
    version = {
        "pending": 0,
        "processing": 1,
        "retry_wait": 2,
        "delivered": 2,
        "failed_final": 2,
        "cancelled": 1,
    }[status]
    attempt_count = 0 if status in {"pending", "cancelled"} else 1
    last_error = (
        "temporary delivery failure"
        if status == "retry_wait"
        else "permanent delivery failure"
        if status == "failed_final"
        else None
    )
    return (
        OUTBOUND_UUID,
        "telegram:chat:-100:user:42:thread:9",
        turn_id,
        "telegram:primary:chat:-100:thread:9",
        7,
        "telegram.send_message",
        {"chat_id": -100, "text": "hello"},
        "turn:answer",
        status,
        version,
        attempt_count,
        next_attempt_at,
        NOW - timedelta(seconds=1),
        NOW,
        delivered_at,
        None,
        last_error,
        TRACEPARENT,
    )


def _outbound_claim_row(
    *,
    previous_status: str,
    turn_id: UUID | None = TURN_UUID,
) -> tuple[object, ...]:
    """@brief 构造带领取前状态的 outbox 行 / Build an outbox row carrying its pre-claim status.

    @param previous_status 领取前状态 / Status before claiming.
    @param turn_id 可选来源 Turn / Optional source Turn.
    @return claim UPDATE RETURNING 行 / Claim UPDATE RETURNING row.
    """

    processing = list(_outbound_row(status="processing", turn_id=turn_id))
    if previous_status == "pending":
        previous_version = 0
        previous_attempt_count = 0
        previous_error = None
    elif previous_status == "retry_wait":
        previous_version = 2
        previous_attempt_count = 1
        previous_error = "temporary delivery failure"
        processing[9] = 3
        processing[10] = 2
    else:
        raise ValueError(f"Unsupported pre-claim status: {previous_status}")
    return (
        *processing,
        previous_status,
        previous_version,
        previous_attempt_count,
        NOW,
        NOW,
        previous_error,
    )


def _outbound_cancellation_row(
    *,
    cancelled_at: datetime,
    previous_status: str = "pending",
    turn_id: UUID | None = TURN_UUID,
) -> tuple[object, ...]:
    """@brief 构造取消操作的完整 pre/post 行 / Build a complete cancellation pre/post row.

    @param cancelled_at 取消时刻 / Cancellation time.
    @param previous_status 取消前 pending 或 retry_wait / Pending or retry-wait status before cancellation.
    @param turn_id 可选来源 Turn / Optional source Turn.
    @return bulk UPDATE RETURNING 行 / Bulk UPDATE RETURNING row.
    """

    if previous_status == "pending":
        previous_version = 0
        previous_attempt_count = 0
        previous_error = None
    elif previous_status == "retry_wait":
        previous_version = 2
        previous_attempt_count = 1
        previous_error = "temporary delivery failure"
    else:
        raise ValueError(f"Unsupported pre-cancellation status: {previous_status}")
    cancelled = list(_outbound_row(status="cancelled", turn_id=turn_id))
    cancelled[9] = previous_version + 1
    cancelled[10] = previous_attempt_count
    cancelled[13] = cancelled_at
    cancelled[16] = (
        "delivery plan cancelled after permanent sibling failure"
    )
    return (
        *cancelled,
        None,
        None,
        previous_status,
        previous_version,
        previous_attempt_count,
        NOW if previous_status == "pending" else cancelled_at,
        NOW,
        None,
        None,
        previous_error,
        None,
        None,
    )


def _outbound_recovery_row(
    *,
    recovered_at: datetime,
    turn_id: UUID | None = TURN_UUID,
) -> tuple[object, ...]:
    """@brief 构造过期 lease recovery 的完整 pre/post 行 / Build a complete expired-lease recovery pre/post row.

    @param recovered_at 回收时刻 / Recovery time.
    @param turn_id 可选来源 Turn / Optional source Turn.
    @return bulk UPDATE RETURNING 行 / Bulk UPDATE RETURNING row.
    """

    previous_updated_at = recovered_at - timedelta(seconds=1)
    recovered = list(_outbound_row(status="retry_wait", turn_id=turn_id))
    recovered[11] = recovered_at + timedelta(microseconds=1)
    recovered[13] = recovered_at
    recovered[16] = "recovered expired worker lease"
    return (
        *recovered,
        None,
        None,
        "processing",
        1,
        1,
        None,
        previous_updated_at,
        None,
        None,
        None,
        LEASE_UUID,
        recovered_at,
    )


def _turn_row(
    *,
    state: str = "received",
    version: int = 0,
    inference_attempts: int = 0,
    delivery_attempts: int = 0,
    next_retry_at: datetime | None = None,
    last_error: str | None = None,
) -> tuple[object, ...]:
    """@brief 构造回合数据库行 / Build a turn database row.

    @param state 持久化状态 / Persisted state.
    @param version 乐观版本 / Optimistic version.
    @param inference_attempts 推理尝试次数 / Inference attempt count.
    @param delivery_attempts 投递尝试次数 / Delivery attempt count.
    @param next_retry_at 下一次重试时间 / Next retry time.
    @param last_error 最近错误 / Most recent error.
    @return 与仓储 SELECT 一致的行 / Row matching the repository SELECT shape.
    """

    return (
        TURN_UUID,
        "telegram:chat:-100:user:42:thread:9",
        "telegram.update",
        "99",
        99,
        state,
        version,
        inference_attempts,
        delivery_attempts,
        next_retry_at,
        last_error,
        NOW - timedelta(seconds=1),
        NOW,
    )


def _message_draft(*, role: MessageRole) -> MessageDraft:
    """@brief 构造用户或助手消息草稿 / Build a user or assistant message draft.

    @param role 消息角色 / Message role.
    @return 消息草稿 / Message draft.
    """

    return MessageDraft(
        message_id=ConversationMessageId(MESSAGE_UUID),
        conversation_id=ConversationId("telegram:chat:-100:user:42:thread:9"),
        turn_id=TurnId(TURN_UUID),
        source_update_id=None if role is MessageRole.ASSISTANT else UpdateId(99),
        role=role,
        content={"text": "hello"},
        idempotency_key=f"turn:{role.value}:message",
        created_at=NOW,
    )


def _initial_turn() -> ConversationTurn:
    """@brief 构造初始 RECEIVED Turn / Build an initial RECEIVED Turn.

    @return 初始回合 / Initial turn.
    """

    return turn_uow._map_turn(_turn_row())


def _message_result(
    draft: MessageDraft,
    *,
    inserted: bool,
) -> MessageAppendResult:
    """@brief 构造消息追加结果 / Build a message-append result.

    @param draft 消息草稿 / Message draft.
    @param inserted 是否本次插入 / Whether inserted by this call.
    @return 消息追加结果 / Message-append result.
    """

    return MessageAppendResult(
        message=ConversationMessage(
            draft=draft,
            sequence=MessageSequence(1),
        ),
        inserted=inserted,
    )


def _outbound_draft() -> OutboundDraft:
    """@brief 构造出站副作用草稿 / Build an outbound-effect draft.

    @return 出站草稿 / Outbound draft.
    """

    return OutboundDraft(
        message_id=OutboundMessageId(OUTBOUND_UUID),
        conversation_id=ConversationId("telegram:chat:-100:user:42:thread:9"),
        turn_id=TurnId(TURN_UUID),
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:-100:thread:9"),
        kind=SEND_TELEGRAM_MESSAGE,
        payload={"chat_id": -100, "text": "hello"},
        idempotency_key="turn:answer",
        created_at=NOW,
    )


def _standalone_outbound_draft() -> OutboundDraft:
    """@brief 构造无 Turn 的拒绝反馈 / Build standalone rejection feedback."""

    conversation_id = ConversationId("telegram:chat:-100:user:42:thread:9")
    idempotency_key = "update:99:assistant-feedback:text_too_long"
    return OutboundDraft(
        message_id=OutboundMessageId.for_conversation(
            conversation_id,
            idempotency_key,
        ),
        conversation_id=conversation_id,
        turn_id=None,
        delivery_stream_id=DeliveryStreamId("telegram:primary:chat:-100:thread:9"),
        kind=SEND_TELEGRAM_MESSAGE,
        payload={"chat_id": -100, "text": "too long"},
        idempotency_key=idempotency_key,
        created_at=NOW,
    )


def _activity_draft() -> InferenceActivityDraft:
    """@brief 构造 primary 推理活动意图 / Build a primary inference-activity intent.

    @return 活动草稿 / Activity draft.
    """

    turn_id = TurnId(TURN_UUID)
    return InferenceActivityDraft(
        activity_id=InferenceActivityId.for_turn(turn_id),
        turn_id=turn_id,
        conversation_id=ConversationId("telegram:chat:-100:user:42:thread:9"),
        request={"prompt": "hello"},
        created_at=NOW,
    )


def _activity(
    *,
    status: InferenceActivityStatus = InferenceActivityStatus.PROCESSING,
    completion_token: LeaseToken | None = None,
    input_revision: int = 0,
    retry_budget_used: int = 0,
) -> InferenceActivity:
    """@brief 构造推理活动快照 / Build an inference-activity snapshot.

    @param status 活动状态 / Activity status.
    @param completion_token 可选完成 fencing 回执 / Optional completion fencing receipt.
    @param input_revision 用户输入 revision / User-input revision.
    @param retry_budget_used 已持久化的普通重试预算用量 /
        Persisted ordinary retry-budget usage.
    @return 活动快照 / Activity snapshot.
    """

    next_attempt_at = (
        NOW
        if status
        in {
            InferenceActivityStatus.PENDING,
            InferenceActivityStatus.STEER_PENDING,
            InferenceActivityStatus.RETRY,
        }
        else None
    )
    return InferenceActivity(
        draft=_activity_draft(),
        status=status,
        version=1,
        attempt_count=1,
        next_attempt_at=next_attempt_at,
        updated_at=NOW,
        retry_budget_used=retry_budget_used,
        completed_at=NOW if status is InferenceActivityStatus.COMPLETED else None,
        completion_token=completion_token,
        input_revision=TurnRevision(input_revision),
    )


def _inbound_row(*, status: str = "processing") -> tuple[object, ...]:
    """@brief 构造 inbox 数据库行 / Build an inbox database row.

    @param status 持久化状态 / Persisted status.
    @return 与仓储 SELECT 一致的行 / Row matching the repository SELECT shape.
    """

    next_attempt_at = NOW if status in {"pending", "retry_wait"} else None
    version = {
        "pending": 0,
        "processing": 1,
        "retry_wait": 2,
        "processed": 2,
        "failed_final": 2,
    }[status]
    attempt_count = 0 if status == "pending" else 1
    processed_at = NOW if status == "processed" else None
    last_error = (
        "transient failure"
        if status == "retry_wait"
        else "permanent failure"
        if status == "failed_final"
        else None
    )
    return (
        99,
        "telegram:chat:-100:user:42:thread:9",
        {"update_id": 99},
        status,
        version,
        attempt_count,
        next_attempt_at,
        NOW - timedelta(seconds=1),
        NOW,
        processed_at,
        last_error,
        TRACEPARENT,
    )


def _inbound_claim_row(*, previous_status: str = "pending") -> tuple[object, ...]:
    """@brief 构造同时携带领取前后状态的 inbox 行 / Build an inbox row carrying pre- and post-claim state.

    @param previous_status 领取前状态 / State before the claim.
    @return claim UPDATE RETURNING 行 / Claim UPDATE RETURNING row.
    """

    previous = _inbound_row(status=previous_status)
    processing = list(_inbound_row(status="processing"))
    processing[4] = cast(int, previous[4]) + 1
    processing[5] = cast(int, previous[5]) + 1
    return (
        *processing,
        previous[3],
        previous[4],
        previous[5],
        previous[6],
        previous[8],
        previous[9],
        previous[10],
    )
