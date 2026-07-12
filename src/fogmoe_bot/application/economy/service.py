"""@brief 非质押经济应用服务 / Non-staking economy application service."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
import hashlib
from uuid import UUID, uuid4

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
from .redemption import (
    CreateCodesCommand,
    RedeemCodeCommand,
    RedeemCodeResult,
    RedemptionOperations,
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
from .shop import (
    PoolFundingCommand,
    ShopItem,
    ShopOperations,
    ShopPurchaseCommand,
    ShopPurchaseResult,
    draw_shop_reward,
)
from .topup import ApproveTopUp, TopUpAccountStatus, TopUpOperations, TopUpResult
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
        topups: TopUpOperations,
        rewards: RewardOperations,
        community: CommunityOperations,
        redemption: RedemptionOperations,
        referrals: ReferralOperations,
        web_passwords: WebPasswordOperations,
        shop: ShopOperations,
    ) -> None:
        """@brief 显式注入各项经济能力 / Explicitly inject each economy capability.

        @param accounts 账户查询能力 / Account-lookup capability.
        @param topups 充值能力 / Top-up capability.
        @param rewards 签到与抽奖能力 / Check-in and lottery capability.
        @param community 社区经济能力 / Community-economy capability.
        @param redemption 卡密能力 / Redemption-code capability.
        @param referrals 推荐关系能力 / Referral capability.
        @param web_passwords Web 密码能力 / Web-password capability.
        @param shop 商店与奖励池能力 / Shop and reward-pool capability.
        """

        self._accounts = accounts
        self._topups = topups
        self._rewards = rewards
        self._community = community
        self._redemption = redemption
        self._referrals = referrals
        self._web_passwords = web_passwords
        self._shop = shop

    async def account_exists(self, user_id: int) -> bool:
        """@brief 检查账户 / Check whether an account exists.

        @param user_id 用户 ID / User ID.
        @return 存在为 True / True when present.
        """

        return await self._accounts.account_exists(user_id)

    async def fund_pool_from_cost(
        self,
        cost: int,
        *,
        idempotency_key: str,
    ) -> Decimal:
        """@brief 按旧 20% 规则追加奖励池 credit / Append a pool credit using the legacy 20% rule.

        @param cost 正整数产品费用 / Positive product cost.
        @param idempotency_key 业务幂等键 / Business idempotency key.
        @return 已追加金额 / Posted amount.
        """

        if cost <= 0:
            return Decimal(0)
        amount = (Decimal(cost) * Decimal("0.2")).quantize(
            Decimal("0.01"),
            rounding=ROUND_DOWN,
        )
        await self._shop.fund_pool(
            PoolFundingCommand(amount=amount, idempotency_key=idempotency_key)
        )
        return amount

    async def topup_status(self, user_id: int) -> TopUpAccountStatus:
        """@brief 读取充值账户状态 / Read top-up account status.

        @param user_id 用户 ID / User ID.
        @return 充值状态 / Top-up status.
        """

        return await self._topups.topup_status(user_id)

    async def approve_topup(self, command: ApproveTopUp) -> TopUpResult:
        """@brief 幂等确认充值 / Idempotently approve a top-up.

        @param command 充值命令 / Top-up command.
        @return 充值结果 / Top-up result.
        """

        _validate_identity(command.user_id, command.idempotency_key)
        if command.coins <= 0:
            raise ValueError("Top-up coins must be positive")
        return await self._topups.approve_topup(command)

    async def block_recharge(self, user_id: int, until: datetime) -> TopUpResult:
        """@brief 设置充值禁用时间 / Set a recharge block deadline.

        @param user_id 用户 ID / User ID.
        @param until 截止时间 / Deadline.
        @return 处理结果 / Processing result.
        """

        return await self._topups.block_recharge(user_id, until)

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

    async def redeem(
        self,
        user_id: int,
        raw_code: str,
        *,
        redeemed_at: datetime,
        idempotency_key: str,
    ) -> RedeemCodeResult:
        """@brief 规范化并兑换 UUID 卡密 / Normalize and redeem a UUID code.

        @param user_id 用户 ID / User ID.
        @param raw_code 用户输入 / User input.
        @param redeemed_at 兑换时间 / Redemption time.
        @param idempotency_key 幂等键 / Idempotency key.
        @return 兑换结果 / Redemption result.
        """

        _validate_identity(user_id, idempotency_key)
        try:
            normalized = str(UUID(raw_code.strip()))
        except ValueError, AttributeError:
            return RedeemCodeResult(EconomyCode.INVALID)
        return await self._redemption.redeem(
            RedeemCodeCommand(user_id, normalized, redeemed_at, idempotency_key)
        )

    async def create_codes(self, count: int, amount: int) -> tuple[str, ...]:
        """@brief 在事务外生成 UUID 并持久化 / Generate UUIDs outside a transaction and persist them.

        @param count 1 至 20 枚 / Between 1 and 20 codes.
        @param amount 1 至 10000 金币 / Between 1 and 10000 coins.
        @return 已创建卡密 / Created codes.
        """

        if not 1 <= count <= 20 or not 1 <= amount <= 10_000:
            raise ValueError("Code count or amount is outside product limits")
        codes = tuple(str(uuid4()) for _ in range(count))
        return await self._redemption.create_codes(CreateCodesCommand(codes, amount))

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

    async def purchase(
        self,
        user_id: int,
        item: ShopItem,
        *,
        day: date,
        idempotency_key: str,
    ) -> ShopPurchaseResult:
        """@brief 抽取事务外随机数并执行购买 / Draw randomness outside the transaction and execute a purchase.

        @param user_id 用户 ID / User ID.
        @param item 商品 / Item.
        @param day 业务日期 / Business date.
        @param idempotency_key 幂等键 / Idempotency key.
        @return 购买结果 / Purchase result.
        """

        _validate_identity(user_id, idempotency_key)
        reward = draw_shop_reward(item)
        return await self._shop.purchase(
            ShopPurchaseCommand(user_id, item, day, reward, idempotency_key)
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
