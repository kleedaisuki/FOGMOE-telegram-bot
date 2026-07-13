"""@brief User Profile 清除与手动更新应用契约 / User Profile clearing and manual-refresh contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fogmoe_bot.domain.conversation.identity import ConversationId, TurnSource
from fogmoe_bot.domain.conversation.outbox import OutboundDraft, OutboundEnqueueResult
from fogmoe_bot.domain.temporal import ensure_utc


def _validate_profile_command(
    *,
    user_id: int,
    conversation_id: ConversationId,
    confirmation: OutboundDraft,
    requested_at: datetime,
) -> datetime:
    """@brief 校验 Profile 管理命令的共享 envelope / Validate the shared Profile-management envelope.

    @param user_id Profile owner / Profile owner.
    @param conversation_id 命令 Conversation / Command Conversation.
    @param confirmation 原子确认副作用 / Atomic confirmation effect.
    @param requested_at 请求时间 / Request time.
    @return 规范 UTC 时间 / Normalized UTC timestamp.
    @raise ValueError 用户或确认边界非法 / Invalid owner or confirmation boundary.
    """

    if isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("Profile management user_id must be positive")
    timestamp = ensure_utc(requested_at)
    if confirmation.conversation_id != conversation_id:
        raise ValueError("Profile confirmation must belong to its conversation")
    if confirmation.turn_id is not None:
        raise ValueError("Profile confirmation must be standalone")
    if confirmation.created_at != timestamp:
        raise ValueError("Profile command and confirmation must share one timestamp")
    return timestamp


@dataclass(frozen=True, slots=True)
class ClearUserProfile:
    """@brief 清除 Profile 及请求前画像证据 / Clear a Profile and profile evidence through the request time.

    @param source durable 幂等来源 / Durable idempotency source.
    @param conversation_id 命令 Conversation / Command Conversation.
    @param user_id Profile owner / Profile owner.
    @param confirmation 原子确认消息 / Atomic confirmation message.
    @param requested_at 清除上界 / Inclusive clearing cutoff.
    """

    source: TurnSource
    conversation_id: ConversationId
    user_id: int
    confirmation: OutboundDraft
    requested_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验清除命令 / Validate the clearing command.

        @return None / None.
        """

        object.__setattr__(
            self,
            "requested_at",
            _validate_profile_command(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                confirmation=self.confirmation,
                requested_at=self.requested_at,
            ),
        )


@dataclass(frozen=True, slots=True)
class RequestUserProfileRegeneration:
    """@brief 请求 Dreaming 尽快消费尚未归纳的新证据 / Ask Dreaming to consume unconsolidated evidence promptly.

    @param source durable 幂等来源 / Durable idempotency source.
    @param conversation_id 命令 Conversation / Command Conversation.
    @param user_id Profile owner / Profile owner.
    @param confirmation 原子确认消息 / Atomic confirmation message.
    @param requested_at 手动更新请求时刻 / Manual-refresh request time.
    """

    source: TurnSource
    conversation_id: ConversationId
    user_id: int
    confirmation: OutboundDraft
    requested_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验更新请求 / Validate the refresh request.

        @return None / None.
        """

        object.__setattr__(
            self,
            "requested_at",
            _validate_profile_command(
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                confirmation=self.confirmation,
                requested_at=self.requested_at,
            ),
        )


@dataclass(frozen=True, slots=True)
class UserProfileManagementResult:
    """@brief Profile 管理命令的幂等结果 / Idempotent result of a Profile-management command.

    @param applied 本次是否首次应用 / Whether this call first applied the command.
    @param confirmation 规范 outbox 回执 / Canonical outbox receipt.
    """

    applied: bool
    confirmation: OutboundEnqueueResult


class UserProfileManagementPersistence(Protocol):
    """@brief Profile 清除、更新请求与确认的原子持久化 / Atomic persistence for Profile commands and confirmations."""

    async def clear(
        self,
        command: ClearUserProfile,
    ) -> UserProfileManagementResult:
        """@brief 幂等清除 Profile / Idempotently clear a Profile.

        @param command 清除命令 / Clearing command.
        @return 规范结果 / Canonical result.
        """

        ...

    async def request_regeneration(
        self,
        command: RequestUserProfileRegeneration,
    ) -> UserProfileManagementResult:
        """@brief 幂等请求后台更新 / Idempotently request a background refresh.

        @param command 更新请求 / Refresh request.
        @return 规范结果 / Canonical result.
        """

        ...


__all__ = [
    "ClearUserProfile",
    "RequestUserProfileRegeneration",
    "UserProfileManagementPersistence",
    "UserProfileManagementResult",
]
