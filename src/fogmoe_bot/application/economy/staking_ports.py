"""@brief 质押短事务端口 / Staking short-transaction ports."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from fogmoe_bot.domain.economy import AccountBalance, StakeDecision, StakePosition


class StakeSession(Protocol):
    """@brief 单个短事务内的质押会话 / Staking session scoped to one short transaction.

    @note 实现必须遵守锁序 ``account -> stake -> pool gate`` / Implementations must preserve lock order ``account -> stake -> pool gate``.
    """

    async def lock_account(self, user_id: int) -> AccountBalance | None:
        """@brief 加锁账户行 / Lock an account row.

        @param user_id 用户 ID / User ID.
        @return 账户快照；不存在为 None / Account snapshot, or None.
        """

        ...

    async def save_account(self, account: AccountBalance) -> None:
        """@brief 保存已锁定账户的余额 / Save balances for an already locked account.

        @param account 新账户快照 / New account snapshot.
        @return None / None.
        """

        ...

    async def credit_free_coins(self, user_id: int, amount: int) -> None:
        """@brief 增加已锁定账户的免费金币 / Credit free coins to an already locked account.

        @param user_id 用户 ID / User ID.
        @param amount 正整数金币 / Positive coin amount.
        @return None / None.
        """

        ...

    async def lock_stake(self, user_id: int) -> StakePosition | None:
        """@brief 加锁用户质押行 / Lock a user's staking row.

        @param user_id 用户 ID / User ID.
        @return 质押头寸；不存在为 None / Position, or None.
        """

        ...

    async def insert_stake(self, position: StakePosition) -> None:
        """@brief 创建质押头寸 / Insert a staking position.

        @param position 新头寸 / New position.
        @return None / None.
        """

        ...

    async def update_reward_cursor(
        self,
        position: StakePosition,
        *,
        new_cursor: datetime,
    ) -> StakePosition:
        """@brief 以版本检查推进奖励游标 / Advance the reward cursor with a version check.

        @param position 带期望版本的头寸 / Position carrying the expected version.
        @param new_cursor 新游标 / New cursor.
        @return 版本加一的头寸 / Position with incremented version.
        """

        ...

    async def delete_stake(self, position: StakePosition) -> None:
        """@brief 以版本检查删除头寸 / Delete a position with a version check.

        @param position 带期望版本的头寸 / Position carrying the expected version.
        @return None / None.
        """

        ...

    async def supply(self) -> tuple[int, int]:
        """@brief 读取流通量与质押量 / Read unstaked and staked supply.

        @return ``(total_coins, total_staked)`` / ``(total_coins, total_staked)``.
        """

        ...

    async def lock_pool_balance(self, pool_id: int) -> Decimal:
        """@brief 锁定奖励支出 gate 并读取 ledger 余额 / Lock the reward-debit gate and read ledger balance.

        @param pool_id 奖励池 ID / Reward-pool ID.
        @return 帖本净额 / Ledger net balance.
        """

        ...

    async def post_pool_delta(
        self,
        pool_id: int,
        delta: Decimal,
        *,
        idempotency_key: str,
    ) -> None:
        """@brief 追加幂等奖励池 posting / Append an idempotent reward-pool posting.

        @param pool_id 奖励池 ID / Reward-pool ID.
        @param delta 正数入账、负数出账 / Positive credit or negative debit.
        @param idempotency_key 全局唯一业务幂等键 / Globally unique business idempotency key.
        @return None / None.
        """

        ...

    async def load_receipt(self, idempotency_key: str) -> StakeDecision | None:
        """@brief 读取已提交操作回执 / Read a committed operation receipt.

        @param idempotency_key 幂等键 / Idempotency key.
        @return 原决策；未执行为 None / Original decision, or None.
        """

        ...

    async def save_receipt(
        self,
        idempotency_key: str,
        *,
        user_id: int,
        decision: StakeDecision,
    ) -> None:
        """@brief 保存与业务写入同事务的回执 / Save a receipt in the same transaction as business writes.

        @param idempotency_key 幂等键 / Idempotency key.
        @param user_id 用户 ID / User ID.
        @param decision 已提交决策 / Committed decision.
        @return None / None.
        """

        ...


class StakeTransactions(Protocol):
    """@brief 质押短事务工厂 / Staking short-transaction factory."""

    def transaction(self) -> AbstractAsyncContextManager[StakeSession]:
        """@brief 创建一个事务会话 / Create one transactional session.

        @return 异步事务上下文 / Async transaction context.
        """

        ...
