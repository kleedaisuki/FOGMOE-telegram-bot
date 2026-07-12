"""@brief 质押应用服务单元测试 / Staking application-service unit tests."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from decimal import Decimal
from types import TracebackType

from fogmoe_bot.application.economy.staking import (
    CollectStakeReward,
    OpenStake,
    StakingService,
    WithdrawStake,
)
from fogmoe_bot.application.economy.staking_ports import StakeSession
from fogmoe_bot.domain.economy import (
    AccountBalance,
    StakeAction,
    StakeDecision,
    StakePosition,
    calculate_payable_intervals,
    calculate_reward_for_intervals,
)


class _Session(StakeSession):
    """@brief 记录锁序的内存会话 / In-memory session recording lock order."""

    def __init__(
        self,
        *,
        account: AccountBalance | None,
        stake: StakePosition | None = None,
        pool: Decimal = Decimal("100"),
        supply: tuple[int, int] = (900, 100),
    ) -> None:
        """@brief 创建内存会话 / Create an in-memory session.

        @param account 账户快照 / Account snapshot.
        @param stake 质押头寸 / Staking position.
        @param pool 奖励池余额 / Reward-pool balance.
        @param supply 流通与质押总量 / Unstaked and staked supply.
        """

        self.account = account
        self.stake = stake
        self.pool = pool
        self.total_supply = supply
        self.calls: list[str] = []
        self.receipts: dict[str, StakeDecision] = {}
        self.postings: list[tuple[int, Decimal, str]] = []

    async def lock_account(self, user_id: int) -> AccountBalance | None:
        """@brief 记录账户锁 / Record the account lock."""

        self.calls.append("account")
        return self.account

    async def save_account(self, account: AccountBalance) -> None:
        """@brief 保存账户 / Save the account."""

        self.calls.append("save-account")
        self.account = account

    async def credit_free_coins(self, user_id: int, amount: int) -> None:
        """@brief 记录账户入账 / Record an account credit."""

        self.calls.append("credit-account")
        assert self.account is not None
        self.account = AccountBalance(
            user_id=user_id,
            free=self.account.free + amount,
            paid=self.account.paid,
            plan=self.account.plan,
        )

    async def lock_stake(self, user_id: int) -> StakePosition | None:
        """@brief 记录质押锁 / Record the staking lock."""

        self.calls.append("stake")
        return self.stake

    async def insert_stake(self, position: StakePosition) -> None:
        """@brief 创建头寸 / Insert a position."""

        self.calls.append("insert-stake")
        self.stake = position

    async def update_reward_cursor(
        self,
        position: StakePosition,
        *,
        new_cursor: datetime,
    ) -> StakePosition:
        """@brief 推进游标 / Advance the reward cursor."""

        self.calls.append("cursor")
        self.stake = StakePosition(
            user_id=position.user_id,
            amount=position.amount,
            staked_at=position.staked_at,
            last_reward_at=new_cursor,
            version=position.version + 1,
        )
        return self.stake

    async def delete_stake(self, position: StakePosition) -> None:
        """@brief 删除头寸 / Delete the position."""

        self.calls.append("delete-stake")
        self.stake = None

    async def supply(self) -> tuple[int, int]:
        """@brief 返回总量 / Return supply totals."""

        self.calls.append("supply")
        return self.total_supply

    async def lock_pool_balance(self, pool_id: int) -> Decimal:
        """@brief 记录奖励支出 gate / Record the reward-debit gate."""

        self.calls.append("pool")
        return self.pool

    async def post_pool_delta(
        self,
        pool_id: int,
        delta: Decimal,
        *,
        idempotency_key: str,
    ) -> None:
        """@brief 记录 posting / Record a posting."""

        self.calls.append("post-pool")
        self.postings.append((pool_id, delta, idempotency_key))
        self.pool += delta

    async def load_receipt(self, idempotency_key: str) -> StakeDecision | None:
        """@brief 读取回执 / Read a receipt."""

        self.calls.append("load-receipt")
        return self.receipts.get(idempotency_key)

    async def save_receipt(
        self,
        idempotency_key: str,
        *,
        user_id: int,
        decision: StakeDecision,
    ) -> None:
        """@brief 保存回执 / Save a receipt."""

        self.calls.append("save-receipt")
        self.receipts[idempotency_key] = decision


class _TransactionContext(AbstractAsyncContextManager[StakeSession]):
    """@brief 内存事务上下文 / In-memory transaction context."""

    def __init__(self, session: _Session) -> None:
        """@brief 注入会话 / Inject the session."""

        self._session = session

    async def __aenter__(self) -> StakeSession:
        """@brief 返回会话 / Return the session."""

        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """@brief 结束内存事务 / End the in-memory transaction."""


class _Transactions:
    """@brief 固定会话的事务工厂 / Transaction factory for one fixed session."""

    def __init__(self, session: _Session) -> None:
        """@brief 注入会话 / Inject the session."""

        self._session = session

    def transaction(self) -> AbstractAsyncContextManager[StakeSession]:
        """@brief 创建事务上下文 / Create a transaction context."""

        return _TransactionContext(self._session)


def _run[T](awaitable: object) -> T:
    """@brief 运行测试协程 / Run a test coroutine.

    @param awaitable 协程 / Coroutine.
    @return 协程结果 / Coroutine result.
    """

    return asyncio.run(awaitable)  # type: ignore[arg-type, no-any-return]


def test_reward_math_preserves_legacy_rounding_and_pool_cap() -> None:
    """@brief 奖励计算保持旧的向下取整并受池余额限制 / Reward math preserves legacy floor rounding and pool cap."""

    assert calculate_reward_for_intervals(100, Decimal("0.3"), 1) == 2
    assert (
        calculate_payable_intervals(
            stake_amount=100,
            daily_rate=Decimal("0.3"),
            intervals_due=10,
            pool_balance=Decimal("14"),
        )
        == 7
    )


def test_open_spends_free_before_paid_and_replays_without_second_write() -> None:
    """@brief 开仓先扣免费余额，同键回放不重写 / Opening spends free first and same-key replay performs no second write."""

    session = _Session(account=AccountBalance(42, free=3, paid=5, plan="paid"))
    service = StakingService(_Transactions(session), admin_user_id=1)
    command = OpenStake(42, 6, datetime(2026, 1, 1), "stake:42:100")

    first = asyncio.run(service.open(command))
    calls_after_first = list(session.calls)
    second = asyncio.run(service.open(command))

    assert first.action is StakeAction.OPENED
    assert session.account == AccountBalance(42, free=0, paid=2, plan="paid")
    assert second.replayed is True
    assert session.calls[len(calls_after_first) :] == ["account", "load-receipt"]


def test_collect_uses_account_stake_pool_lock_order_and_one_atomic_receipt() -> None:
    """@brief 领奖严格使用 account→stake→pool 锁序 / Collection strictly uses account→stake→pool lock order."""

    now = datetime(2026, 2, 1)
    session = _Session(
        account=AccountBalance(42, free=10, paid=0, plan="free"),
        stake=StakePosition(42, 100, now - timedelta(days=7), None),
    )
    service = StakingService(_Transactions(session), admin_user_id=1)

    result = asyncio.run(
        service.collect(CollectStakeReward(42, now, "stake:collect:42:101"))
    )

    assert result.action is StakeAction.COLLECTED
    assert result.reward == 1
    assert session.calls.index("account") < session.calls.index("stake")
    assert session.calls.index("stake") < session.calls.index("pool")
    assert session.calls[-1] == "save-receipt"
    assert session.postings == [(1, Decimal("-1"), "stake:collect:42:101:pool-debit")]


def test_withdraw_rolls_principal_reward_and_fee_into_one_transition() -> None:
    """@brief 取回本金、奖励与手续费在一次转移完成 / Withdrawal settles principal, reward, and fee in one transition."""

    now = datetime(2026, 2, 1)
    session = _Session(
        account=AccountBalance(42, free=0, paid=0, plan="free"),
        stake=StakePosition(42, 100, now - timedelta(days=7), None),
    )
    service = StakingService(_Transactions(session), admin_user_id=1)

    result = asyncio.run(
        service.withdraw(WithdrawStake(42, now, "stake:withdraw:42:102"))
    )

    assert result.action is StakeAction.WITHDRAWN
    assert result.principal == 97
    assert result.fee == 3
    assert result.reward == 1
    assert session.account is not None and session.account.total == 98
    assert session.stake is None
