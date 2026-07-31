"""@brief Durable inbox 领域模型 / Durable-inbox domain model."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from fogmoe_bot.domain.observability.trace import TraceContext
from fogmoe_bot.domain.temporal import ensure_utc

from .identity import ConversationId, LeaseToken, UpdateId
from .payloads import JsonObject


class InboxStatus(StrEnum):
    """@brief Inbox item 的稳定持久化状态 / Stable persisted status of an inbox item."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    PROCESSED = "processed"
    FAILED_FINAL = "failed_final"


@dataclass(frozen=True, slots=True)
class InboxFailure:
    """@brief 可安全持久化的入口失败摘要 / Safely persistable ingress-failure summary.

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
            raise TypeError("Inbox failure summary must be a string")
        normalized = self.summary.strip()
        if not normalized:
            raise ValueError("Inbox failure cannot be empty")
        object.__setattr__(self, "summary", normalized[:4000])


@dataclass(frozen=True, slots=True)
class AwaitingInboxClaim:
    """@brief 等待首次领取的状态 / State awaiting its first claim.

    @param claimable_at 最早可领取时刻 / Earliest claimable time.
    """

    claimable_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范化领取时刻 / Normalize the claimable time.

        @return None / None.
        """

        object.__setattr__(self, "claimable_at", ensure_utc(self.claimable_at))


@dataclass(frozen=True, slots=True)
class ProcessingInboxItem:
    """@brief 已由带 fencing token 的 worker 领取 / Claimed by a worker carrying a fencing token."""


@dataclass(frozen=True, slots=True)
class WaitingInboxRetry:
    """@brief 失败后等待再次领取 / Waiting to be reclaimed after a failure.

    @param claimable_at 最早可再次领取时刻 / Earliest reclaim time.
    @param failure 最近一次失败 / Most recent failure.
    """

    claimable_at: datetime
    failure: InboxFailure

    def __post_init__(self) -> None:
        """@brief 校验重试状态 / Validate the retry-wait state.

        @return None / None.
        """

        object.__setattr__(self, "claimable_at", ensure_utc(self.claimable_at))
        if not isinstance(self.failure, InboxFailure):
            raise TypeError("Inbox retry state requires an InboxFailure")


@dataclass(frozen=True, slots=True)
class ProcessedInboxItem:
    """@brief 已成功处理的终态 / Successfully processed terminal state.

    @param processed_at 完成时刻 / Completion time.
    """

    processed_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范化完成时刻 / Normalize the completion time.

        @return None / None.
        """

        object.__setattr__(self, "processed_at", ensure_utc(self.processed_at))


@dataclass(frozen=True, slots=True)
class DeadLetteredInboxItem:
    """@brief 不再自动重试的终态 / Terminal state excluded from automatic retries.

    @param failure 最终失败 / Final failure.
    """

    failure: InboxFailure

    def __post_init__(self) -> None:
        """@brief 校验 dead-letter 状态 / Validate the dead-letter state.

        @return None / None.
        """

        if not isinstance(self.failure, InboxFailure):
            raise TypeError("Dead-letter state requires an InboxFailure")


type InboxState = (
    AwaitingInboxClaim
    | ProcessingInboxItem
    | WaitingInboxRetry
    | ProcessedInboxItem
    | DeadLetteredInboxItem
)
"""@brief Inbox item 的穷尽状态和 / Exhaustive sum of inbox-item states."""


@dataclass(frozen=True, slots=True, init=False)
class InboundUpdate:
    """@brief 路由可读的不可变入口事实 / Immutable inbound fact consumed by routes.

    @param update_id Telegram Update ID / Telegram Update identifier.
    @param conversation_id 规范会话键 / Canonical conversation key.
    @param payload 规范化入口载荷 / Normalized ingress payload.
    @param received_at 接收时刻 / Receipt time.
    @param trace_context 可持久传播的 trace / Persistable trace context.
    @note 本类型刻意不暴露 inbox status/version/lease；这些属于 ``InboxItem`` 聚合。/
        This type intentionally exposes no inbox status, version, or lease; those belong
        to the ``InboxItem`` aggregate.
    """

    update_id: UpdateId
    conversation_id: ConversationId
    _payload: JsonObject
    received_at: datetime
    trace_context: TraceContext

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝绕过 pending 工厂的空壳事实 / Reject shell facts that bypass the pending factory.

        @param args 未使用位置参数 / Unused positional arguments.
        @param kwargs 未使用关键字参数 / Unused keyword arguments.
        @return 永不返回 / Never returns.
        @raise TypeError 必须使用 pending / ``pending`` must be used.
        """

        del args, kwargs
        raise TypeError("Use InboundUpdate.pending()")

    @classmethod
    def _create(
        cls,
        *,
        update_id: UpdateId,
        conversation_id: ConversationId,
        payload: JsonObject,
        received_at: datetime,
        trace_context: TraceContext,
    ) -> Self:
        """@brief 经统一校验创建入口事实 / Create an inbound fact through one validation gate.

        @param update_id Telegram Update ID / Telegram Update identifier.
        @param conversation_id 规范会话键 / Canonical conversation key.
        @param payload 规范化入口载荷 / Normalized ingress payload.
        @param received_at 接收时刻 / Receipt time.
        @param trace_context 可持久传播的 trace / Persistable trace context.
        @return 不可变入口事实 / Immutable inbound fact.
        """

        if not isinstance(update_id, UpdateId):
            raise TypeError("Inbound Update requires an UpdateId")
        if not isinstance(conversation_id, ConversationId):
            raise TypeError("Inbound Update requires a ConversationId")
        if not isinstance(trace_context, TraceContext):
            raise TypeError("Inbound Update requires a TraceContext")
        update = object.__new__(cls)
        object.__setattr__(update, "update_id", update_id)
        object.__setattr__(update, "conversation_id", conversation_id)
        object.__setattr__(update, "_payload", deepcopy(payload))
        object.__setattr__(update, "received_at", ensure_utc(received_at))
        object.__setattr__(update, "trace_context", trace_context)
        return update

    @classmethod
    def pending(
        cls,
        *,
        update_id: UpdateId,
        conversation_id: ConversationId,
        payload: JsonObject,
        received_at: datetime,
        trace_context: TraceContext | None = None,
    ) -> Self:
        """@brief 捕获一个准备写入 durable inbox 的入口事实 / Capture an inbound fact ready for the durable inbox.

        @param update_id Telegram Update ID / Telegram Update identifier.
        @param conversation_id 会话键 / Conversation key.
        @param payload 规范化 Update 载荷 / Normalized Update payload.
        @param received_at 接收时刻 / Receipt time.
        @param trace_context 可选上游 trace / Optional upstream trace.
        @return 不可变入口事实 / Immutable inbound fact.
        """

        return cls._create(
            update_id=update_id,
            conversation_id=conversation_id,
            payload=payload,
            received_at=received_at,
            trace_context=trace_context or TraceContext.new_root(),
        )

    def with_trace_context(self, trace_context: TraceContext) -> Self:
        """@brief 为下游路由显式派生新的 trace carrier / Explicitly derive the trace carrier used by downstream routing.

        @param trace_context 当前处理 span 的 context / Context of the current processing span.
        @return 入口语义相同、trace 更新的新事实 / New fact with identical ingress semantics and the updated trace.
        """

        if not isinstance(trace_context, TraceContext):
            raise TypeError("Inbound Update requires a TraceContext")
        return type(self)._create(
            update_id=self.update_id,
            conversation_id=self.conversation_id,
            payload=self._payload,
            received_at=self.received_at,
            trace_context=trace_context,
        )

    @property
    def payload(self) -> JsonObject:
        """@brief 返回隔离的 payload 副本 / Return an isolated payload copy.

        @return 深拷贝 JSON 对象 / Deep-copied JSON object.
        @note 调用方无法经嵌套 dict/list 修改 durable 入口事实。/
            Callers cannot mutate the durable inbound fact through nested dicts or lists.
        """

        return deepcopy(self._payload)

    def is_replay_of(self, other: InboundUpdate) -> bool:
        """@brief 判断两个事实是否是同一入口语义的重放 / Check whether two facts replay the same ingress semantics.

        @param other 待比较入口事实 / Inbound fact to compare.
        @return ID、会话与 payload 相同则为 True / True when identity, conversation, and payload match.
        @note 接收时刻和 trace 是每次接收的观测元数据，不参与幂等语义。/
            Receipt time and trace are per-receipt observations and are excluded from idempotency semantics.
        """

        return (
            self.update_id == other.update_id
            and self.conversation_id == other.conversation_id
            and self._payload == other._payload
        )


class InvalidInboxTransition(RuntimeError):
    """@brief Inbox item 拒绝了非法生命周期转换 / Inbox item rejected an illegal lifecycle transition."""


@dataclass(frozen=True, slots=True, init=False)
class InboxItem:
    """@brief 拥有 durable ingress 生命周期的聚合根 / Aggregate root owning the durable-ingress lifecycle.

    @param update 不可变入口事实 / Immutable inbound fact.
    @param state 穷尽生命周期状态 / Exhaustive lifecycle state.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param attempt_count 已成功领取次数 / Number of successful claims.
    @param updated_at 最近转换时刻 / Most recent transition time.
    @note ``init=False`` 防止调用方任意拼装 status 与 nullable 字段；数据库恢复必须经过
        ``restore``，新事实必须经过 ``receive``。/ ``init=False`` prevents arbitrary
        combinations of status and nullable fields; database hydration goes through ``restore``
        and new facts go through ``receive``.
    """

    update: InboundUpdate
    state: InboxState
    version: int
    attempt_count: int
    updated_at: datetime

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝绕过 receive/restore 的空壳聚合 / Reject shell aggregates that bypass receive or restore.

        @param args 未使用位置参数 / Unused positional arguments.
        @param kwargs 未使用关键字参数 / Unused keyword arguments.
        @return 永不返回 / Never returns.
        @raise TypeError 必须使用领域工厂 / A domain factory must be used.
        """

        del args, kwargs
        raise TypeError("Use InboxItem.receive() or restore()")

    @classmethod
    def _create(
        cls,
        *,
        update: InboundUpdate,
        state: InboxState,
        version: int,
        attempt_count: int,
        updated_at: datetime,
    ) -> Self:
        """@brief 经统一不变量检查创建聚合 / Create an aggregate through one invariant gate.

        @param update 入口事实 / Inbound fact.
        @param state 生命周期状态 / Lifecycle state.
        @param version 乐观版本 / Optimistic version.
        @param attempt_count 领取次数 / Claim count.
        @param updated_at 最近转换时刻 / Most recent transition time.
        @return 已校验聚合 / Validated aggregate.
        """

        if not isinstance(update, InboundUpdate):
            raise TypeError("Inbox item requires an InboundUpdate")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("Inbox version must be a non-negative integer")
        if (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise ValueError("Inbox attempt count must be a non-negative integer")
        if version < attempt_count:
            raise ValueError("Inbox version cannot trail its claim-attempt count")
        timestamp = ensure_utc(updated_at)
        if timestamp < update.received_at:
            raise ValueError("Inbox updated_at cannot precede received_at")
        if isinstance(state, AwaitingInboxClaim):
            if version != 0 or attempt_count != 0:
                raise ValueError(
                    "A pending inbox item must be its unclaimed initial version"
                )
            if state.claimable_at < timestamp:
                raise ValueError("Pending inbox claim time cannot precede updated_at")
        else:
            if version == 0:
                raise ValueError("A non-pending inbox item requires a positive version")
            if attempt_count == 0:
                raise ValueError("A non-pending inbox item requires at least one claim")
        if isinstance(state, WaitingInboxRetry) and state.claimable_at < timestamp:
            raise ValueError("Inbox retry time cannot precede updated_at")

        item = object.__new__(cls)
        object.__setattr__(item, "update", update)
        object.__setattr__(item, "state", state)
        object.__setattr__(item, "version", version)
        object.__setattr__(item, "attempt_count", attempt_count)
        object.__setattr__(item, "updated_at", timestamp)
        return item

    @classmethod
    def receive(cls, update: InboundUpdate) -> Self:
        """@brief 把新入口事实放入待领取状态 / Place a new inbound fact into the claim queue.

        @param update 新入口事实 / New inbound fact.
        @return 初始 inbox item / Initial inbox item.
        """

        return cls._create(
            update=update,
            state=AwaitingInboxClaim(update.received_at),
            version=0,
            attempt_count=0,
            updated_at=update.received_at,
        )

    @classmethod
    def restore(
        cls,
        *,
        update: InboundUpdate,
        status: InboxStatus,
        version: int,
        attempt_count: int,
        next_attempt_at: datetime | None,
        updated_at: datetime,
        processed_at: datetime | None,
        last_error: str | None,
    ) -> Self:
        """@brief 从持久化标量恢复并验证聚合 / Restore and validate an aggregate from persistence scalars.

        @param update 不可变入口事实 / Immutable inbound fact.
        @param status 持久化状态 / Persisted status.
        @param version 乐观版本 / Optimistic version.
        @param attempt_count 领取次数 / Claim count.
        @param next_attempt_at 可选下次领取时刻 / Optional next claim time.
        @param updated_at 最近转换时刻 / Most recent transition time.
        @param processed_at 可选完成时刻 / Optional completion time.
        @param last_error 可选最近失败 / Optional most recent failure.
        @return 已验证聚合 / Validated aggregate.
        @raise ValueError 持久化字段不符合精确状态矩阵时抛出 / Raised when persisted fields violate the exact state matrix.
        """

        if not isinstance(status, InboxStatus):
            raise TypeError("Inbox restore requires an InboxStatus")
        next_time = ensure_utc(next_attempt_at) if next_attempt_at else None
        completion_time = ensure_utc(processed_at) if processed_at else None
        failure = InboxFailure(last_error) if last_error is not None else None

        if status is InboxStatus.PENDING:
            if next_time is None or completion_time is not None or failure is not None:
                raise ValueError(
                    "Pending inbox state has inconsistent persistence fields"
                )
            state: InboxState = AwaitingInboxClaim(next_time)
        elif status is InboxStatus.PROCESSING:
            if (
                next_time is not None
                or completion_time is not None
                or failure is not None
            ):
                raise ValueError(
                    "Processing inbox state has inconsistent persistence fields"
                )
            state = ProcessingInboxItem()
        elif status is InboxStatus.RETRY_WAIT:
            if next_time is None or completion_time is not None or failure is None:
                raise ValueError(
                    "Retrying inbox state has inconsistent persistence fields"
                )
            state = WaitingInboxRetry(next_time, failure)
        elif status is InboxStatus.PROCESSED:
            if next_time is not None or completion_time is None or failure is not None:
                raise ValueError(
                    "Processed inbox state has inconsistent persistence fields"
                )
            state = ProcessedInboxItem(completion_time)
        else:
            if next_time is not None or completion_time is not None or failure is None:
                raise ValueError(
                    "Dead-letter inbox state has inconsistent persistence fields"
                )
            state = DeadLetteredInboxItem(failure)

        return cls._create(
            update=update,
            state=state,
            version=version,
            attempt_count=attempt_count,
            updated_at=updated_at,
        )

    @property
    def status(self) -> InboxStatus:
        """@brief 返回持久化状态名称 / Return the persisted status name.

        @return 稳定状态枚举 / Stable status enum.
        """

        if isinstance(self.state, AwaitingInboxClaim):
            return InboxStatus.PENDING
        if isinstance(self.state, ProcessingInboxItem):
            return InboxStatus.PROCESSING
        if isinstance(self.state, WaitingInboxRetry):
            return InboxStatus.RETRY_WAIT
        if isinstance(self.state, ProcessedInboxItem):
            return InboxStatus.PROCESSED
        return InboxStatus.FAILED_FINAL

    @property
    def next_attempt_at(self) -> datetime | None:
        """@brief 返回可选下次领取时刻 / Return the optional next claim time.

        @return 待领取/重试时刻，其他状态为 None / Claim time for pending/retry states, otherwise None.
        """

        if isinstance(self.state, AwaitingInboxClaim | WaitingInboxRetry):
            return self.state.claimable_at
        return None

    @property
    def processed_at(self) -> datetime | None:
        """@brief 返回可选成功完成时刻 / Return the optional successful-completion time.

        @return 成功时刻或 None / Completion time or None.
        """

        return (
            self.state.processed_at
            if isinstance(self.state, ProcessedInboxItem)
            else None
        )

    @property
    def last_error(self) -> str | None:
        """@brief 返回可选持久化失败摘要 / Return the optional persisted failure summary.

        @return 失败摘要或 None / Failure summary or None.
        """

        if isinstance(self.state, WaitingInboxRetry | DeadLetteredInboxItem):
            return self.state.failure.summary
        return None

    def claim(
        self,
        *,
        token: LeaseToken,
        claimed_at: datetime,
        lease_expires_at: datetime,
    ) -> InboxClaim:
        """@brief 领取到期 item 并创建 fencing capability / Claim a due item and create a fencing capability.

        @param token 本次领取的 fencing token / Fencing token for this claim.
        @param claimed_at 领取时刻 / Claim time.
        @param lease_expires_at 可回收时刻 / Lease-recovery eligibility time.
        @return processing item 与 ownership capability / Processing item and ownership capability.
        @raise InvalidInboxTransition 当前状态不可领取时抛出 / Raised when the current state is not claimable.
        """

        if not isinstance(self.state, AwaitingInboxClaim | WaitingInboxRetry):
            raise InvalidInboxTransition(
                f"Inbox state {self.status.value} cannot be claimed"
            )
        timestamp = ensure_utc(claimed_at)
        lease_end = ensure_utc(lease_expires_at)
        if timestamp < self.updated_at:
            raise ValueError("Inbox claim time cannot precede the current version")
        if timestamp < self.state.claimable_at:
            raise ValueError("Inbox item cannot be claimed before next_attempt_at")
        if lease_end <= timestamp:
            raise ValueError("Inbox lease must expire after claim time")
        processing = type(self)._create(
            update=self.update,
            state=ProcessingInboxItem(),
            version=self.version + 1,
            attempt_count=self.attempt_count + 1,
            updated_at=timestamp,
        )
        return InboxClaim.from_processing(
            processing,
            token=token,
            lease_expires_at=lease_end,
        )

    def succeed(
        self,
        claim: InboxClaim,
        *,
        processed_at: datetime,
    ) -> InboxSucceeded:
        """@brief 成功终结当前 claim / Successfully finalize the current claim.

        @param claim 当前 ownership capability / Current ownership capability.
        @param processed_at 完成时刻 / Completion time.
        @return 类型化成功决定 / Typed success decision.
        """

        self._require_claim(claim)
        timestamp = self._transition_time(processed_at)
        target = type(self)._create(
            update=self.update,
            state=ProcessedInboxItem(timestamp),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            updated_at=timestamp,
        )
        return InboxSucceeded(claim=claim, item=target)

    def retry(
        self,
        claim: InboxClaim,
        *,
        failed_at: datetime,
        retry_at: datetime,
        failure: InboxFailure,
    ) -> InboxRetryScheduled:
        """@brief 记录失败并安排下一次领取 / Record a failure and schedule the next claim.

        @param claim 当前 ownership capability / Current ownership capability.
        @param failed_at 本次失败时刻 / Failure time.
        @param retry_at 下次可领取时刻 / Next claim time.
        @param failure 安全失败摘要 / Safe failure summary.
        @return 类型化重试决定 / Typed retry decision.
        """

        self._require_claim(claim)
        failure_time = self._transition_time(failed_at)
        retry_time = ensure_utc(retry_at)
        if retry_time <= failure_time:
            raise ValueError("retry_at must be later than failed_at")
        if not isinstance(failure, InboxFailure):
            raise TypeError("Inbox retry requires an InboxFailure")
        target = type(self)._create(
            update=self.update,
            state=WaitingInboxRetry(retry_time, failure),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            updated_at=failure_time,
        )
        return InboxRetryScheduled(claim=claim, item=target)

    def dead_letter(
        self,
        claim: InboxClaim,
        *,
        failed_at: datetime,
        failure: InboxFailure,
    ) -> InboxDeadLettered:
        """@brief 将不可恢复 claim 移入 dead-letter 终态 / Move an unrecoverable claim into the dead-letter terminal state.

        @param claim 当前 ownership capability / Current ownership capability.
        @param failed_at 最终失败时刻 / Final-failure time.
        @param failure 安全失败摘要 / Safe failure summary.
        @return 类型化 dead-letter 决定 / Typed dead-letter decision.
        """

        self._require_claim(claim)
        failure_time = self._transition_time(failed_at)
        if not isinstance(failure, InboxFailure):
            raise TypeError("Inbox dead-letter transition requires an InboxFailure")
        target = type(self)._create(
            update=self.update,
            state=DeadLetteredInboxItem(failure),
            version=self.version + 1,
            attempt_count=self.attempt_count,
            updated_at=failure_time,
        )
        return InboxDeadLettered(claim=claim, item=target)

    def _require_claim(self, claim: InboxClaim) -> None:
        """@brief 验证 capability 属于当前 processing 版本 / Verify that a capability owns this processing version.

        @param claim 待验证 capability / Capability to validate.
        @return None / None.
        @raise InvalidInboxTransition capability 与当前聚合不匹配时抛出 / Raised when the capability does not match this aggregate.
        """

        if not isinstance(self.state, ProcessingInboxItem):
            raise InvalidInboxTransition(
                f"Inbox state {self.status.value} cannot be settled"
            )
        if claim.item != self or claim.expected_version != self.version:
            raise InvalidInboxTransition("Inbox claim does not own this item version")

    def _transition_time(self, occurred_at: datetime) -> datetime:
        """@brief 校验转换时间不倒退 / Validate that a transition time does not regress.

        @param occurred_at 转换时刻 / Transition time.
        @return 规范 UTC 时刻 / Normalized UTC time.
        """

        timestamp = ensure_utc(occurred_at)
        if timestamp < self.updated_at:
            raise ValueError("Inbox transition time cannot precede the claim time")
        return timestamp


@dataclass(frozen=True, slots=True, init=False)
class InboxClaim:
    """@brief 带 version/token/lease ownership 的处理 capability / Processing capability carrying version, token, and lease ownership.

    @param item 已进入 processing 的聚合 / Aggregate in processing state.
    @param token 本次 fencing token / Fencing token for this claim.
    @param lease_expires_at 可回收时刻 / Lease-recovery eligibility time.
    """

    item: InboxItem
    token: LeaseToken
    lease_expires_at: datetime

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """@brief 拒绝伪造 ownership capability / Reject forged ownership capabilities.

        @param args 未使用位置参数 / Unused positional arguments.
        @param kwargs 未使用关键字参数 / Unused keyword arguments.
        @return 永不返回 / Never returns.
        @raise TypeError 必须从 processing 聚合签发 / A processing aggregate must issue the capability.
        """

        del args, kwargs
        raise TypeError("Use InboxClaim.from_processing()")

    @classmethod
    def from_processing(
        cls,
        item: InboxItem,
        *,
        token: LeaseToken,
        lease_expires_at: datetime,
    ) -> Self:
        """@brief 为 processing 聚合签发 capability / Issue a capability for a processing aggregate.

        @param item processing 聚合 / Processing aggregate.
        @param token fencing token / Fencing token.
        @param lease_expires_at 可回收时刻 / Lease-recovery eligibility time.
        @return 已校验 capability / Validated capability.
        """

        if item.status is not InboxStatus.PROCESSING:
            raise ValueError("Inbox claims require a processing item")
        if not isinstance(token, LeaseToken):
            raise TypeError("Inbox claim requires a LeaseToken")
        lease_end = ensure_utc(lease_expires_at)
        if lease_end <= item.updated_at:
            raise ValueError("Inbox lease must expire after claim time")
        claim = object.__new__(cls)
        object.__setattr__(claim, "item", item)
        object.__setattr__(claim, "token", token)
        object.__setattr__(claim, "lease_expires_at", lease_end)
        return claim

    @property
    def expected_version(self) -> int:
        """@brief 返回 settlement 必须匹配的 processing 版本 / Return the processing version a settlement must match.

        @return 乐观版本 / Optimistic version.
        """

        return self.item.version


@dataclass(frozen=True, slots=True)
class InboxSucceeded:
    """@brief 成功 settlement 决定 / Successful settlement decision.

    @param claim 被终结的 ownership capability / Ownership capability being settled.
    @param item 成功终态聚合 / Aggregate in successful terminal state.
    """

    claim: InboxClaim
    item: InboxItem

    def __post_init__(self) -> None:
        """@brief 校验成功决定 / Validate the success decision.

        @return None / None.
        """

        _validate_settlement(self.claim, self.item, InboxStatus.PROCESSED)


@dataclass(frozen=True, slots=True)
class InboxRetryScheduled:
    """@brief 重试 settlement 决定 / Retry settlement decision.

    @param claim 被释放的 ownership capability / Ownership capability being released.
    @param item 重试等待聚合 / Aggregate in retry-wait state.
    """

    claim: InboxClaim
    item: InboxItem

    def __post_init__(self) -> None:
        """@brief 校验重试决定 / Validate the retry decision.

        @return None / None.
        """

        _validate_settlement(self.claim, self.item, InboxStatus.RETRY_WAIT)


@dataclass(frozen=True, slots=True)
class InboxDeadLettered:
    """@brief 最终失败 settlement 决定 / Final-failure settlement decision.

    @param claim 被终结的 ownership capability / Ownership capability being settled.
    @param item dead-letter 终态聚合 / Aggregate in dead-letter terminal state.
    """

    claim: InboxClaim
    item: InboxItem

    def __post_init__(self) -> None:
        """@brief 校验 dead-letter 决定 / Validate the dead-letter decision.

        @return None / None.
        """

        _validate_settlement(self.claim, self.item, InboxStatus.FAILED_FINAL)


type InboxSettlement = InboxSucceeded | InboxRetryScheduled | InboxDeadLettered
"""@brief Inbox claim 的穷尽 settlement 决定 / Exhaustive settlement decisions for an inbox claim."""


def _validate_settlement(
    claim: InboxClaim,
    item: InboxItem,
    expected_status: InboxStatus,
) -> None:
    """@brief 验证 settlement 保持 identity、attempt 并递增一个版本 / Validate settlement identity, attempt count, and one-version advance.

    @param claim 来源 capability / Source capability.
    @param item 目标聚合 / Target aggregate.
    @param expected_status 预期目标状态 / Expected target status.
    @return None / None.
    """

    if not isinstance(claim, InboxClaim) or not isinstance(item, InboxItem):
        raise TypeError("Inbox settlement requires a claim and an InboxItem")
    if item.status is not expected_status:
        raise ValueError(
            f"Inbox settlement requires {expected_status.value}, found {item.status.value}"
        )
    if item.update != claim.item.update:
        raise ValueError("Inbox settlement cannot replace its inbound fact")
    if item.version != claim.expected_version + 1:
        raise ValueError("Inbox settlement must advance exactly one version")
    if item.attempt_count != claim.item.attempt_count:
        raise ValueError("Inbox settlement cannot change the claim-attempt count")


__all__ = [
    "AwaitingInboxClaim",
    "DeadLetteredInboxItem",
    "InboxClaim",
    "InboxDeadLettered",
    "InboxFailure",
    "InboxItem",
    "InboxRetryScheduled",
    "InboxSettlement",
    "InboxState",
    "InboxStatus",
    "InboxSucceeded",
    "InboundUpdate",
    "InvalidInboxTransition",
    "ProcessedInboxItem",
    "ProcessingInboxItem",
    "WaitingInboxRetry",
]
