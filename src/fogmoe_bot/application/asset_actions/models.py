"""@brief 资产动作确认用例的类型化命令与结果 / Typed commands and results for asset-action confirmation use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from fogmoe_bot.domain.asset_actions.confirmation import (
    AssetActionConfirmation,
    AssetActionDecision,
    AssetActionKind,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.temporal import ensure_utc


class AssetActionDecisionCode(StrEnum):
    """@brief 一次确认回调的规范结果码 / Canonical result codes for one confirmation callback."""

    EXECUTED = "executed"
    """@brief 已执行或重放已执行结果 / Executed or replayed an executed result."""

    CANCELLED = "cancelled"
    """@brief owner 已取消 / The owner cancelled the action."""

    EXPIRED = "expired"
    """@brief 动作在同意前过期 / The action expired before approval."""

    FORBIDDEN = "forbidden"
    """@brief callback 的 owner 或私聊绑定不匹配 / Callback owner or private-chat binding did not match."""

    NOT_FOUND = "not_found"
    """@brief 确认 ID 不存在 / Confirmation identifier does not exist."""

    PROCESSING = "processing"
    """@brief 另一有效租约正在执行 / Another valid lease is executing."""


@dataclass(frozen=True, slots=True)
class ProposeAssetAction:
    """@brief 从可信 Agent 工具上下文创建确认草案 / Create a confirmation proposal from trusted Agent-tool context.

    @param confirmation_id 预先生成的稳定确认 ID / Pre-generated stable confirmation identifier.
    @param source_key Agent 调用派生的幂等来源键 / Idempotency source key derived from the Agent invocation.
    @param kind 封闭资产动作类别 / Closed asset-action kind.
    @param owner_user_id 只能确认的认证用户 / Authenticated user who alone may confirm.
    @param chat_id owner 的私聊 ID / Owner's private-chat identifier.
    @param conversation_id durable 结果通知会话 / Durable outcome-notification conversation.
    @param delivery_stream_id durable 结果通知流 / Durable outcome-notification stream.
    @param arguments 已校验的业务参数 / Validated business arguments.
    @param created_at 创建时刻 / Creation time.
    @param expires_at 过期时刻 / Expiration time.
    """

    confirmation_id: UUID
    source_key: str
    kind: AssetActionKind
    owner_user_id: int
    chat_id: int
    conversation_id: str
    delivery_stream_id: str
    arguments: JsonObject
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        """@brief 验证并冻结提议命令 / Validate and freeze the proposal command.

        @return None / None.
        @raise ValueError owner、私聊或时间边界非法时抛出 / Raised for invalid owner, private-chat, or timing boundaries.
        """

        source_key = self.source_key.strip()
        if not source_key or len(source_key) > 255:
            raise ValueError("Asset-action source_key must contain 1-255 characters")
        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Asset-action owner_user_id must be positive")
        if isinstance(self.chat_id, bool) or self.chat_id <= 0:
            raise ValueError("Asset-action chat_id must be a positive private-chat ID")
        if self.chat_id != self.owner_user_id:
            raise ValueError("Asset-action proposal must bind private chat to owner")
        if not self.conversation_id.strip() or not self.delivery_stream_id.strip():
            raise ValueError("Asset-action conversation and delivery stream must be non-empty")
        created_at = ensure_utc(self.created_at)
        expires_at = ensure_utc(self.expires_at)
        if expires_at <= created_at:
            raise ValueError("Asset-action expiry must follow creation")
        object.__setattr__(self, "source_key", source_key)
        object.__setattr__(self, "conversation_id", self.conversation_id.strip())
        object.__setattr__(self, "delivery_stream_id", self.delivery_stream_id.strip())
        object.__setattr__(self, "arguments", dict(self.arguments))
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)


@dataclass(frozen=True, slots=True)
class AssetActionDecisionCommand:
    """@brief owner 对确认按钮的一次持久化选择 / One durable owner choice on a confirmation button.

    @param confirmation_id 点击按钮引用的确认 ID / Confirmation identifier referenced by the button.
    @param decision 批准或取消 / Approve or cancel.
    @param actor_user_id Telegram 认证后的点击用户 / Telegram-authenticated clicking user.
    @param chat_id callback 所在私聊 ID / Private-chat identifier containing the callback.
    @param update_id durable Telegram Update ID / Durable Telegram Update identifier.
    @param decided_at 入口观察到的可信时刻 / Trusted time observed by ingress.
    """

    confirmation_id: UUID
    decision: AssetActionDecision
    actor_user_id: int
    chat_id: int
    update_id: int
    decided_at: datetime

    def __post_init__(self) -> None:
        """@brief 验证 callback 身份与时间 / Validate callback identity and time.

        @return None / None.
        @raise ValueError callback identity or timing is invalid / Callback 身份或时间非法时抛出.
        """

        if isinstance(self.actor_user_id, bool) or self.actor_user_id <= 0:
            raise ValueError("Asset-action callback actor must be positive")
        if isinstance(self.chat_id, bool) or self.chat_id <= 0:
            raise ValueError("Asset-action callback chat_id must be positive")
        if self.chat_id != self.actor_user_id:
            raise ValueError("Asset-action callback must originate in actor private chat")
        if isinstance(self.update_id, bool) or self.update_id < 0:
            raise ValueError("Asset-action callback update_id cannot be negative")
        object.__setattr__(self, "decided_at", ensure_utc(self.decided_at))


@dataclass(frozen=True, slots=True)
class AssetActionExecutionClaim:
    """@brief 由单个 fenced worker 持有的执行权 / Execution authority held by one fenced worker.

    @param confirmation 进入 executing 的规范确认记录 / Canonical confirmation moved to executing.
    @param token 当前执行租约 token / Current execution lease token.
    """

    confirmation: AssetActionConfirmation
    token: UUID


@dataclass(frozen=True, slots=True)
class AssetActionDecisionResult:
    """@brief 一次确认决定的用户可解释结果 / User-explainable result of one confirmation decision.

    @param code 规范决定结果码 / Canonical decision result code.
    @param confirmation 可选规范确认快照 / Optional canonical confirmation snapshot.
    @param result 已执行业务结果 / Executed business result.
    @param replayed 是否是对终态的重放 / Whether this is a replay of a terminal state.
    """

    code: AssetActionDecisionCode
    confirmation: AssetActionConfirmation | None = None
    result: JsonObject | None = None
    replayed: bool = False

    def __post_init__(self) -> None:
        """@brief 验证结果形状 / Validate result shape.

        @return None / None.
        @raise ValueError 终态结果与结果码不一致时抛出 / Raised when a terminal result disagrees with its code.
        """

        if self.code is AssetActionDecisionCode.EXECUTED:
            if self.confirmation is None or self.result is None:
                raise ValueError("Executed asset action requires confirmation and result")
        elif self.result is not None:
            raise ValueError("Only executed asset actions may expose a business result")
        object.__setattr__(self, "result", dict(self.result) if self.result is not None else None)


__all__ = [
    "AssetActionDecisionCode",
    "AssetActionDecisionCommand",
    "AssetActionDecisionResult",
    "AssetActionExecutionClaim",
    "ProposeAssetAction",
]
