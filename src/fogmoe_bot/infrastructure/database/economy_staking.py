"""@brief PostgreSQL 质押事务适配器 / PostgreSQL staking transaction adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from decimal import Decimal
import json
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.economy.staking_ports import (
    StakeSession,
    StakeTransactions,
)
from fogmoe_bot.domain.economy import (
    AccountBalance,
    StakeAction,
    StakeDecision,
    StakePosition,
)
from fogmoe_bot.infrastructure.database import connection as db_connection


class ConcurrentStakeMutationError(RuntimeError):
    """@brief OCC 检测到质押并发修改 / OCC detected a concurrent staking mutation."""


class PostgresStakeSession(StakeSession):
    """@brief 绑定单个 SQLAlchemy 连接的质押会话 / Staking session bound to one SQLAlchemy connection."""

    def __init__(self, connection: AsyncConnection) -> None:
        """@brief 绑定已开启事务的连接 / Bind an already transactional connection.

        @param connection 当前事务连接 / Current transaction connection.
        """

        self._connection = connection

    async def lock_account(self, user_id: int) -> AccountBalance | None:
        """@brief 使用 ``FOR UPDATE`` 锁定账户 / Lock an account with ``FOR UPDATE``.

        @param user_id 用户 ID / User ID.
        @return 账户快照；不存在为 None / Account snapshot, or None.
        """

        row = await db_connection.fetch_one(
            "SELECT id, coins, coins_paid, user_plan "
            "FROM identity.users WHERE id = %s FOR UPDATE",
            (user_id,),
            connection=self._connection,
        )
        if row is None:
            return None
        return AccountBalance(
            user_id=int(row[0]),
            free=int(row[1]),
            paid=int(row[2]),
            plan=str(row[3]),
        )

    async def save_account(self, account: AccountBalance) -> None:
        """@brief 保存已锁定账户余额 / Save an already locked account balance.

        @param account 新快照 / New snapshot.
        @return None / None.
        """

        result = await db_connection.execute(
            "UPDATE identity.users SET coins = %s, coins_paid = %s, user_plan = %s "
            "WHERE id = %s",
            (account.free, account.paid, account.plan, account.user_id),
            connection=self._connection,
        )
        if result != 1:
            raise ConcurrentStakeMutationError("Locked account disappeared")

    async def credit_free_coins(self, user_id: int, amount: int) -> None:
        """@brief 向已锁账户增加免费金币 / Credit free coins to an already locked account.

        @param user_id 用户 ID / User ID.
        @param amount 正整数金币 / Positive coin amount.
        @return None / None.
        """

        if amount <= 0:
            raise ValueError("Account credit must be positive")
        result = await db_connection.execute(
            "UPDATE identity.users SET coins = coins + %s WHERE id = %s",
            (amount, user_id),
            connection=self._connection,
        )
        if result != 1:
            raise ConcurrentStakeMutationError("Locked account disappeared")

    async def lock_stake(self, user_id: int) -> StakePosition | None:
        """@brief 使用 ``FOR UPDATE`` 锁定质押头寸 / Lock a staking position with ``FOR UPDATE``.

        @param user_id 用户 ID / User ID.
        @return 质押头寸；不存在为 None / Position, or None.
        """

        row = await db_connection.fetch_one(
            "SELECT user_id, stake_amount, stake_time, last_reward_time, version "
            "FROM economy.user_stakes WHERE user_id = %s FOR UPDATE",
            (user_id,),
            connection=self._connection,
        )
        return _position_from_row(row)

    async def insert_stake(self, position: StakePosition) -> None:
        """@brief 创建质押头寸 / Insert a staking position.

        @param position 新头寸 / New position.
        @return None / None.
        """

        await db_connection.execute(
            "INSERT INTO economy.user_stakes "
            "(user_id, stake_amount, stake_time, last_reward_time, version) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                position.user_id,
                position.amount,
                position.staked_at,
                position.last_reward_at,
                position.version,
            ),
            connection=self._connection,
        )

    async def update_reward_cursor(
        self,
        position: StakePosition,
        *,
        new_cursor: datetime,
    ) -> StakePosition:
        """@brief 使用 OCC 推进奖励游标 / Advance the reward cursor using OCC.

        @param position 带期望版本的头寸 / Position with expected version.
        @param new_cursor 新游标 / New cursor.
        @return 版本加一的头寸 / Position with incremented version.
        """

        result = await db_connection.execute(
            "UPDATE economy.user_stakes "
            "SET last_reward_time = %s, version = version + 1 "
            "WHERE user_id = %s AND version = %s",
            (new_cursor, position.user_id, position.version),
            connection=self._connection,
        )
        if result != 1:
            raise ConcurrentStakeMutationError("Stake reward cursor version changed")
        return StakePosition(
            user_id=position.user_id,
            amount=position.amount,
            staked_at=position.staked_at,
            last_reward_at=new_cursor,
            version=position.version + 1,
        )

    async def delete_stake(self, position: StakePosition) -> None:
        """@brief 使用 OCC 删除质押头寸 / Delete a staking position using OCC.

        @param position 带期望版本的头寸 / Position with expected version.
        @return None / None.
        """

        result = await db_connection.execute(
            "DELETE FROM economy.user_stakes WHERE user_id = %s AND version = %s",
            (position.user_id, position.version),
            connection=self._connection,
        )
        if result != 1:
            raise ConcurrentStakeMutationError(
                "Stake version changed before withdrawal"
            )

    async def supply(self) -> tuple[int, int]:
        """@brief 统计金币与质押本金 / Sum account coins and staking principal.

        @return ``(total_coins, total_staked)`` / ``(total_coins, total_staked)``.
        """

        row = await db_connection.fetch_one(
            "SELECT "
            "(SELECT COALESCE(SUM(coins + coins_paid), 0) FROM identity.users), "
            "(SELECT COALESCE(SUM(stake_amount), 0) FROM economy.user_stakes)",
            connection=self._connection,
        )
        if row is None:
            return 0, 0
        return int(row[0]), int(row[1])

    async def lock_pool_balance(self, pool_id: int) -> Decimal:
        """@brief 锁定稀有支出 gate 并汇总 posting / Lock the infrequent debit gate and sum postings.

        @param pool_id 奖励池 ID / Reward-pool ID.
        @return 帖本净额 / Ledger net balance.
        """

        await db_connection.execute(
            "INSERT INTO economy.stake_reward_pool (id, balance) VALUES (%s, 0) "
            "ON CONFLICT (id) DO NOTHING",
            (pool_id,),
            connection=self._connection,
        )
        await db_connection.fetch_one(
            "SELECT id FROM economy.stake_reward_pool WHERE id = %s FOR UPDATE",
            (pool_id,),
            connection=self._connection,
        )
        row = await db_connection.fetch_one(
            "SELECT COALESCE(SUM(delta), 0) "
            "FROM economy.stake_pool_postings WHERE pool_id = %s",
            (pool_id,),
            connection=self._connection,
        )
        return Decimal(str(row[0] if row is not None else 0))

    async def post_pool_delta(
        self,
        pool_id: int,
        delta: Decimal,
        *,
        idempotency_key: str,
    ) -> None:
        """@brief 追加幂等奖励池 posting / Append an idempotent reward-pool posting.

        @param pool_id 奖励池 ID / Reward-pool ID.
        @param delta 净变化 / Net change.
        @param idempotency_key 业务幂等键 / Business idempotency key.
        @return None / None.
        """

        if delta == 0:
            raise ValueError("Pool posting delta cannot be zero")
        result = await db_connection.execute(
            "INSERT INTO economy.stake_pool_postings "
            "(pool_id, idempotency_key, delta) VALUES (%s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (pool_id, idempotency_key, delta),
            connection=self._connection,
        )
        if result == 1:
            return
        row = await db_connection.fetch_one(
            "SELECT pool_id, delta FROM economy.stake_pool_postings "
            "WHERE idempotency_key = %s",
            (idempotency_key,),
            connection=self._connection,
        )
        if row is None or int(row[0]) != pool_id or Decimal(str(row[1])) != delta:
            raise ConcurrentStakeMutationError(
                "Pool idempotency key was reused with different posting data"
            )

    async def load_receipt(self, idempotency_key: str) -> StakeDecision | None:
        """@brief 读取操作回执 / Read an operation receipt.

        @param idempotency_key 幂等键 / Idempotency key.
        @return 原决策；未执行为 None / Original decision, or None.
        """

        row = await db_connection.fetch_one(
            "SELECT result FROM economy.operation_receipts WHERE idempotency_key = %s",
            (idempotency_key,),
            connection=self._connection,
        )
        if row is None:
            return None
        return _decision_from_json(row[0])

    async def save_receipt(
        self,
        idempotency_key: str,
        *,
        user_id: int,
        decision: StakeDecision,
    ) -> None:
        """@brief 保存操作回执 / Save an operation receipt.

        @param idempotency_key 幂等键 / Idempotency key.
        @param user_id 用户 ID / User ID.
        @param decision 已提交决策 / Committed decision.
        @return None / None.
        """

        await db_connection.execute(
            "INSERT INTO economy.operation_receipts "
            "(idempotency_key, operation_kind, user_id, result) "
            "VALUES (%s, %s, %s, CAST(%s AS JSONB))",
            (
                idempotency_key,
                decision.action.value,
                user_id,
                json.dumps(_decision_to_mapping(decision)),
            ),
            connection=self._connection,
        )


class PostgresStakeTransactions(StakeTransactions):
    """@brief PostgreSQL 质押短事务工厂 / PostgreSQL staking short-transaction factory."""

    def transaction(self) -> AbstractAsyncContextManager[StakeSession]:
        """@brief 创建事务会话 / Create a transactional session.

        @return 异步事务上下文 / Async transaction context.
        """

        return self._transaction()

    @staticmethod
    @asynccontextmanager
    async def _transaction() -> AsyncIterator[StakeSession]:
        """@brief 将 SQLAlchemy 连接隐藏在端口后 / Hide a SQLAlchemy connection behind the port.

        @return 绑定连接的会话 / Connection-bound session.
        """

        async with db_connection.transaction() as connection:
            yield PostgresStakeSession(connection)


def _position_from_row(row: Sequence[object] | None) -> StakePosition | None:
    """@brief 把数据库行转为质押头寸 / Convert a database row to a staking position.

    @param row 数据库行 / Database row.
    @return 质押头寸；空行为 None / Position, or None for no row.
    """

    if row is None:
        return None
    return StakePosition(
        user_id=cast(int, row[0]),
        amount=cast(int, row[1]),
        staked_at=cast(datetime, row[2]),
        last_reward_at=cast(datetime | None, row[3]),
        version=cast(int, row[4]),
    )


def _decision_to_mapping(decision: StakeDecision) -> dict[str, object]:
    """@brief 序列化质押决策 / Serialize a staking decision.

    @param decision 质押决策 / Staking decision.
    @return JSON 可序列化映射 / JSON-serializable mapping.
    """

    position: dict[str, object] | None = None
    if decision.position is not None:
        position = {
            "user_id": decision.position.user_id,
            "amount": decision.position.amount,
            "staked_at": decision.position.staked_at.isoformat(),
            "last_reward_at": (
                decision.position.last_reward_at.isoformat()
                if decision.position.last_reward_at is not None
                else None
            ),
            "version": decision.position.version,
        }
    return {
        "action": decision.action.value,
        "position": position,
        "available": decision.available,
        "reward": decision.reward,
        "principal": decision.principal,
        "fee": decision.fee,
        "daily_rate": str(decision.daily_rate),
    }


def _decision_from_json(value: object) -> StakeDecision:
    """@brief 解析持久化质押决策 / Parse a persisted staking decision.

    @param value JSONB 映射或 JSON 文本 / JSONB mapping or JSON text.
    @return 质押决策 / Staking decision.
    """

    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise ValueError("Invalid staking receipt payload")
    payload = cast(Mapping[str, Any], decoded)
    raw_position = payload.get("position")
    position: StakePosition | None = None
    if isinstance(raw_position, Mapping):
        data = cast(Mapping[str, Any], raw_position)
        last_reward = data.get("last_reward_at")
        position = StakePosition(
            user_id=int(data["user_id"]),
            amount=int(data["amount"]),
            staked_at=datetime.fromisoformat(str(data["staked_at"])),
            last_reward_at=(
                datetime.fromisoformat(str(last_reward))
                if last_reward is not None
                else None
            ),
            version=int(data["version"]),
        )
    return StakeDecision(
        action=StakeAction(str(payload["action"])),
        position=position,
        available=int(payload.get("available", 0)),
        reward=int(payload.get("reward", 0)),
        principal=int(payload.get("principal", 0)),
        fee=int(payload.get("fee", 0)),
        daily_rate=Decimal(str(payload.get("daily_rate", "0.3"))),
    )


__all__ = [
    "ConcurrentStakeMutationError",
    "PostgresStakeSession",
    "PostgresStakeTransactions",
]
