"""@brief 封闭账户方案策略与 PostgreSQL 事实读取测试 / Tests for closed account-plan policy and PostgreSQL fact reading."""

from __future__ import annotations

import asyncio

import pytest

from fogmoe_bot.domain.accounts.plan import AccountPlan, AccountPlanPolicy
from fogmoe_bot.infrastructure.database import account_plan
from fogmoe_bot.infrastructure.database.account_plan import PostgresAccountPlanResolver


def test_account_plan_policy_prioritizes_explicit_administrator_identity() -> None:
    """@brief 显式管理员身份优先于订阅事实 / Explicit administrator identity takes precedence over subscription facts."""

    policy = AccountPlanPolicy(administrator_id=7)

    assert policy.resolve(user_id=7, has_active_subscription=False) is AccountPlan.ADMIN
    assert policy.resolve(user_id=7, has_active_subscription=True) is AccountPlan.ADMIN
    assert policy.resolve(user_id=8, has_active_subscription=True) is AccountPlan.PAID
    assert policy.resolve(user_id=8, has_active_subscription=False) is AccountPlan.FREE


def test_account_plan_policy_rejects_ambiguous_runtime_values() -> None:
    """@brief 方案策略拒绝 bool-as-int 与非布尔订阅事实 / The plan policy rejects bool-as-int and non-Boolean subscription facts."""

    with pytest.raises(TypeError, match="administrator_id"):
        AccountPlanPolicy(administrator_id=True)  # type: ignore[arg-type]
    policy = AccountPlanPolicy(administrator_id=7)
    with pytest.raises(TypeError, match="has_active_subscription"):
        policy.resolve(
            user_id=8,
            has_active_subscription=1,  # type: ignore[arg-type]
        )


def test_postgres_account_plan_resolver_uses_current_subscription_in_caller_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief PostgreSQL resolver 在调用方事务中读取半开有效周期 / The PostgreSQL resolver reads the half-open effective period in the caller transaction.

    @param monkeypatch pytest 替换器 / Pytest replacement helper.
    """

    async def scenario() -> None:
        """@brief 执行 paid 方案读取 / Execute paid-plan reading.

        @return None / None.
        """

        connection = object()
        calls: list[tuple[str, tuple[object, ...], object]] = []

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[bool]:
            """@brief 记录 SQL 并返回有效订阅 / Record SQL and return an effective subscription.

            @param sql 查询文本 / Query text.
            @param params 查询参数 / Query parameters.
            @param connection 调用方事务 / Caller transaction.
            @return EXISTS true / A true EXISTS result.
            """

            calls.append((sql, params, connection))
            return (True,)

        monkeypatch.setattr(account_plan.db_connection, "fetch_one", fake_fetch_one)
        resolver = PostgresAccountPlanResolver(
            AccountPlanPolicy(administrator_id=1)
        )

        assert await resolver.resolve(42, connection=connection) is AccountPlan.PAID  # type: ignore[arg-type]
        sql, params, used_connection = calls[0]
        assert params == (42,)
        assert used_connection is connection
        assert "status = 'active'" in sql
        assert "period_starts_at <= CURRENT_TIMESTAMP" in sql
        assert "CURRENT_TIMESTAMP < period_ends_at" in sql

    asyncio.run(scenario())
