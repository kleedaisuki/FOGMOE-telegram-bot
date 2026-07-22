"""@brief 非质押经济应用服务 / Non-staking economy application service."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from .common import AccountLookup, EconomyCode
from .community import (
    CommunityOperations,
    GiftCommand,
    GiftResult,
    LeaderboardCommand,
    LeaderboardResult,
    TaskClaimCommand,
    TaskClaimResult,
    calculate_gift_fee,
)
from .referral import (
    ReferralCommand,
    ReferralOperations,
    ReferralResult,
    ReferralSummary,
)
from .rewards import (
    CheckInCommand,
    CheckInResult,
    LotteryCommand,
    LotteryResult,
    RewardOperations,
    draw_lottery_prize,
)
from .web_password import (
    SetWebPassword,
    WebPasswordOperations,
    WebPasswordStatus,
    validate_web_password,
)

ECONOMY_SERVICE_DATA_KEY = "economy.service"
"""@brief runtime capability 中经济服务的稳定键 / Stable runtime-capability key for the economy service."""


class EconomyService:
    """@brief 通过窄能力端口编排经济用例 / Orchestrate economy use cases through narrow capability ports."""

    def __init__(
        self,
        *,
        accounts: AccountLookup,
        rewards: RewardOperations,
        community: CommunityOperations,
        referrals: ReferralOperations,
        web_passwords: WebPasswordOperations,
    ) -> None:
        """@brief 显式注入各项经济能力 / Explicitly inject each economy capability.

        @param accounts 账户查询能力 / Account-lookup capability.
        @param rewards 签到与抽奖能力 / Check-in and lottery capability.
        @param community 社区经济能力 / Community-economy capability.
        @param referrals 推荐关系能力 / Referral capability.
        @param web_passwords Web 密码能力 / Web-password capability.
        """

        self._accounts = accounts
        self._rewards = rewards
        self._community = community
        self._referrals = referrals
        self._web_passwords = web_passwords

    async def account_exists(self, user_id: int) -> bool:
        """@brief 检查账户 / Check whether an account exists.

        @param user_id 用户 ID / User ID.
        @return 存在为 True / True when present.
        """

        return await self._accounts.account_exists(user_id)

    async def check_in(self, command: CheckInCommand) -> CheckInResult:
        """@brief 执行签到 / Execute a check-in.

        @param command 签到命令 / Check-in command.
        @return 签到结果 / Check-in result.
        """

        _validate_identity(command.user_id, command.idempotency_key)
        return await self._rewards.check_in(command)

    async def claim_lottery(
        self,
        user_id: int,
        *,
        claimed_at: datetime,
        idempotency_key: str,
        prize: int | None = None,
    ) -> LotteryResult:
        """@brief 抽取并原子领取每日奖励 / Draw and atomically claim a daily prize.

        @param user_id 用户 ID / User ID.
        @param claimed_at 领取时刻 / Claim instant.
        @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
        @param prize 测试或恢复注入的奖励 / Prize injected by tests or recovery.
        @return 抽奖结果 / Lottery result.
        """

        _validate_identity(user_id, idempotency_key)
        drawn = draw_lottery_prize() if prize is None else prize
        if drawn <= 0:
            raise ValueError("Lottery prize must be positive")
        return await self._rewards.claim_lottery(
            LotteryCommand(
                user_id=user_id,
                prize=drawn,
                claimed_at=claimed_at,
                cooldown=timedelta(hours=24),
                idempotency_key=idempotency_key,
            )
        )

    async def give(
        self,
        sender_id: int,
        target_name: str,
        amount: int,
        *,
        business_date: date,
        idempotency_key: str,
    ) -> GiftResult:
        """@brief 校验并原子赠送金币 / Validate and atomically gift coins.

        @param sender_id 赠送者 ID / Sender ID.
        @param target_name 目标 username / Target username.
        @param amount 到账正整数 / Positive credited amount.
        @param business_date 每日计数日期 / Daily-count date.
        @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
        @return 赠送结果 / Gift result.
        """

        _validate_identity(sender_id, idempotency_key)
        normalized_name = target_name.strip().removeprefix("@")
        if not normalized_name:
            raise ValueError("Gift target name cannot be blank")
        if amount <= 0:
            raise ValueError("Gift amount must be positive")
        return await self._community.give(
            GiftCommand(
                sender_id=sender_id,
                target_name=normalized_name,
                amount=amount,
                fee=calculate_gift_fee(amount),
                business_date=business_date,
                daily_limit=5,
                idempotency_key=idempotency_key,
            )
        )

    async def leaderboard(
        self,
        requester_id: int,
        *,
        idempotency_key: str,
        limit: int = 5,
    ) -> LeaderboardResult:
        """@brief 读取可重放的有限排行榜 / Read a replayable bounded leaderboard.

        @param requester_id 请求用户 / Requesting user.
        @param idempotency_key 来源 Update 幂等键 / Source-Update idempotency key.
        @param limit 返回条目上限 / Maximum returned entries.
        @return 稳定排行榜结果 / Stable leaderboard result.
        """

        _validate_identity(requester_id, idempotency_key)
        if not 1 <= limit <= 100:
            raise ValueError("Leaderboard limit must be between 1 and 100")
        return await self._community.leaderboard(
            LeaderboardCommand(requester_id, limit, idempotency_key)
        )

    async def claim_task(self, command: TaskClaimCommand) -> TaskClaimResult:
        """@brief 领取已验证任务 / Claim an already verified task.

        @param command 任务命令 / Task command.
        @return 领取结果 / Claim result.
        """

        _validate_identity(command.user_id, command.idempotency_key)
        if command.task_id <= 0 or command.reward <= 0:
            raise ValueError("Task and reward must be positive")
        return await self._community.claim_task(command)

    async def bind_referral(self, command: ReferralCommand) -> ReferralResult:
        """@brief 绑定推荐关系 / Bind a referral relationship.

        @param command 推荐命令 / Referral command.
        @return 绑定结果 / Binding result.
        """

        _validate_identity(command.invited_user_id, command.idempotency_key)
        if command.referrer_id <= 0:
            raise ValueError("Referrer ID must be positive")
        if command.invited_user_id == command.referrer_id:
            return ReferralResult(EconomyCode.SELF_REFERRAL)
        return await self._referrals.bind_referral(command)

    async def referral_summary(self, user_id: int) -> ReferralSummary:
        """@brief 读取推荐概览 / Read a referral summary.

        @param user_id 用户 ID / User ID.
        @return 推荐概览 / Referral summary.
        """

        return await self._referrals.referral_summary(user_id)

    async def web_password_status(self, user_id: int) -> WebPasswordStatus:
        """@brief 读取 Web 密码状态 / Read web-password status.

        @param user_id 用户 ID / User ID.
        @return 密码状态 / Password status.
        """

        return await self._web_passwords.web_password_status(user_id)

    async def set_web_password(self, user_id: int, password: str) -> tuple[bool, str]:
        """@brief 校验、哈希并设置 Web 密码 / Validate, hash, and set a web password.

        @param user_id 用户 ID / User ID.
        @param password 原始密码 / Raw password.
        @return ``(is_update, message)`` / ``(is_update, message)``.
        """

        error = validate_web_password(password)
        if error is not None:
            return False, error
        status = await self._web_passwords.web_password_status(user_id)
        digest = hashlib.sha256(password.encode()).hexdigest()
        await self._web_passwords.set_web_password(SetWebPassword(user_id, digest))
        return (
            status.exists,
            "Web密码更新成功！" if status.exists else "Web密码设置成功！",
        )


def _validate_identity(user_id: int, idempotency_key: str) -> None:
    """@brief 校验用户与幂等键 / Validate user identity and idempotency key.

    @param user_id 用户 ID / User ID.
    @param idempotency_key 幂等键 / Idempotency key.
    @return None / None.
    """

    if user_id <= 0:
        raise ValueError("User ID must be positive")
    if not idempotency_key.strip() or len(idempotency_key) > 200:
        raise ValueError("Idempotency key must contain 1-200 characters")


__all__ = ["ECONOMY_SERVICE_DATA_KEY", "EconomyService"]
