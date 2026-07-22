"""Conversation Turn 状态机 / Conversation Turn state machine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from fogmoe_bot.domain.temporal import ensure_utc

from .identity import ConversationId, TurnId, TurnSource


class TurnState(StrEnum):
    """@brief 可持久化回合状态 / Persisted turn state."""

    RECEIVED = "received"
    ACCEPTED = "accepted"
    WAITING_INFERENCE = "waiting_inference"
    INFERENCE_RETRY_WAIT = "inference_retry_wait"
    INFERENCE_COMPLETED = "inference_completed"
    WAITING_DELIVERY = "waiting_delivery"
    DELIVERY_RETRY_WAIT = "delivery_retry_wait"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED_FINAL = "failed_final"


class TurnEvent(StrEnum):
    """@brief 驱动回合状态机的领域事件 / Domain events that drive a turn state machine."""

    ACCEPT = "accept"
    REQUEST_INFERENCE = "request_inference"
    INFERENCE_SUCCEEDED = "inference_succeeded"
    SCHEDULE_INFERENCE_RETRY = "schedule_inference_retry"
    RETRY_INFERENCE = "retry_inference"
    REQUEST_DELIVERY = "request_delivery"
    DELIVERY_SUCCEEDED = "delivery_succeeded"
    SCHEDULE_DELIVERY_RETRY = "schedule_delivery_retry"
    RETRY_DELIVERY = "retry_delivery"
    CANCEL = "cancel"
    FAIL_FINAL = "fail_final"


TERMINAL_TURN_STATES = frozenset(
    {TurnState.DELIVERED, TurnState.CANCELLED, TurnState.FAILED_FINAL}
)
"""@brief 不再接受状态转移的终态 / Terminal states that reject further transitions."""

RETRY_TURN_STATES = frozenset(
    {TurnState.INFERENCE_RETRY_WAIT, TurnState.DELIVERY_RETRY_WAIT}
)
"""@brief 必须携带下次执行时间的重试等待态 / Retry-wait states requiring a next-attempt time."""

POST_ACCEPTANCE_TURN_STATES = frozenset(
    {
        TurnState.WAITING_INFERENCE,
        TurnState.INFERENCE_RETRY_WAIT,
        TurnState.INFERENCE_COMPLETED,
        TurnState.WAITING_DELIVERY,
        TurnState.DELIVERY_RETRY_WAIT,
        TurnState.DELIVERED,
        TurnState.CANCELLED,
        TurnState.FAILED_FINAL,
    }
)
"""@brief 已原子提交 acceptance 的合法当前状态 / Legal current states after atomic acceptance has committed."""

POST_INFERENCE_COMPLETION_TURN_STATES = frozenset(
    {
        TurnState.WAITING_DELIVERY,
        TurnState.DELIVERY_RETRY_WAIT,
        TurnState.DELIVERED,
        TurnState.CANCELLED,
        TurnState.FAILED_FINAL,
    }
)
"""@brief 已原子提交 inference completion 的合法当前状态 / Legal current states after atomic inference completion has committed."""

_TURN_TRANSITIONS: Mapping[
    TurnState,
    Mapping[TurnEvent, TurnState],
] = MappingProxyType(
    {
        TurnState.RECEIVED: MappingProxyType({TurnEvent.ACCEPT: TurnState.ACCEPTED}),
        TurnState.ACCEPTED: MappingProxyType(
            {TurnEvent.REQUEST_INFERENCE: TurnState.WAITING_INFERENCE}
        ),
        TurnState.WAITING_INFERENCE: MappingProxyType(
            {
                TurnEvent.INFERENCE_SUCCEEDED: TurnState.INFERENCE_COMPLETED,
                TurnEvent.SCHEDULE_INFERENCE_RETRY: TurnState.INFERENCE_RETRY_WAIT,
            }
        ),
        TurnState.INFERENCE_RETRY_WAIT: MappingProxyType(
            {TurnEvent.RETRY_INFERENCE: TurnState.WAITING_INFERENCE}
        ),
        TurnState.INFERENCE_COMPLETED: MappingProxyType(
            {TurnEvent.REQUEST_DELIVERY: TurnState.WAITING_DELIVERY}
        ),
        TurnState.WAITING_DELIVERY: MappingProxyType(
            {
                TurnEvent.DELIVERY_SUCCEEDED: TurnState.DELIVERED,
                TurnEvent.SCHEDULE_DELIVERY_RETRY: TurnState.DELIVERY_RETRY_WAIT,
            }
        ),
        TurnState.DELIVERY_RETRY_WAIT: MappingProxyType(
            {TurnEvent.RETRY_DELIVERY: TurnState.WAITING_DELIVERY}
        ),
    }
)
"""@brief 非终态的正常转移表 / Normal transition table for non-terminal states."""


class InvalidTurnTransition(RuntimeError):
    """@brief 非法回合状态转移 / Invalid turn-state transition."""


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """@brief 可乐观并发更新的会话回合快照 / Optimistically versioned conversation-turn snapshot.

    @param turn_id 回合 ID / Turn identifier.
    @param conversation_id 所属会话键 / Owning conversation key.
    @param source 来源事件 identity / Source-event identity.
    @param state 当前状态 / Current state.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param inference_attempts 推理尝试次数 / Inference attempt count.
    @param delivery_attempts 投递尝试次数 / Delivery attempt count.
    @param next_retry_at 下一次活动时间 / Next activity time.
    @param last_error 最近错误 / Most recent error.
    @param created_at 创建时间 / Creation time.
    @param updated_at 最近状态更新时间 / Most recent state-update time.
    """

    turn_id: TurnId
    conversation_id: ConversationId
    source: TurnSource
    state: TurnState
    version: int
    inference_attempts: int
    delivery_attempts: int
    next_retry_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验回合不变量 / Validate turn invariants.

        @return None / None.
        @raise ValueError 版本、计数、时间或重试字段非法时抛出 / Raised for invalid versions, counts, timestamps, or retry fields.
        """

        if not isinstance(self.source, TurnSource):
            raise TypeError("Conversation Turn source must be a TurnSource")
        if self.version < 0:
            raise ValueError("Turn version cannot be negative")
        if self.inference_attempts < 0 or self.delivery_attempts < 0:
            raise ValueError("Turn attempt counts cannot be negative")
        created_at = ensure_utc(self.created_at)
        updated_at = ensure_utc(self.updated_at)
        if updated_at < created_at:
            raise ValueError("Turn updated_at cannot precede created_at")
        retry_at = ensure_utc(self.next_retry_at) if self.next_retry_at else None
        if self.state in RETRY_TURN_STATES and retry_at is None:
            raise ValueError("Retry-wait states require next_retry_at")
        if self.state not in RETRY_TURN_STATES and retry_at is not None:
            raise ValueError("Only retry-wait states may carry next_retry_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "next_retry_at", retry_at)

    @classmethod
    def received(
        cls,
        *,
        turn_id: TurnId,
        conversation_id: ConversationId,
        source: TurnSource,
        received_at: datetime,
    ) -> Self:
        """@brief 创建刚接收的回合 / Create a newly received turn.

        @param turn_id 回合 ID / Turn identifier.
        @param conversation_id 会话键 / Conversation key.
        @param source 来源事件 identity / Source-event identity.
        @param received_at 接收时间 / Receipt time.
        @return 初始版本回合 / Initial-version turn.
        """

        timestamp = ensure_utc(received_at)
        return cls(
            turn_id=turn_id,
            conversation_id=conversation_id,
            source=source,
            state=TurnState.RECEIVED,
            version=0,
            inference_attempts=0,
            delivery_attempts=0,
            next_retry_at=None,
            last_error=None,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @property
    def is_terminal(self) -> bool:
        """@brief 判断是否终态 / Check whether the turn is terminal.

        @return 终态返回 True / True for a terminal turn.
        """

        return self.state in TERMINAL_TURN_STATES

    def transition(
        self,
        event: TurnEvent,
        *,
        occurred_at: datetime,
        retry_at: datetime | None = None,
        error: str | None = None,
    ) -> Self:
        """@brief 应用一个合法领域事件 / Apply one legal domain event.

        @param event 状态机事件 / State-machine event.
        @param occurred_at 事件时间 / Event time.
        @param retry_at 重试事件的下次时间 / Next time for a retry event.
        @param error 失败原因 / Failure reason.
        @return 版本递增的新回合快照 / New turn snapshot with an incremented version.
        @raise InvalidTurnTransition 当前状态不接受事件时抛出 / Raised when the current state rejects the event.
        @raise ValueError 时间或错误字段与事件不匹配时抛出 / Raised when timing or error fields do not match the event.
        """

        timestamp = ensure_utc(occurred_at)
        if timestamp < self.updated_at:
            raise ValueError("Transition time cannot precede the current turn version")
        if self.is_terminal:
            raise InvalidTurnTransition(
                f"Terminal state {self.state.value} rejects {event.value}"
            )

        if event is TurnEvent.CANCEL:
            target = TurnState.CANCELLED
        elif event is TurnEvent.FAIL_FINAL:
            target = TurnState.FAILED_FINAL
        else:
            normal_target = _TURN_TRANSITIONS.get(self.state, {}).get(event)
            if normal_target is None:
                raise InvalidTurnTransition(f"{self.state.value} rejects {event.value}")
            target = normal_target

        normalized_error = error.strip() if error else None
        if target in RETRY_TURN_STATES:
            if retry_at is None:
                raise ValueError("Retry transitions require retry_at")
            normalized_retry_at = ensure_utc(retry_at)
            if normalized_retry_at <= timestamp:
                raise ValueError("retry_at must be later than occurred_at")
            if normalized_error is None:
                raise ValueError("Retry transitions require an error")
        else:
            if retry_at is not None:
                raise ValueError("Non-retry transitions cannot carry retry_at")
            normalized_retry_at = None

        if event is TurnEvent.FAIL_FINAL and normalized_error is None:
            raise ValueError("Final failure requires an error")
        if (
            event
            not in {
                TurnEvent.SCHEDULE_INFERENCE_RETRY,
                TurnEvent.SCHEDULE_DELIVERY_RETRY,
                TurnEvent.FAIL_FINAL,
            }
            and normalized_error is not None
        ):
            raise ValueError("Successful transitions cannot carry an error")

        inference_attempts = self.inference_attempts
        delivery_attempts = self.delivery_attempts
        if event in {TurnEvent.REQUEST_INFERENCE, TurnEvent.RETRY_INFERENCE}:
            inference_attempts += 1
        if event in {TurnEvent.REQUEST_DELIVERY, TurnEvent.RETRY_DELIVERY}:
            delivery_attempts += 1

        return replace(
            self,
            state=target,
            version=self.version + 1,
            inference_attempts=inference_attempts,
            delivery_attempts=delivery_attempts,
            next_retry_at=normalized_retry_at,
            last_error=normalized_error,
            updated_at=timestamp,
        )
