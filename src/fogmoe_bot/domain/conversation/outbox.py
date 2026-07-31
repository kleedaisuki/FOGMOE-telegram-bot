"""@brief Transactional outbox 领域模型 / Transactional-outbox domain model."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from fogmoe_bot.domain.observability.trace import TraceContext
from fogmoe_bot.domain.temporal import ensure_utc

from .identity import (
    ConversationId,
    DeliveryStreamId,
    LeaseToken,
    MessageSequence,
    OutboundMessageId,
    TurnId,
    normalize_idempotency_key,
)
from .payloads import JsonObject


@dataclass(frozen=True, slots=True)
class OutboundKind:
    """@brief 可扩展出站动作类型 / Extensible outbound-action kind.

    @param value 稳定持久化名称 / Stable persisted name.
    """

    value: str

    def __post_init__(self) -> None:
        """@brief 校验动作类型 / Validate the action kind.

        @return None / None.
        @raise TypeError 类型不是字符串时抛出 / Raised when the kind is not a string.
        @raise ValueError 类型为空或过长时抛出 / Raised when the kind is empty or too long.
        """

        if not isinstance(self.value, str):
            raise TypeError("Outbound kind must be a string")
        normalized = self.value.strip().lower()
        if not normalized:
            raise ValueError("Outbound kind cannot be empty")
        if len(normalized) > 100:
            raise ValueError("Outbound kind cannot exceed 100 characters")
        object.__setattr__(self, "value", normalized)


SEND_TELEGRAM_MESSAGE = OutboundKind("telegram.send_message")
"""@brief Telegram 发送消息动作 / Telegram send-message action."""

SEND_TELEGRAM_ASSISTANT_PROGRESS = OutboundKind("telegram.send_assistant_progress")
"""@brief 可追加且不参与最终投递成败的 Assistant 过程消息 /
Assistant progress message independent of final-delivery success.
"""

EDIT_TELEGRAM_MESSAGE = OutboundKind("telegram.edit_message")
"""@brief Telegram 编辑消息动作 / Telegram edit-message action."""

SEND_TELEGRAM_ARTIFACT = OutboundKind("telegram.send_artifact")
"""@brief Telegram durable artifact 投递动作 / Telegram durable-artifact delivery action."""

SEND_TELEGRAM_STICKER = OutboundKind("telegram.send_sticker")
"""@brief Telegram 贴纸投递动作 / Telegram sticker-delivery action."""

SEND_TELEGRAM_PHOTO = OutboundKind("telegram.send_photo")
"""@brief Telegram 远程图片投递动作 / Telegram remote-photo delivery action."""


class OutboundStatus(StrEnum):
    """@brief 出站消息的稳定持久化状态 / Stable persisted outbound-message status."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    DELIVERED = "delivered"
    FAILED_FINAL = "failed_final"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, init=False)
class OutboundDraft:
    """@brief 尚未分配投递流序号的不可变出站意图 / Immutable outbound intent awaiting a stream sequence.

    @param message_id 出站消息 ID / Outbound-message identifier.
    @param conversation_id 会话键 / Conversation key.
    @param turn_id 可选来源回合；独立副作用为 None / Optional source Turn; None for standalone effects.
    @param delivery_stream_id 外部有序投递流 / External ordered-delivery stream.
    @param kind 动作类型 / Action kind.
    @param payload 动作载荷 / Action payload.
    @param idempotency_key 会话内副作用幂等键 / Conversation-scoped effect idempotency key.
    @param created_at 创建时间 / Creation time.
    @param trace_context 可持久传播的 trace / Persistable trace context.
    """

    message_id: OutboundMessageId
    conversation_id: ConversationId
    turn_id: TurnId | None
    delivery_stream_id: DeliveryStreamId
    kind: OutboundKind
    _payload: JsonObject
    idempotency_key: str
    created_at: datetime
    trace_context: TraceContext

    def __init__(
        self,
        *,
        message_id: OutboundMessageId,
        conversation_id: ConversationId,
        turn_id: TurnId | None,
        delivery_stream_id: DeliveryStreamId,
        kind: OutboundKind,
        payload: JsonObject,
        idempotency_key: str,
        created_at: datetime,
        trace_context: TraceContext | None = None,
    ) -> None:
        """@brief 创建并校验不可变出站意图 / Create and validate an immutable outbound intent.

        @param message_id 出站消息 ID / Outbound-message identifier.
        @param conversation_id 会话键 / Conversation key.
        @param turn_id 可选来源回合 / Optional source Turn.
        @param delivery_stream_id 外部有序投递流 / External ordered-delivery stream.
        @param kind 动作类型 / Action kind.
        @param payload 动作载荷 / Action payload.
        @param idempotency_key 会话内副作用幂等键 / Conversation-scoped idempotency key.
        @param created_at 创建时间 / Creation time.
        @param trace_context 可选可持久传播 trace / Optional persistable trace context.
        @return None / None.
        @raise TypeError identity、kind 或 trace 类型错误时抛出 / Raised for invalid identity, kind, or trace types.
        """

        context = trace_context or TraceContext.new_root()
        if not isinstance(message_id, OutboundMessageId):
            raise TypeError("Outbound draft requires an OutboundMessageId")
        if not isinstance(conversation_id, ConversationId):
            raise TypeError("Outbound draft requires a ConversationId")
        if turn_id is not None and not isinstance(turn_id, TurnId):
            raise TypeError("Outbound draft turn_id must be a TurnId or None")
        if not isinstance(delivery_stream_id, DeliveryStreamId):
            raise TypeError("Outbound draft requires a DeliveryStreamId")
        if not isinstance(kind, OutboundKind):
            raise TypeError("Outbound draft requires an OutboundKind")
        if not isinstance(context, TraceContext):
            raise TypeError("Outbound draft requires a TraceContext")
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "conversation_id", conversation_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "delivery_stream_id", delivery_stream_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "_payload", deepcopy(payload))
        object.__setattr__(
            self,
            "idempotency_key",
            normalize_idempotency_key(idempotency_key),
        )
        object.__setattr__(self, "created_at", ensure_utc(created_at))
        object.__setattr__(self, "trace_context", context)

    @property
    def payload(self) -> JsonObject:
        """@brief 返回隔离的动作载荷 / Return an isolated action payload.

        @return 深拷贝 JSON 载荷 / Deep-copied JSON payload.
        """

        return deepcopy(self._payload)


@dataclass(frozen=True, slots=True)
class OutboundFailure:
    """@brief 可安全持久化的投递失败摘要 / Safely persistable delivery-failure summary.

    @param summary 去除首尾空白且有界的摘要 / Trimmed, bounded summary.
    """

    summary: str

    def __post_init__(self) -> None:
        """@brief 规范化失败摘要 / Normalize the failure summary.

        @return None / None.
        @raise TypeError 摘要不是字符串时抛出 / Raised when the summary is not a string.
        @raise ValueError 摘要为空时抛出 / Raised when the summary is empty.
        """

        if not isinstance(self.summary, str):
            raise TypeError("Outbound failure summary must be a string")
        normalized = self.summary.strip()
        if not normalized:
            raise ValueError("Outbound failure cannot be empty")
        object.__setattr__(self, "summary", normalized[:4000])


@dataclass(frozen=True, slots=True)
class AwaitingOutboundDelivery:
    """@brief 等待首次投递领取 / Awaiting the first delivery claim.

    @param claimable_at 最早可领取时刻 / Earliest claimable time.
    """

    claimable_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范化领取时刻 / Normalize the claimable time.

        @return None / None.
        """

        object.__setattr__(self, "claimable_at", ensure_utc(self.claimable_at))


@dataclass(frozen=True, slots=True)
class ProcessingOutboundDelivery:
    """@brief 已由带 fencing token 的 worker 领取 / Claimed by a worker carrying a fencing token."""


@dataclass(frozen=True, slots=True)
class WaitingOutboundRetry:
    """@brief 失败后等待再次投递 / Waiting for another delivery attempt after failure.

    @param claimable_at 最早可再次领取时刻 / Earliest reclaim time.
    @param failure 最近一次失败 / Most recent failure.
    """

    claimable_at: datetime
    failure: OutboundFailure

    def __post_init__(self) -> None:
        """@brief 校验重试等待状态 / Validate the retry-wait state.

        @return None / None.
        """

        object.__setattr__(self, "claimable_at", ensure_utc(self.claimable_at))
        if not isinstance(self.failure, OutboundFailure):
            raise TypeError("Outbound retry state requires an OutboundFailure")


@dataclass(frozen=True, slots=True)
class DeliveredOutboundMessage:
    """@brief 外部投递成功终态 / Successfully delivered terminal state.

    @param delivered_at 外部调用成功时刻 / External-delivery success time.
    @param external_message_id 外部系统回执 ID / External-system receipt identifier.
    """

    delivered_at: datetime
    external_message_id: str | None

    def __post_init__(self) -> None:
        """@brief 校验投递回执 / Validate the delivery receipt.

        @return None / None.
        @raise TypeError 外部 ID 既不是字符串也不是 None 时抛出 / Raised when the external ID is neither a string nor None.
        """

        object.__setattr__(self, "delivered_at", ensure_utc(self.delivered_at))
        if self.external_message_id is not None and not isinstance(
            self.external_message_id,
            str,
        ):
            raise TypeError("External message ID must be a string or None")


@dataclass(frozen=True, slots=True)
class DeadLetteredOutboundMessage:
    """@brief 不再自动重试的投递终态 / Delivery terminal state excluded from automatic retries.

    @param failure 最终失败 / Final failure.
    """

    failure: OutboundFailure

    def __post_init__(self) -> None:
        """@brief 校验 dead-letter 状态 / Validate the dead-letter state.

        @return None / None.
        """

        if not isinstance(self.failure, OutboundFailure):
            raise TypeError("Dead-lettered outbound requires an OutboundFailure")


@dataclass(frozen=True, slots=True)
class CancelledOutboundMessage:
    """@brief 因所属投递计划终止而取消 / Cancelled because its owning delivery plan terminated.

    @param reason 可选取消原因 / Optional cancellation reason.
    """

    reason: OutboundFailure | None = None

    def __post_init__(self) -> None:
        """@brief 校验取消原因 / Validate the cancellation reason.

        @return None / None.
        """

        if self.reason is not None and not isinstance(self.reason, OutboundFailure):
            raise TypeError("Outbound cancellation reason must be an OutboundFailure")


type OutboundState = (
    AwaitingOutboundDelivery
    | ProcessingOutboundDelivery
    | WaitingOutboundRetry
    | DeliveredOutboundMessage
    | DeadLetteredOutboundMessage
    | CancelledOutboundMessage
)
"""@brief 出站消息生命周期的穷尽状态和 / Exhaustive sum of outbound-message lifecycle states."""


class InvalidOutboundTransition(RuntimeError):
    """@brief 出站聚合拒绝了非法生命周期转换 / Outbound aggregate rejected an illegal lifecycle transition."""


@dataclass(frozen=True, slots=True, init=False)
class OutboundMessage:
    """@brief 拥有投递生命周期、版本与 attempt 的聚合根 / Aggregate root owning delivery lifecycle, version, and attempts.

    @param draft 不可变出站意图 / Immutable outbound intent.
    @param stream_sequence 投递流内单调序号 / Monotonic sequence in the delivery stream.
    @param state 穷尽生命周期状态 / Exhaustive lifecycle state.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param attempt_count 已成功领取次数 / Number of successful claims.
    @param updated_at 最近转换时刻 / Most recent transition time.
    @note 新消息必须经 ``enqueue``，数据库 hydration 必须经 ``restore``；调用方不能任意拼装
        status 与 nullable 字段。/ New messages go through ``enqueue`` and database hydration
        goes through ``restore``; callers cannot arbitrarily combine status and nullable fields.
    """

    draft: OutboundDraft
    stream_sequence: MessageSequence
    state: OutboundState
    version: int
    attempt_count: int
    updated_at: datetime

    def __new__(cls, *_args: object, **_kwargs: object) -> Self:
        """@brief 禁止绕过命名构造器 / Prevent bypassing the named constructors.

        @return 永不返回 / Never returns.
        @raise TypeError 始终抛出，强制使用 enqueue 或 restore / Always raised; use enqueue or restore.
        """

        raise TypeError("Use OutboundMessage.enqueue() or OutboundMessage.restore()")

    @classmethod
    def _create(
        cls,
        *,
        draft: OutboundDraft,
        stream_sequence: MessageSequence,
        state: OutboundState,
        version: int,
        attempt_count: int,
        updated_at: datetime,
    ) -> Self:
        """@brief 经统一不变量门创建聚合 / Create an aggregate through one invariant gate.

        @param draft 出站意图 / Outbound intent.
        @param stream_sequence 投递流序号 / Delivery-stream sequence.
        @param state 生命周期状态 / Lifecycle state.
        @param version 乐观版本 / Optimistic version.
        @param attempt_count 领取次数 / Claim count.
        @param updated_at 最近转换时间 / Most recent transition time.
        @return 已验证聚合 / Validated aggregate.
        """

        if not isinstance(draft, OutboundDraft):
            raise TypeError("Outbound message requires an OutboundDraft")
        if not isinstance(stream_sequence, MessageSequence):
            raise TypeError("Outbound message requires a MessageSequence")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("Outbound version must be a non-negative integer")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise ValueError("Outbound attempt count must be a non-negative integer")
        if version < attempt_count:
            raise ValueError("Outbound version cannot trail its claim-attempt count")
        timestamp = ensure_utc(updated_at)
        if timestamp < draft.created_at:
            raise ValueError("Outbound updated_at cannot precede created_at")

        if isinstance(state, AwaitingOutboundDelivery):
            if version != 0 or attempt_count != 0:
                raise ValueError("A pending outbound must be its unclaimed initial version")
            if state.claimable_at < timestamp:
                raise ValueError("Pending outbound claim time cannot precede updated_at")
        elif isinstance(state, CancelledOutboundMessage):
            if version == 0:
                raise ValueError("A cancelled outbound requires a positive version")
        else:
            if version == 0 or attempt_count == 0:
                raise ValueError("An active or settled outbound requires a prior claim")
        if isinstance(state, WaitingOutboundRetry) and state.claimable_at < timestamp:
            raise ValueError("Outbound retry time cannot precede updated_at")

        message = object.__new__(cls)
        object.__setattr__(message, "draft", draft)
        object.__setattr__(message, "stream_sequence", stream_sequence)
        object.__setattr__(message, "state", state)
        object.__setattr__(message, "version", version)
        object.__setattr__(message, "attempt_count", attempt_count)
        object.__setattr__(message, "updated_at", timestamp)
        return message

    @classmethod
    def enqueue(
        cls,
        draft: OutboundDraft,
        *,
        stream_sequence: MessageSequence,
    ) -> Self:
        """@brief 为出站意图分配序号并进入待领取状态 / Sequence an outbound intent and make it claimable.

        @param draft 待排队意图 / Intent to enqueue.
        @param stream_sequence 原子分配的流序号 / Atomically allocated stream sequence.
        @return 初始出站聚合 / Initial outbound aggregate.
        """

        return cls._create(
            draft=draft,
            stream_sequence=stream_sequence,
            state=AwaitingOutboundDelivery(draft.created_at),
            version=0,
            attempt_count=0,
            updated_at=draft.created_at,
        )

    @classmethod
    def restore(
        cls,
        *,
        draft: OutboundDraft,
        stream_sequence: MessageSequence,
        status: OutboundStatus,
        version: int,
        attempt_count: int,
        next_attempt_at: datetime | None,
        updated_at: datetime,
        delivered_at: datetime | None,
        external_message_id: str | None,
        last_error: str | None,
    ) -> Self:
        """@brief 从持久化标量恢复并验证聚合 / Restore and validate an aggregate from persistence scalars.

        @param draft 不可变出站意图 / Immutable outbound intent.
        @param stream_sequence 投递流序号 / Delivery-stream sequence.
        @param status 持久化状态 / Persisted status.
        @param version 乐观版本 / Optimistic version.
        @param attempt_count 领取次数 / Claim count.
        @param next_attempt_at 可选下一次领取时刻 / Optional next claim time.
        @param updated_at 最近转换时刻 / Most recent transition time.
        @param delivered_at 可选成功时刻 / Optional success time.
        @param external_message_id 可选外部回执 / Optional external receipt.
        @param last_error 可选最近失败或取消原因 / Optional latest failure or cancellation reason.
        @return 已验证聚合 / Validated aggregate.
        @raise ValueError 持久化字段不符合精确状态矩阵时抛出 / Raised when persisted fields violate the exact state matrix.
        """

        if not isinstance(status, OutboundStatus):
            raise TypeError("Outbound restore requires an OutboundStatus")
        next_time = ensure_utc(next_attempt_at) if next_attempt_at else None
        completion_time = ensure_utc(delivered_at) if delivered_at else None
        failure = OutboundFailure(last_error) if last_error is not None else None

        if status is OutboundStatus.PENDING:
            if (
                next_time is None
                or completion_time is not None
                or external_message_id is not None
                or failure is not None
            ):
                raise ValueError("Pending outbound has inconsistent persistence fields")
            state: OutboundState = AwaitingOutboundDelivery(next_time)
        elif status is OutboundStatus.PROCESSING:
            if (
                next_time is not None
                or completion_time is not None
                or external_message_id is not None
                or failure is not None
            ):
                raise ValueError("Processing outbound has inconsistent persistence fields")
            state = ProcessingOutboundDelivery()
        elif status is OutboundStatus.RETRY_WAIT:
            if (
                next_time is None
                or completion_time is not None
                or external_message_id is not None
                or failure is None
            ):
                raise ValueError("Retrying outbound has inconsistent persistence fields")
            state = WaitingOutboundRetry(next_time, failure)
        elif status is OutboundStatus.DELIVERED:
            if next_time is not None or completion_time is None or failure is not None:
                raise ValueError("Delivered outbound has inconsistent persistence fields")
            state = DeliveredOutboundMessage(completion_time, external_message_id)
        elif status is OutboundStatus.FAILED_FINAL:
            if (
                next_time is not None
                or completion_time is not None
                or external_message_id is not None
                or failure is None
            ):
                raise ValueError("Dead-lettered outbound has inconsistent persistence fields")
            state = DeadLetteredOutboundMessage(failure)
        else:
            if (
                next_time is not None
                or completion_time is not None
                or external_message_id is not None
            ):
                raise ValueError("Cancelled outbound has inconsistent persistence fields")
            state = CancelledOutboundMessage(failure)

        return cls._create(
            draft=draft,
            stream_sequence=stream_sequence,
            state=state,
            version=version,
            attempt_count=attempt_count,
            updated_at=updated_at,
        )

    @property
    def status(self) -> OutboundStatus:
        """@brief 返回稳定持久化状态 / Return the stable persisted status.

        @return 状态枚举 / Status enum.
        """

        if isinstance(self.state, AwaitingOutboundDelivery):
            return OutboundStatus.PENDING
        if isinstance(self.state, ProcessingOutboundDelivery):
            return OutboundStatus.PROCESSING
        if isinstance(self.state, WaitingOutboundRetry):
            return OutboundStatus.RETRY_WAIT
        if isinstance(self.state, DeliveredOutboundMessage):
            return OutboundStatus.DELIVERED
        if isinstance(self.state, DeadLetteredOutboundMessage):
            return OutboundStatus.FAILED_FINAL
        return OutboundStatus.CANCELLED

    @property
    def next_attempt_at(self) -> datetime | None:
        """@brief 返回可选下次领取时刻 / Return the optional next claim time.

        @return 待领取/重试时刻，其他状态为 None / Claim time for pending/retry states, otherwise None.
        """

        if isinstance(self.state, AwaitingOutboundDelivery | WaitingOutboundRetry):
            return self.state.claimable_at
        return None

    @property
    def delivered_at(self) -> datetime | None:
        """@brief 返回可选成功投递时刻 / Return the optional successful-delivery time.

        @return 成功时刻或 None / Delivery time or None.
        """

        if isinstance(self.state, DeliveredOutboundMessage):
            return self.state.delivered_at
        return None

    @property
    def external_message_id(self) -> str | None:
        """@brief 返回可选外部回执 ID / Return the optional external receipt identifier.

        @return 外部 ID 或 None / External identifier or None.
        """

        if isinstance(self.state, DeliveredOutboundMessage):
            return self.state.external_message_id
        return None

    @property
    def last_error(self) -> str | None:
        """@brief 返回可选失败或取消摘要 / Return the optional failure or cancellation summary.

        @return 摘要或 None / Summary or None.
        """

        if isinstance(self.state, WaitingOutboundRetry | DeadLetteredOutboundMessage):
            return self.state.failure.summary
        if isinstance(self.state, CancelledOutboundMessage):
            return self.state.reason.summary if self.state.reason else None
        return None

    def claim(
        self,
        *,
        token: LeaseToken,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> OutboundClaim:
        """@brief 领取到期消息并签发 fencing capability / Claim a due message and issue a fencing capability.

        @param token 本次领取 token / Token for this claim.
        @param claimed_at 领取时刻 / Claim time.
        @param lease_expires_at 可回收时刻 / Lease-recovery eligibility time.
        @return processing 聚合与 ownership capability / Processing aggregate and ownership capability.
        @raise InvalidOutboundTransition 当前状态不可领取时抛出 / Raised when the current state is not claimable.
        """

        if not isinstance(self.state, AwaitingOutboundDelivery | WaitingOutboundRetry):
            raise InvalidOutboundTransition(
                f"Outbound state {self.status.value} cannot be claimed"
            )
        if not isinstance(token, LeaseToken):
            raise TypeError("Outbound claim requires a LeaseToken")
        timestamp = ensure_utc(claimed_at)
        lease_end = ensure_utc(lease_expires_at)
        if timestamp < self.updated_at:
            raise ValueError("Outbound claim time cannot precede the current version")
        if timestamp < self.state.claimable_at:
            raise ValueError("Outbound message cannot be claimed before next_attempt_at")
        if lease_end <= timestamp:
            raise ValueError("Outbound lease must expire after claim time")
        processing = type(self)._create(
            draft=self.draft,
            stream_sequence=self.stream_sequence,
            state=ProcessingOutboundDelivery(),
            version=self.version + 1,
            attempt_count=self.attempt_count + 1,
            updated_at=timestamp,
        )
        return OutboundClaim.from_processing(
            processing,
            token=token,
            lease_expires_at=lease_end,
        )

    def succeed(
        self,
        claim: OutboundClaim,
        *,
        delivered_at: datetime,
        external_message_id: str | None,
    ) -> OutboundDeliverySucceeded:
        """@brief 成功终结当前投递 claim / Successfully settle the current delivery claim.

        @param claim 当前 ownership capability / Current ownership capability.
        @param delivered_at 投递成功时刻 / Delivery success time.
        @param external_message_id 外部系统回执 / External-system receipt.
        @return 类型化成功 settlement / Typed successful settlement.
        """

        self._require_claim(claim)
        timestamp = self._transition_time(delivered_at)
        target = type(self)._create(
            draft=self.draft,
            stream_sequence=self.stream_sequence,
            state=DeliveredOutboundMessage(timestamp, external_message_id),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            updated_at=timestamp,
        )
        return OutboundDeliverySucceeded(claim=claim, message=target)

    def retry(
        self,
        claim: OutboundClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        failure: OutboundFailure,
    ) -> OutboundRetryScheduled:
        """@brief 记录失败并安排下一次投递 / Record a failure and schedule the next delivery attempt.

        @param claim 当前 ownership capability / Current ownership capability.
        @param failed_at 本次失败时刻 / Failure time.
        @param retry_at 下次可领取时刻 / Next claim time.
        @param failure 安全失败摘要 / Safe failure summary.
        @return 类型化重试 settlement / Typed retry settlement.
        """

        self._require_claim(claim)
        failure_time = self._transition_time(failed_at)
        retry_time = ensure_utc(retry_at)
        if retry_time <= failure_time:
            raise ValueError("retry_at must be later than failed_at")
        if not isinstance(failure, OutboundFailure):
            raise TypeError("Outbound retry requires an OutboundFailure")
        target = type(self)._create(
            draft=self.draft,
            stream_sequence=self.stream_sequence,
            state=WaitingOutboundRetry(retry_time, failure),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            updated_at=failure_time,
        )
        return OutboundRetryScheduled(claim=claim, message=target)

    def dead_letter(
        self,
        claim: OutboundClaim,
        *,
        failed_at: datetime,
        failure: OutboundFailure,
    ) -> OutboundDeadLettered:
        """@brief 将不可恢复 claim 移入最终失败终态 / Move an unrecoverable claim into final failure.

        @param claim 当前 ownership capability / Current ownership capability.
        @param failed_at 最终失败时刻 / Final-failure time.
        @param failure 安全失败摘要 / Safe failure summary.
        @return 类型化 dead-letter settlement / Typed dead-letter settlement.
        """

        self._require_claim(claim)
        failure_time = self._transition_time(failed_at)
        if not isinstance(failure, OutboundFailure):
            raise TypeError("Outbound dead-letter transition requires an OutboundFailure")
        target = type(self)._create(
            draft=self.draft,
            stream_sequence=self.stream_sequence,
            state=DeadLetteredOutboundMessage(failure),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            updated_at=failure_time,
        )
        return OutboundDeadLettered(claim=claim, message=target)

    def recover_expired_lease(
        self,
        claim: OutboundClaim,
        *,
        recovered_at: datetime,
        retry_at: datetime,
        failure: OutboundFailure,
    ) -> OutboundLeaseRecovered:
        """@brief 回收已到期 processing lease 并显式安排重试 / Recover an expired processing lease and explicitly schedule retry.

        @param claim 由持久化 token/lease 恢复的 ownership capability / Ownership capability restored from persisted token and lease.
        @param recovered_at 回收时刻 / Recovery time.
        @param retry_at 下一次可领取时刻 / Next claim time.
        @param failure 可观测恢复原因 / Observable recovery reason.
        @return 类型化 lease-recovery 决定 / Typed lease-recovery decision.
        @raise InvalidOutboundTransition lease 尚未到期时抛出 / Raised when the lease has not expired.
        """

        self._require_claim(claim)
        recovery_time = self._transition_time(recovered_at)
        if recovery_time < claim.lease_expires_at:
            raise InvalidOutboundTransition(
                "Outbound lease cannot be recovered before it expires"
            )
        retry_time = ensure_utc(retry_at)
        if retry_time <= recovery_time:
            raise ValueError("Recovery retry_at must be later than recovered_at")
        if not isinstance(failure, OutboundFailure):
            raise TypeError("Outbound lease recovery requires an OutboundFailure")
        target = type(self)._create(
            draft=self.draft,
            stream_sequence=self.stream_sequence,
            state=WaitingOutboundRetry(retry_time, failure),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            updated_at=recovery_time,
        )
        return OutboundLeaseRecovered(claim=claim, message=target)

    def cancel(
        self,
        *,
        cancelled_at: datetime,
        reason: OutboundFailure | None = None,
    ) -> OutboundCancelled:
        """@brief 取消尚未被 worker 持有的消息 / Cancel a message not currently owned by a worker.

        @param cancelled_at 取消时刻 / Cancellation time.
        @param reason 可选计划取消原因 / Optional plan-cancellation reason.
        @return 类型化取消决定 / Typed cancellation decision.
        @raise InvalidOutboundTransition processing 或终态消息被取消时抛出 / Raised for processing or terminal messages.
        """

        if not isinstance(self.state, AwaitingOutboundDelivery | WaitingOutboundRetry):
            raise InvalidOutboundTransition(
                f"Outbound state {self.status.value} cannot be cancelled"
            )
        if reason is not None and not isinstance(reason, OutboundFailure):
            raise TypeError("Outbound cancellation requires an OutboundFailure or None")
        timestamp = self._transition_time(cancelled_at)
        target = type(self)._create(
            draft=self.draft,
            stream_sequence=self.stream_sequence,
            state=CancelledOutboundMessage(reason),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            updated_at=timestamp,
        )
        return OutboundCancelled(previous=self, message=target)

    def _require_claim(self, claim: OutboundClaim) -> None:
        """@brief 验证 capability 拥有当前 processing 版本 / Verify that a capability owns this processing version.

        @param claim 待验证 capability / Capability to validate.
        @return None / None.
        @raise InvalidOutboundTransition capability 与聚合不匹配时抛出 / Raised when the capability does not match this aggregate.
        """

        if not isinstance(self.state, ProcessingOutboundDelivery):
            raise InvalidOutboundTransition(
                f"Outbound state {self.status.value} cannot be settled"
            )
        if claim.message != self or claim.expected_version != self.version:
            raise InvalidOutboundTransition(
                "Outbound claim does not own this message version"
            )

    def _transition_time(self, occurred_at: datetime) -> datetime:
        """@brief 校验转换时间不倒退 / Validate that a transition time does not regress.

        @param occurred_at 转换时刻 / Transition time.
        @return 规范 UTC 时刻 / Normalized UTC time.
        """

        timestamp = ensure_utc(occurred_at)
        if timestamp < self.updated_at:
            raise ValueError("Outbound transition time cannot precede the current version")
        return timestamp


@dataclass(frozen=True, slots=True, init=False)
class OutboundClaim:
    """@brief 携带 version/token/lease ownership 的投递 capability / Delivery capability carrying version, token, and lease ownership.

    @param message 已进入 processing 的聚合 / Aggregate in processing state.
    @param token 本次 fencing token / Fencing token for this claim.
    @param lease_expires_at 可回收时刻 / Lease-recovery eligibility time.
    """

    message: OutboundMessage
    token: LeaseToken
    lease_expires_at: datetime

    def __new__(cls, *_args: object, **_kwargs: object) -> Self:
        """@brief 禁止伪造投递 capability / Prevent forging a delivery capability.

        @return 永不返回 / Never returns.
        @raise TypeError 始终抛出，capability 只能由领取流程签发 / Always raised; claims are issued only by the claim flow.
        """

        raise TypeError("Outbound claims are issued by OutboundMessage.claim()")

    @classmethod
    def from_processing(
        cls,
        message: OutboundMessage,
        *,
        token: LeaseToken,
        lease_expires_at: datetime,
    ) -> Self:
        """@brief 为 processing 聚合签发 capability / Issue a capability for a processing aggregate.

        @param message processing 聚合 / Processing aggregate.
        @param token fencing token / Fencing token.
        @param lease_expires_at 可回收时刻 / Lease-recovery eligibility time.
        @return 已验证 capability / Validated capability.
        """

        if not isinstance(message, OutboundMessage):
            raise TypeError("Outbound claim requires an OutboundMessage")
        if message.status is not OutboundStatus.PROCESSING:
            raise ValueError("Outbound claims require a processing message")
        if not isinstance(token, LeaseToken):
            raise TypeError("Outbound claim requires a LeaseToken")
        lease_end = ensure_utc(lease_expires_at)
        if lease_end <= message.updated_at:
            raise ValueError("Outbound lease must expire after claim time")
        claim = object.__new__(cls)
        object.__setattr__(claim, "message", message)
        object.__setattr__(claim, "token", token)
        object.__setattr__(claim, "lease_expires_at", lease_end)
        return claim

    @property
    def expected_version(self) -> int:
        """@brief 返回 settlement 必须匹配的 processing 版本 / Return the processing version a settlement must match.

        @return 乐观版本 / Optimistic version.
        """

        return self.message.version


@dataclass(frozen=True, slots=True)
class OutboundDeliverySucceeded:
    """@brief 成功投递 settlement / Successful-delivery settlement.

    @param claim 被终结的 ownership capability / Ownership capability being settled.
    @param message 成功终态聚合 / Aggregate in successful terminal state.
    """

    claim: OutboundClaim
    message: OutboundMessage

    def __post_init__(self) -> None:
        """@brief 校验成功 settlement / Validate the successful settlement.

        @return None / None.
        """

        _validate_settlement(self.claim, self.message, OutboundStatus.DELIVERED)


@dataclass(frozen=True, slots=True)
class OutboundRetryScheduled:
    """@brief 重试等待 settlement / Retry-wait settlement.

    @param claim 被释放的 ownership capability / Ownership capability being released.
    @param message 重试等待聚合 / Aggregate in retry-wait state.
    """

    claim: OutboundClaim
    message: OutboundMessage

    def __post_init__(self) -> None:
        """@brief 校验重试 settlement / Validate the retry settlement.

        @return None / None.
        """

        _validate_settlement(self.claim, self.message, OutboundStatus.RETRY_WAIT)


@dataclass(frozen=True, slots=True)
class OutboundDeadLettered:
    """@brief 最终失败 settlement / Final-failure settlement.

    @param claim 被终结的 ownership capability / Ownership capability being settled.
    @param message 最终失败聚合 / Aggregate in final-failure state.
    """

    claim: OutboundClaim
    message: OutboundMessage

    def __post_init__(self) -> None:
        """@brief 校验最终失败 settlement / Validate the final-failure settlement.

        @return None / None.
        """

        _validate_settlement(self.claim, self.message, OutboundStatus.FAILED_FINAL)


@dataclass(frozen=True, slots=True)
class OutboundLeaseRecovered:
    """@brief 过期 lease 的恢复决定 / Recovery decision for an expired lease.

    @param claim 从持久化 pre-state 恢复的 capability / Capability restored from the persisted pre-state.
    @param message 重试等待聚合 / Aggregate in retry-wait state.
    """

    claim: OutboundClaim
    message: OutboundMessage

    def __post_init__(self) -> None:
        """@brief 校验 lease-recovery 决定 / Validate the lease-recovery decision.

        @return None / None.
        """

        _validate_settlement(self.claim, self.message, OutboundStatus.RETRY_WAIT)


@dataclass(frozen=True, slots=True)
class OutboundCancelled:
    """@brief 未领取消息的取消决定 / Cancellation decision for an unclaimed message.

    @param previous 取消前聚合 / Aggregate before cancellation.
    @param message 取消终态聚合 / Aggregate in cancelled state.
    """

    previous: OutboundMessage
    message: OutboundMessage

    def __post_init__(self) -> None:
        """@brief 校验取消决定 / Validate the cancellation decision.

        @return None / None.
        """

        if self.previous.status not in {
            OutboundStatus.PENDING,
            OutboundStatus.RETRY_WAIT,
        }:
            raise ValueError("Only claimable outbound messages can be cancelled")
        if self.message.status is not OutboundStatus.CANCELLED:
            raise ValueError("Outbound cancellation requires a cancelled target")
        if self.message.draft != self.previous.draft:
            raise ValueError("Outbound cancellation cannot replace its intent")
        if self.message.stream_sequence != self.previous.stream_sequence:
            raise ValueError("Outbound cancellation cannot replace its sequence")
        if self.message.version != self.previous.version + 1:
            raise ValueError("Outbound cancellation must advance exactly one version")
        if self.message.attempt_count != self.previous.attempt_count:
            raise ValueError("Outbound cancellation cannot change the attempt count")


type OutboundSettlement = (
    OutboundDeliverySucceeded
    | OutboundRetryScheduled
    | OutboundDeadLettered
    | OutboundLeaseRecovered
)
"""@brief Outbound claim 的穷尽 settlement 决定 / Exhaustive settlement decisions for an outbound claim."""


def _validate_settlement(
    claim: OutboundClaim,
    message: OutboundMessage,
    expected_status: OutboundStatus,
) -> None:
    """@brief 验证 settlement 保持 identity、attempt 并递增一个版本 / Validate identity, attempt count, and one-version advance.

    @param claim 来源 capability / Source capability.
    @param message 目标聚合 / Target aggregate.
    @param expected_status 预期目标状态 / Expected target status.
    @return None / None.
    """

    if not isinstance(claim, OutboundClaim) or not isinstance(
        message,
        OutboundMessage,
    ):
        raise TypeError("Outbound settlement requires a claim and a message")
    if message.status is not expected_status:
        raise ValueError(
            f"Outbound settlement requires {expected_status.value}, "
            f"found {message.status.value}"
        )
    if message.draft != claim.message.draft:
        raise ValueError("Outbound settlement cannot replace its intent")
    if message.stream_sequence != claim.message.stream_sequence:
        raise ValueError("Outbound settlement cannot replace its sequence")
    if message.version != claim.expected_version + 1:
        raise ValueError("Outbound settlement must advance exactly one version")
    if message.attempt_count != claim.message.attempt_count:
        raise ValueError("Outbound settlement cannot change the claim-attempt count")


@dataclass(frozen=True, slots=True)
class OutboundEnqueueResult:
    """@brief 幂等 outbox 入队结果 / Idempotent outbox-enqueue result.

    @param message 数据库中的规范消息 / Canonical stored message.
    @param inserted 本次是否插入 / Whether this call inserted the row.
    """

    message: OutboundMessage
    inserted: bool


__all__ = [
    "AwaitingOutboundDelivery",
    "CancelledOutboundMessage",
    "DeadLetteredOutboundMessage",
    "DeliveredOutboundMessage",
    "EDIT_TELEGRAM_MESSAGE",
    "InvalidOutboundTransition",
    "OutboundCancelled",
    "OutboundClaim",
    "OutboundDeadLettered",
    "OutboundDeliverySucceeded",
    "OutboundDraft",
    "OutboundEnqueueResult",
    "OutboundFailure",
    "OutboundKind",
    "OutboundLeaseRecovered",
    "OutboundMessage",
    "OutboundRetryScheduled",
    "OutboundSettlement",
    "OutboundState",
    "OutboundStatus",
    "ProcessingOutboundDelivery",
    "SEND_TELEGRAM_ARTIFACT",
    "SEND_TELEGRAM_ASSISTANT_PROGRESS",
    "SEND_TELEGRAM_MESSAGE",
    "SEND_TELEGRAM_PHOTO",
    "SEND_TELEGRAM_STICKER",
    "WaitingOutboundRetry",
]
