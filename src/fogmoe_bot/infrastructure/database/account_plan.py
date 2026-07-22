"""@brief PostgreSQL 账户方案事实读取 / PostgreSQL account-plan fact reading."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.domain.accounts.plan import AccountPlan, AccountPlanPolicy
from fogmoe_bot.infrastructure.database import connection as db_connection


class TransactionalAccountPlanResolver(Protocol):
    """@brief 同事务账户方案端口 / Transaction-bound account-plan port."""

    async def resolve(
        self,
        user_id: int,
        *,
        connection: AsyncConnection,
    ) -> AccountPlan:
        """@brief 在调用方事务中推导方案 / Derive a plan in the caller transaction.

        @param user_id 待分类用户 / User to classify.
        @param connection 调用方拥有的事务连接 / Caller-owned transaction connection.
        @return 当前封闭方案 / Current closed account plan.
        """

        ...


class PostgresAccountPlanResolver:
    """@brief 从 Billing 订阅和显式管理员策略推导方案 / Derive plans from Billing subscriptions and an explicit administrator policy."""

    def __init__(self, policy: AccountPlanPolicy) -> None:
        """@brief 注入纯账户方案策略 / Inject the pure account-plan policy.

        @param policy 管理员优先的封闭方案策略 / Administrator-first closed plan policy.
        @return None / None.
        """

        self._policy = policy
        """@brief 无持久化依赖的方案决策 / Persistence-independent plan decision."""

    async def resolve(
        self,
        user_id: int,
        *,
        connection: AsyncConnection,
    ) -> AccountPlan:
        """@brief 以事务时间读取当前有效订阅并推导方案 / Read an effective subscription at transaction time and derive the plan.

        @param user_id 待分类用户 / User to classify.
        @param connection 调用方拥有的事务连接 / Caller-owned transaction connection.
        @return admin、paid 或 free / ``admin``, ``paid``, or ``free``.
        @raise RuntimeError PostgreSQL 未返回 EXISTS 结果 / PostgreSQL returns no EXISTS result.
        @note 当前没有 product-to-plan 映射；``paid`` 的唯一规则是存在状态为 active
            且当前事务时间位于 ``[period_starts_at, period_ends_at)`` 的 user
            subscription。/ No product-to-plan mapping exists yet; ``paid`` means exactly that
            an active user subscription covers the current transaction instant.
        """

        row = await db_connection.fetch_one(
            "SELECT EXISTS ("
            "SELECT 1 FROM billing.subscriptions "
            "WHERE owner_id = %s AND status = 'active' "
            "AND period_starts_at <= CURRENT_TIMESTAMP "
            "AND CURRENT_TIMESTAMP < period_ends_at"
            ")",
            (user_id,),
            connection=connection,
        )
        if row is None:
            raise RuntimeError("Account-plan subscription query returned no row")
        return self._policy.resolve(
            user_id=user_id,
            has_active_subscription=bool(row[0]),
        )


__all__ = ["PostgresAccountPlanResolver", "TransactionalAccountPlanResolver"]
