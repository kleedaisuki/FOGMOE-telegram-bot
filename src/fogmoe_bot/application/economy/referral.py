"""@brief 推荐关系应用模型与端口 / Referral application models and port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .common import EconomyCode


@dataclass(frozen=True, slots=True)
class ReferralCommand:
    """@brief 绑定推荐关系命令 / Bind-referral command.

    @param invited_user_id 被邀请者 ID / Invited user ID.
    @param referrer_id 邀请人 ID / Referrer ID.
    @param invited_name 被邀请者展示名 / Invited user's display name.
    @param invitation_reward 双方邀请奖励 / Referral reward for both parties.
    @param new_user_bonus 新用户额外奖励 / Additional new-user bonus.
    @param idempotency_key 幂等键 / Idempotency key.
    """

    invited_user_id: int
    referrer_id: int
    invited_name: str
    invitation_reward: int
    new_user_bonus: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ReferralResult:
    """@brief 推荐绑定结果 / Referral-binding result.

    @param code 结果代码 / Result code.
    @param new_user 是否创建新账户 / Whether a new account was created.
    @param referrer_name 邀请人展示名 / Referrer display name.
    """

    code: EconomyCode
    new_user: bool = False
    referrer_name: str | None = None


@dataclass(frozen=True, slots=True)
class InvitedUser:
    """@brief 推荐列表条目 / Referral-list entry.

    @param user_id 被邀请者 ID / Invited user ID.
    @param name 展示名 / Display name.
    @param invited_at 邀请时间 / Invitation time.
    """

    user_id: int
    name: str
    invited_at: datetime


@dataclass(frozen=True, slots=True)
class ReferralSummary:
    """@brief 用户推荐概览 / User referral summary.

    @param referrer_id 邀请人 ID / Referrer ID.
    @param referrer_name 邀请人名 / Referrer name.
    @param invited 最近邀请列表 / Recent invited users.
    @param total 总人数 / Total count.
    """

    referrer_id: int | None
    referrer_name: str | None
    invited: tuple[InvitedUser, ...]
    total: int


class ReferralOperations(Protocol):
    """@brief 推荐关系持久化能力端口 / Referral persistence capability port."""

    async def bind_referral(self, command: ReferralCommand) -> ReferralResult:
        """@brief 原子绑定推荐 / Atomically bind a referral.

        @param command 推荐命令 / Referral command.
        @return 推荐结果 / Referral result.
        """

        ...

    async def referral_summary(self, user_id: int) -> ReferralSummary:
        """@brief 读取推荐概览 / Read a referral summary.

        @param user_id 用户 ID / User ID.
        @return 推荐概览 / Referral summary.
        """

        ...


__all__ = [
    "InvitedUser",
    "ReferralCommand",
    "ReferralOperations",
    "ReferralResult",
    "ReferralSummary",
]
