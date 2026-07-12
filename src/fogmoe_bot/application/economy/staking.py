"""@brief 质押应用服务 / Staking application service."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from fogmoe_bot.domain.economy import (
    WITHDRAW_FEE_RATE,
    StakeAction,
    StakeDecision,
    StakePosition,
    calculate_daily_reward_rate,
    calculate_payable_intervals,
    calculate_reward_for_intervals,
    calculate_reward_window,
)
from fogmoe_bot.domain.economy.staking import advance_reward_cursor

from .staking_ports import StakeSession, StakeTransactions

DEFAULT_STAKE_POOL_ID = 1
"""@brief 旧产品唯一质押池 ID / Legacy product's sole staking-pool ID."""


@dataclass(frozen=True, slots=True)
class OpenStake:
    """@brief 开启质押命令 / Open-stake command.

    @param user_id 用户 ID / User ID.
    @param amount 质押本金 / Principal.
    @param requested_at 业务时间 / Business time.
    @param idempotency_key 来源 Update 派生键 / Source-Update-derived key.
    """

    user_id: int
    amount: int
    requested_at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        """@brief 校验开仓命令 / Validate the open command.

        @return None / None.
        """

        _validate_command(self.user_id, self.idempotency_key)
        if self.amount <= 0:
            raise ValueError("Stake amount must be positive")


@dataclass(frozen=True, slots=True)
class CollectStakeReward:
    """@brief 领取质押奖励命令 / Collect-staking-reward command.

    @param user_id 用户 ID / User ID.
    @param requested_at 业务时间 / Business time.
    @param idempotency_key 来源 Update 派生键 / Source-Update-derived key.
    """

    user_id: int
    requested_at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        """@brief 校验领奖命令 / Validate the collection command.

        @return None / None.
        """

        _validate_command(self.user_id, self.idempotency_key)


@dataclass(frozen=True, slots=True)
class WithdrawStake:
    """@brief 取回质押本金命令 / Withdraw-stake command.

    @param user_id 用户 ID / User ID.
    @param requested_at 业务时间 / Business time.
    @param idempotency_key 来源 Update 派生键 / Source-Update-derived key.
    """

    user_id: int
    requested_at: datetime
    idempotency_key: str

    def __post_init__(self) -> None:
        """@brief 校验取回命令 / Validate the withdrawal command.

        @return None / None.
        """

        _validate_command(self.user_id, self.idempotency_key)


class StakingService:
    """@brief 以固定锁序编排质押状态转移 / Orchestrate staking transitions with a fixed lock order."""

    def __init__(
        self,
        transactions: StakeTransactions,
        *,
        admin_user_id: int,
        pool_id: int = DEFAULT_STAKE_POOL_ID,
    ) -> None:
        """@brief 注入事务端口与账户规则 / Inject transaction port and account rules.

        @param transactions 短事务工厂 / Short-transaction factory.
        @param admin_user_id 管理员账户 ID / Administrator account ID.
        @param pool_id 奖励池 ID / Reward-pool ID.
        """

        self._transactions = transactions
        self._admin_user_id = admin_user_id
        self._pool_id = pool_id

    async def status(self, user_id: int, *, now: datetime) -> StakeDecision:
        """@brief 读取质押状态 / Read staking status.

        @param user_id 用户 ID / User ID.
        @param now 业务时间 / Business time.
        @return 无副作用状态决策 / Side-effect-free status decision.
        """

        async with self._transactions.transaction() as session:
            account = await session.lock_account(user_id)
            if account is None:
                return StakeDecision(StakeAction.NOT_REGISTERED)
            position = await session.lock_stake(user_id)
            daily_rate = await _daily_rate(session)
            if position is None:
                return StakeDecision(
                    StakeAction.NO_STAKE,
                    available=account.total,
                    daily_rate=daily_rate,
                )
            reward, _, _ = calculate_reward_window(position, daily_rate, now=now)
            return StakeDecision(
                StakeAction.STATUS,
                position=position,
                available=account.total,
                reward=reward,
                daily_rate=daily_rate,
            )

    async def open(self, command: OpenStake) -> StakeDecision:
        """@brief 原子扣费并开启头寸 / Atomically charge and open a position.

        @param command 开仓命令 / Open command.
        @return 开仓结果 / Opening decision.
        """

        async with self._transactions.transaction() as session:
            account = await session.lock_account(command.user_id)
            if account is None:
                return StakeDecision(StakeAction.NOT_REGISTERED)
            replay = await session.load_receipt(command.idempotency_key)
            if replay is not None:
                return replace(replay, replayed=True)
            position = await session.lock_stake(command.user_id)
            daily_rate = await _daily_rate(session)
            if position is not None:
                decision = StakeDecision(
                    StakeAction.ALREADY_STAKED,
                    position=position,
                    available=account.total,
                    daily_rate=daily_rate,
                )
                await session.save_receipt(
                    command.idempotency_key,
                    user_id=command.user_id,
                    decision=decision,
                )
                return decision
            charged = account.spend(command.amount)
            if charged is None:
                decision = StakeDecision(
                    StakeAction.INSUFFICIENT_COINS,
                    available=account.total,
                    daily_rate=daily_rate,
                )
                await session.save_receipt(
                    command.idempotency_key,
                    user_id=command.user_id,
                    decision=decision,
                )
                return decision
            charged = replace(
                charged,
                plan=_resolve_plan(
                    command.user_id,
                    charged.paid,
                    admin_user_id=self._admin_user_id,
                ),
            )
            position = StakePosition(
                user_id=command.user_id,
                amount=command.amount,
                staked_at=command.requested_at,
                last_reward_at=None,
            )
            await session.save_account(charged)
            await session.insert_stake(position)
            decision = StakeDecision(
                StakeAction.OPENED,
                position=position,
                available=charged.total,
                daily_rate=daily_rate,
            )
            await session.save_receipt(
                command.idempotency_key,
                user_id=command.user_id,
                decision=decision,
            )
            return decision

    async def collect(self, command: CollectStakeReward) -> StakeDecision:
        """@brief 原子领取已到期奖励 / Atomically collect matured rewards.

        @param command 领奖命令 / Collection command.
        @return 领奖结果 / Collection decision.
        """

        async with self._transactions.transaction() as session:
            account = await session.lock_account(command.user_id)
            if account is None:
                return StakeDecision(StakeAction.NOT_REGISTERED)
            replay = await session.load_receipt(command.idempotency_key)
            if replay is not None:
                return replace(replay, replayed=True)
            position = await session.lock_stake(command.user_id)
            if position is None:
                decision = StakeDecision(StakeAction.NO_STAKE, available=account.total)
                await _save(session, command, decision)
                return decision
            daily_rate = await _daily_rate(session)
            due, intervals_due, cursor = calculate_reward_window(
                position,
                daily_rate,
                now=command.requested_at,
            )
            if intervals_due <= 0:
                decision = StakeDecision(
                    StakeAction.TOO_EARLY,
                    position=position,
                    available=account.total,
                    daily_rate=daily_rate,
                )
                await _save(session, command, decision)
                return decision
            if due <= 0:
                decision = StakeDecision(
                    StakeAction.BELOW_ONE_COIN,
                    position=position,
                    available=account.total,
                    daily_rate=daily_rate,
                )
                await _save(session, command, decision)
                return decision

            pool_balance = await session.lock_pool_balance(self._pool_id)
            paid_intervals = calculate_payable_intervals(
                stake_amount=position.amount,
                daily_rate=daily_rate,
                intervals_due=intervals_due,
                pool_balance=pool_balance,
            )
            reward = calculate_reward_for_intervals(
                position.amount,
                daily_rate,
                paid_intervals,
            )
            if reward <= 0:
                decision = StakeDecision(
                    StakeAction.POOL_EMPTY,
                    position=position,
                    available=account.total,
                    daily_rate=daily_rate,
                )
                await _save(session, command, decision)
                return decision
            await session.credit_free_coins(command.user_id, reward)
            await session.post_pool_delta(
                self._pool_id,
                -Decimal(reward),
                idempotency_key=f"{command.idempotency_key}:pool-debit",
            )
            updated = await session.update_reward_cursor(
                position,
                new_cursor=advance_reward_cursor(cursor, paid_intervals),
            )
            decision = StakeDecision(
                StakeAction.COLLECTED,
                position=updated,
                available=account.total + reward,
                reward=reward,
                daily_rate=daily_rate,
            )
            await _save(session, command, decision)
            return decision

    async def withdraw(self, command: WithdrawStake) -> StakeDecision:
        """@brief 原子取回本金并尽可能结算奖励 / Atomically withdraw principal and settle available rewards.

        @param command 取回命令 / Withdrawal command.
        @return 取回结果 / Withdrawal decision.
        """

        async with self._transactions.transaction() as session:
            account = await session.lock_account(command.user_id)
            if account is None:
                return StakeDecision(StakeAction.NOT_REGISTERED)
            replay = await session.load_receipt(command.idempotency_key)
            if replay is not None:
                return replace(replay, replayed=True)
            position = await session.lock_stake(command.user_id)
            if position is None:
                decision = StakeDecision(StakeAction.NO_STAKE, available=account.total)
                await _save(session, command, decision)
                return decision
            daily_rate = await _daily_rate(session)
            due, intervals_due, _ = calculate_reward_window(
                position,
                daily_rate,
                now=command.requested_at,
            )
            reward = 0
            if due > 0 and intervals_due > 0:
                pool_balance = await session.lock_pool_balance(self._pool_id)
                paid_intervals = calculate_payable_intervals(
                    stake_amount=position.amount,
                    daily_rate=daily_rate,
                    intervals_due=intervals_due,
                    pool_balance=pool_balance,
                )
                reward = calculate_reward_for_intervals(
                    position.amount,
                    daily_rate,
                    paid_intervals,
                )
                if reward > 0:
                    await session.post_pool_delta(
                        self._pool_id,
                        -Decimal(reward),
                        idempotency_key=f"{command.idempotency_key}:pool-debit",
                    )
            fee = int(Decimal(position.amount) * WITHDRAW_FEE_RATE)
            principal = max(position.amount - fee, 0)
            await session.credit_free_coins(command.user_id, principal + reward)
            await session.delete_stake(position)
            decision = StakeDecision(
                StakeAction.WITHDRAWN,
                available=account.total + principal + reward,
                reward=reward,
                principal=principal,
                fee=fee,
                daily_rate=daily_rate,
            )
            await _save(session, command, decision)
            return decision


async def _daily_rate(session: StakeSession) -> Decimal:
    """@brief 从事务会话读取并计算日回报率 / Read supply and calculate the daily rate.

    @param session 当前事务会话 / Current transaction session.
    @return 日回报百分比 / Daily percentage rate.
    """

    total_coins, total_staked = await session.supply()
    return calculate_daily_reward_rate(total_coins, total_staked)


async def _save(
    session: StakeSession,
    command: CollectStakeReward | WithdrawStake,
    decision: StakeDecision,
) -> None:
    """@brief 保存领奖或取回回执 / Save a collection or withdrawal receipt.

    @param session 当前事务会话 / Current transaction session.
    @param command 原命令 / Original command.
    @param decision 决策 / Decision.
    @return None / None.
    """

    await session.save_receipt(
        command.idempotency_key,
        user_id=command.user_id,
        decision=decision,
    )


def _resolve_plan(user_id: int, paid: int, *, admin_user_id: int) -> str:
    """@brief 按旧产品规则解析扣费后计划 / Resolve the post-charge plan using legacy rules.

    @param user_id 用户 ID / User ID.
    @param paid 付费余额 / Paid balance.
    @param admin_user_id 管理员 ID / Administrator ID.
    @return ``admin``、``paid`` 或 ``free`` / ``admin``, ``paid``, or ``free``.
    """

    if user_id == admin_user_id:
        return "admin"
    return "paid" if paid > 0 else "free"


def _validate_command(user_id: int, idempotency_key: str) -> None:
    """@brief 校验质押命令公共字段 / Validate common staking-command fields.

    @param user_id 用户 ID / User ID.
    @param idempotency_key 幂等键 / Idempotency key.
    @return None / None.
    """

    if user_id <= 0:
        raise ValueError("Stake command user_id must be positive")
    if not idempotency_key.strip() or len(idempotency_key) > 200:
        raise ValueError("Stake idempotency key must contain 1-200 characters")
