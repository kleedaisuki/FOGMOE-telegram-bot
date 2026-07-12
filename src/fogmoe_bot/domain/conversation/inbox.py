"""Durable inbound Update 模型 / Durable inbound Update models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from .identity import ConversationId, LeaseToken, UpdateId
from .payloads import JsonObject
from .temporal import ensure_utc


class InboundStatus(StrEnum):
    """@brief 持久化入口 Update 状态 / Persisted inbound-Update status."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    PROCESSED = "processed"
    FAILED_FINAL = "failed_final"


@dataclass(frozen=True, slots=True)
class InboundUpdate:
    """@brief 已落盘的 Telegram Update / Persisted Telegram Update.

    @param update_id Telegram Update ID / Telegram Update identifier.
    @param conversation_id 路由后的会话键 / Routed conversation key.
    @param payload 原始规范化载荷 / Raw normalized payload.
    @param status 入口处理状态 / Ingress-processing status.
    @param version 乐观并发版本 / Optimistic-concurrency version.
    @param attempt_count 领取次数 / Claim attempt count.
    @param next_attempt_at 下次可领取时间 / Next claimable time.
    @param received_at 接收时间 / Receipt time.
    @param updated_at 最近状态更新时间 / Most recent state-update time.
    @param processed_at 完成时间 / Completion time.
    @param last_error 最近错误 / Most recent error.
    """

    update_id: UpdateId
    conversation_id: ConversationId
    payload: JsonObject
    status: InboundStatus
    version: int
    attempt_count: int
    next_attempt_at: datetime | None
    received_at: datetime
    updated_at: datetime
    processed_at: datetime | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        """@brief 校验入口 Update 不变量 / Validate inbound-Update invariants.

        @return None / None.
        @raise ValueError 版本、尝试次数或时间非法时抛出 / Raised for invalid versions, attempts, or timestamps.
        """

        if self.version < 0 or self.attempt_count < 0:
            raise ValueError("Inbound version and attempt count cannot be negative")
        received_at = ensure_utc(self.received_at)
        updated_at = ensure_utc(self.updated_at)
        next_attempt_at = (
            ensure_utc(self.next_attempt_at) if self.next_attempt_at else None
        )
        processed_at = ensure_utc(self.processed_at) if self.processed_at else None
        if updated_at < received_at:
            raise ValueError("Inbound updated_at cannot precede received_at")
        if (
            self.status in {InboundStatus.PENDING, InboundStatus.RETRY_WAIT}
            and next_attempt_at is None
        ):
            raise ValueError("Claimable inbound updates require next_attempt_at")
        if self.status is InboundStatus.PROCESSED and processed_at is None:
            raise ValueError("Processed inbound updates require processed_at")
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "next_attempt_at", next_attempt_at)
        object.__setattr__(self, "processed_at", processed_at)
        object.__setattr__(self, "payload", dict(self.payload))

    @classmethod
    def pending(
        cls,
        *,
        update_id: UpdateId,
        conversation_id: ConversationId,
        payload: JsonObject,
        received_at: datetime,
    ) -> Self:
        """@brief 创建待处理入口 Update / Create a pending inbound Update.

        @param update_id Telegram Update ID / Telegram Update identifier.
        @param conversation_id 会话键 / Conversation key.
        @param payload 规范化 Update 载荷 / Normalized Update payload.
        @param received_at 接收时间 / Receipt time.
        @return 待处理入口实体 / Pending inbound entity.
        """

        timestamp = ensure_utc(received_at)
        return cls(
            update_id=update_id,
            conversation_id=conversation_id,
            payload=dict(payload),
            status=InboundStatus.PENDING,
            version=0,
            attempt_count=0,
            next_attempt_at=timestamp,
            received_at=timestamp,
            updated_at=timestamp,
        )


@dataclass(frozen=True, slots=True)
class InboundClaim:
    """@brief 带 fencing token 的 inbox 领取凭证 / Inbox claim carrying a fencing token.

    @param update 已进入 processing 的 Update / Update now in processing state.
    @param token 本次领取 token / Token for this claim.
    @param lease_expires_at 租约过期时间 / Lease expiration time.
    """

    update: InboundUpdate
    token: LeaseToken
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验入口领取凭证 / Validate the inbound claim.

        @return None / None.
        @raise ValueError Update 不在 processing 或租约无效时抛出 / Raised when the Update is not processing or its lease is invalid.
        """

        lease_expires_at = ensure_utc(self.lease_expires_at)
        if self.update.status is not InboundStatus.PROCESSING:
            raise ValueError("Inbound claims require a processing update")
        if lease_expires_at <= self.update.updated_at:
            raise ValueError("Inbound lease must expire after claim time")
        object.__setattr__(self, "lease_expires_at", lease_expires_at)
