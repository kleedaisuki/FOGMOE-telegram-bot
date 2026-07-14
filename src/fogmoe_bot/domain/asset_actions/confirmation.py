"""@brief 需明确同意的账户资产动作聚合 / Explicit-consent account-asset action aggregate.

本领域对象不相信模型文本、Telegram callback 或调用者传来的 actor。它只描述已经
由可信入口绑定 owner 的不可变动作，以及由数据库 fencing 保证的执行状态。/
This domain object trusts neither model text, Telegram callbacks, nor caller-supplied actors. It
describes only an immutable action whose owner was bound by trusted ingress and whose execution
state is fenced by the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.temporal import ensure_utc


class AssetActionKind(StrEnum):
    """@brief 需要账户所有人确认的高风险动作类别 / High-impact actions requiring account-owner consent."""

    BANK_REVIEW_TOKEN_REQUEST = "bank.review_token_request"
    """@brief 审核并可能发行代币 / Review and potentially issue a token request."""

    BANK_ISSUE_TOKENS = "bank.issue_tokens"
    """@brief 直接发行免费代币 / Directly issue free tokens."""

    BANK_FUND_ACTIVITY_POT = "bank.fund_activity_pot"
    """@brief 为活动奖池注资 / Fund the activity prize pot."""


class AssetActionDecision(StrEnum):
    """@brief 所有人对待确认动作的终端选择 / Account owner's terminal choice for a pending action."""

    APPROVE = "approve"
    """@brief 明确同意并开始执行 / Explicitly approve and begin execution."""

    CANCEL = "cancel"
    """@brief 明确取消且不执行 / Explicitly cancel without execution."""


class AssetActionStatus(StrEnum):
    """@brief 确认动作的持久状态机 / Persistent confirmation-action state machine."""

    PENDING = "pending"
    """@brief 等待 owner 明确选择 / Waiting for explicit owner decision."""

    EXECUTING = "executing"
    """@brief 已获同意，当前由一个 fenced worker 执行 / Approved and executing under one fenced worker."""

    EXECUTED = "executed"
    """@brief 业务执行与结果通知均已持久化 / Business execution and result notification are durable."""

    CANCELLED = "cancelled"
    """@brief owner 已取消 / Cancelled by the owner."""

    EXPIRED = "expired"
    """@brief 在同意前自然过期 / Naturally expired before approval."""


@dataclass(frozen=True, slots=True)
class AssetActionConfirmation:
    """@brief 一个 owner 绑定、可重放的资产动作确认 / An owner-bound, replayable asset-action confirmation.

    @param confirmation_id 稳定确认 ID / Stable confirmation identifier.
    @param source_key Agent 工具调用派生的幂等来源键 / Idempotency source key derived from the Agent tool invocation.
    @param kind 被批准后可执行的封闭动作类别 / Closed action kind executable after approval.
    @param owner_user_id 唯一可批准/取消的 Telegram 用户 / Sole Telegram user allowed to approve or cancel.
    @param chat_id 必须承载确认按钮的私聊 ID / Private-chat identifier that must carry the confirmation button.
    @param conversation_id 结果通知的 durable Conversation / Durable Conversation for outcome notification.
    @param delivery_stream_id 结果通知的有序 Telegram 流 / Ordered Telegram stream for outcome notification.
    @param arguments 已校验且冻结的业务参数 / Validated and frozen business arguments.
    @param status 当前确认状态 / Current confirmation state.
    @param created_at 创建时刻 / Creation time.
    @param expires_at 同意窗口截止时刻 / Approval-window deadline.
    @param updated_at 最近状态变更时刻 / Most recent state-change time.
    @param execution_token 当前 fenced 执行令牌 / Current fenced execution token.
    @param execution_lease_expires_at 执行租约截止 / Execution-lease deadline.
    @param execution_attempts 已开始的执行次数 / Number of begun execution attempts.
    @param result 已持久化的业务结果 / Persisted business result.
    @param executed_at 业务执行完成时刻 / Business-execution completion time.
    @param version 乐观版本 / Optimistic version.
    """

    confirmation_id: UUID
    source_key: str
    kind: AssetActionKind
    owner_user_id: int
    chat_id: int
    conversation_id: str
    delivery_stream_id: str
    arguments: JsonObject
    status: AssetActionStatus
    created_at: datetime
    expires_at: datetime
    updated_at: datetime
    execution_token: UUID | None = None
    execution_lease_expires_at: datetime | None = None
    execution_attempts: int = 0
    result: JsonObject | None = None
    executed_at: datetime | None = None
    version: int = 0

    def __post_init__(self) -> None:
        """@brief 验证确认聚合不变量 / Validate confirmation aggregate invariants.

        @return None / None.
        @raise ValueError 持久化状态违反 owner、时间或租约不变量时抛出 /
            Raised when persisted state violates owner, timing, or lease invariants.
        """

        source_key = self.source_key.strip()
        conversation_id = self.conversation_id.strip()
        delivery_stream_id = self.delivery_stream_id.strip()
        if not source_key or len(source_key) > 255:
            raise ValueError("Asset-action source_key must contain 1-255 characters")
        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Asset-action owner_user_id must be positive")
        if isinstance(self.chat_id, bool) or self.chat_id <= 0:
            raise ValueError("Asset-action chat_id must be a positive private-chat ID")
        if self.chat_id != self.owner_user_id:
            raise ValueError("Asset-action confirmation must bind Telegram private chat to owner")
        if not conversation_id or len(conversation_id) > 512:
            raise ValueError("Asset-action conversation_id must contain 1-512 characters")
        if not delivery_stream_id or len(delivery_stream_id) > 512:
            raise ValueError(
                "Asset-action delivery_stream_id must contain 1-512 characters"
            )
        if (
            isinstance(self.execution_attempts, bool)
            or isinstance(self.version, bool)
            or self.execution_attempts < 0
            or self.version < 0
        ):
            raise ValueError("Asset-action attempts and version cannot be negative")
        created_at = ensure_utc(self.created_at)
        expires_at = ensure_utc(self.expires_at)
        updated_at = ensure_utc(self.updated_at)
        if expires_at <= created_at:
            raise ValueError("Asset-action expiry must follow creation")
        if updated_at < created_at:
            raise ValueError("Asset-action updated_at cannot precede creation")
        lease_expires_at = (
            ensure_utc(self.execution_lease_expires_at)
            if self.execution_lease_expires_at is not None
            else None
        )
        executed_at = ensure_utc(self.executed_at) if self.executed_at is not None else None
        if self.status is AssetActionStatus.EXECUTING:
            if self.execution_token is None or lease_expires_at is None:
                raise ValueError("Executing asset action requires a fencing lease")
            if self.result is not None or executed_at is not None:
                raise ValueError("Executing asset action cannot have a final result")
        elif self.execution_token is not None or lease_expires_at is not None:
            raise ValueError("Only executing asset actions may retain a fencing lease")
        if self.status is AssetActionStatus.EXECUTED:
            if self.result is None or executed_at is None:
                raise ValueError("Executed asset action requires result and timestamp")
        elif self.result is not None or executed_at is not None:
            raise ValueError("Only executed asset actions may retain a result")
        object.__setattr__(self, "source_key", source_key)
        object.__setattr__(self, "conversation_id", conversation_id)
        object.__setattr__(self, "delivery_stream_id", delivery_stream_id)
        object.__setattr__(self, "arguments", dict(self.arguments))
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "execution_lease_expires_at", lease_expires_at)
        object.__setattr__(
            self,
            "result",
            dict(self.result) if self.result is not None else None,
        )
        object.__setattr__(self, "executed_at", executed_at)

    def is_expired_at(self, now: datetime) -> bool:
        """@brief 判断待确认动作在给定时刻是否已过期 / Test whether a pending action is expired at a time.

        @param now 可信 UTC 当前时刻 / Trusted current UTC time.
        @return 仅 pending 且到达截止时刻时为 True / True only when pending and deadline has arrived.
        """

        return (
            self.status is AssetActionStatus.PENDING
            and ensure_utc(now) >= self.expires_at
        )


__all__ = [
    "AssetActionConfirmation",
    "AssetActionDecision",
    "AssetActionKind",
    "AssetActionStatus",
]
